# SPDX-License-Identifier: GPL-2.0-or-later
"""The proxy a proxy fixture needs, started by the harness that needs it.

`vm-proxy` and `vm-proxy-http` are the only fixtures that prove an install
completes through a proxy, and neither runner could ever run them: the cluster
refuses them because `10.0.2.2` is qemu's user-mode gateway and exists nowhere
else, and locally they were skipped unless somebody had started a proxy by
hand. So the direction that matters to an operator on an intranet — the proxy
works and the install finishes — had never been measured.

Standard library only, and no configuration file: what the fixture asks for is
the whole specification.
"""

from __future__ import annotations

import selectors
import socket
import socketserver
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, Iterator
from urllib.parse import urlsplit

#: How long a client has to finish its handshake, and how long a stalled
#: tunnel is carried. A guest fetching a stage3 goes quiet between chunks, so
#: this bounds the handshake rather than the transfer.
HANDSHAKE_SECONDS: Final[float] = 30.0

#: What one `recv` takes off either side of a tunnel.
CHUNK_BYTES: Final[int] = 65536

SOCKS_VERSION: Final[int] = 5
AUTH_NONE: Final[int] = 0
AUTH_PASSWORD: Final[int] = 2
AUTH_UNACCEPTABLE: Final[int] = 0xFF
COMMAND_CONNECT: Final[int] = 1
ADDRESS_IPV4: Final[int] = 1
ADDRESS_DOMAIN: Final[int] = 3
ADDRESS_IPV6: Final[int] = 4
REPLY_OK: Final[int] = 0
REPLY_REFUSED: Final[int] = 5
REPLY_UNSUPPORTED: Final[int] = 7


@dataclass(frozen=True)
class Credential:
    """What a client must present, empty when the proxy asks for nothing."""

    username: str = ""
    password: str = ""

    @property
    def demanded(self) -> bool:
        return bool(self.username or self.password)


def _pipe(one: socket.socket, other: socket.socket) -> None:
    """Carry bytes both ways until either side closes.

    One selector rather than two threads: a tunnel that loses its reader
    thread leaks it for the life of the run, and a fixture opens one per
    fetch.
    """
    one.settimeout(None)
    other.settimeout(None)
    with selectors.DefaultSelector() as watching:
        watching.register(one, selectors.EVENT_READ, other)
        watching.register(other, selectors.EVENT_READ, one)
        open_ends = 2
        while open_ends == 2:
            for key, _ in watching.select():
                source = key.fileobj
                assert isinstance(source, socket.socket)
                target = key.data
                try:
                    chunk = source.recv(CHUNK_BYTES)
                except OSError:
                    chunk = b""
                if not chunk:
                    open_ends -= 1
                    break
                try:
                    target.sendall(chunk)
                except OSError:
                    open_ends -= 1
                    break


def _connect(host: str, port: int) -> socket.socket | None:
    try:
        return socket.create_connection((host, port), timeout=HANDSHAKE_SECONDS)
    except OSError:
        return None


class _Socks5(socketserver.BaseRequestHandler):
    """One SOCKS5 CONNECT, with RFC 1929 authentication when one is demanded."""

    credential: Credential = Credential()

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(HANDSHAKE_SECONDS)
        try:
            if not self._greet(client) or not self._authenticate(client):
                return
            upstream = self._requested(client)
        except OSError:
            return
        if upstream is None:
            return
        with upstream:
            _pipe(client, upstream)

    def _greet(self, client: socket.socket) -> bool:
        head = _exactly(client, 2)
        if len(head) != 2 or head[0] != SOCKS_VERSION:
            return False
        offered = set(_exactly(client, head[1]))
        wanted = AUTH_PASSWORD if self.credential.demanded else AUTH_NONE
        if wanted not in offered:
            client.sendall(bytes((SOCKS_VERSION, AUTH_UNACCEPTABLE)))
            return False
        client.sendall(bytes((SOCKS_VERSION, wanted)))
        return True

    def _authenticate(self, client: socket.socket) -> bool:
        if not self.credential.demanded:
            return True
        version = _exactly(client, 1)
        if version != b"\x01":
            return False
        name = _exactly(client, _exactly(client, 1)[0]).decode(errors="replace")
        secret = _exactly(client, _exactly(client, 1)[0]).decode(errors="replace")
        allowed = (name, secret) == (self.credential.username, self.credential.password)
        client.sendall(bytes((1, 0 if allowed else 1)))
        return allowed

    def _requested(self, client: socket.socket) -> socket.socket | None:
        head = _exactly(client, 4)
        if len(head) != 4 or head[0] != SOCKS_VERSION:
            return None
        if head[1] != COMMAND_CONNECT:
            _refuse(client, REPLY_UNSUPPORTED)
            return None
        kind = head[3]
        if kind == ADDRESS_IPV4:
            host = socket.inet_ntoa(_exactly(client, 4))
        elif kind == ADDRESS_DOMAIN:
            host = _exactly(client, _exactly(client, 1)[0]).decode(errors="replace")
        elif kind == ADDRESS_IPV6:
            host = socket.inet_ntop(socket.AF_INET6, _exactly(client, 16))
        else:
            _refuse(client, REPLY_UNSUPPORTED)
            return None
        port = int.from_bytes(_exactly(client, 2), "big")
        upstream = _connect(host, port)
        if upstream is None:
            _refuse(client, REPLY_REFUSED)
            return None
        # The bound address the reply carries is ignored by every client that
        # only tunnels, so the unspecified one is honest rather than invented.
        client.sendall(bytes((SOCKS_VERSION, REPLY_OK, 0, ADDRESS_IPV4, 0, 0, 0, 0, 0, 0)))
        return upstream


def _refuse(client: socket.socket, reply: int) -> None:
    client.sendall(bytes((SOCKS_VERSION, reply, 0, ADDRESS_IPV4, 0, 0, 0, 0, 0, 0)))


def _exactly(client: socket.socket, count: int) -> bytes:
    """Exactly `count` bytes, or fewer when the peer stopped.

    `recv` answers with what has arrived rather than what was asked for, and a
    handshake read short is a handshake misparsed.
    """
    seen = b""
    while len(seen) < count:
        chunk = client.recv(count - len(seen))
        if not chunk:
            break
        seen += chunk
    return seen


class _Http(socketserver.BaseRequestHandler):
    """One HTTP proxy request: `CONNECT` tunnelled, anything else forwarded.

    Both, because a fixture uses both: `emerge-webrsync` hands gemato only
    `http_proxy` and fetches over plain HTTP with an absolute request target,
    while a stage3 over https arrives as `CONNECT`.
    """

    credential: Credential = Credential()

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(HANDSHAKE_SECONDS)
        try:
            head = _until_headers(client)
            if not head:
                return
            request_line, _, rest = head.partition(b"\r\n")
            method, _, remainder = request_line.partition(b" ")
            target, _, version = remainder.partition(b" ")
            if method.upper() == b"CONNECT":
                self._tunnel(client, target.decode(errors="replace"))
                return
            self._forward(client, method, target.decode(errors="replace"), version, rest)
        except OSError:
            return

    def _tunnel(self, client: socket.socket, authority: str) -> None:
        host, _, port = authority.rpartition(":")
        upstream = _connect(host, int(port or 443))
        if upstream is None:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            _pipe(client, upstream)

    def _forward(
        self, client: socket.socket, method: bytes, target: str, version: bytes, rest: bytes
    ) -> None:
        parsed = urlsplit(target)
        if not parsed.hostname:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        upstream = _connect(parsed.hostname, parsed.port or 80)
        if upstream is None:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        with upstream:
            # The origin form, because a server answering an absolute target
            # is optional and Portage's mirrors are ordinary web servers.
            upstream.sendall(b"%s %s %s\r\n%s" % (method, path.encode(), version, rest))
            _pipe(client, upstream)


def _until_headers(client: socket.socket) -> bytes:
    """The request line and its headers, including the blank line that ends
    them. The body is left on the socket for the tunnel to carry."""
    seen = b""
    while b"\r\n\r\n" not in seen:
        chunk = client.recv(CHUNK_BYTES)
        if not chunk:
            return b""
        seen += chunk
    return seen


class _Threaded(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def serving(kind: str, port: int, credential: Credential = Credential()) -> Iterator[int]:
    """Run a proxy of `kind` on `port` for the life of the block.

    `port` may be zero, and the port actually bound is what is yielded: a
    fixture names its own, and a test does not want to.
    """
    handlers = {"socks5": _Socks5, "http": _Http}
    if kind not in handlers:
        raise ValueError(f"no proxy of kind {kind!r}; have {', '.join(sorted(handlers))}")
    handler = type(f"_Bound{kind}", (handlers[kind],), {"credential": credential})
    with _Threaded(("0.0.0.0", port), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield int(server.server_address[1])
        finally:
            server.shutdown()
            thread.join(timeout=HANDSHAKE_SECONDS)
