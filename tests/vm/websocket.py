"""A websocket client, enough of RFC 6455 to carry a serial console.

Proxmox hands out a console over `vncwebsocket` and nothing else: the node's
serial socket is a file on the node, port 22 is closed, and the API is the only
way in. The standard library has no websocket client, and the harness takes no
dependency the workstation does not already have, so this is the subset that
protocol needs: a masked client frame, an unmasked server frame, ping and
close. No extensions, no fragmentation of what is sent.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from types import TracebackType
from typing import Final, Protocol, Self

#: Continuation and reserved opcodes never appear here: the server sends one
#: frame per write, and this client sends one frame per call.
_TEXT: Final[int] = 0x1
_BINARY: Final[int] = 0x2
_CLOSE: Final[int] = 0x8
_PING: Final[int] = 0x9
_PONG: Final[int] = 0xA


class WebSocketError(Exception):
    """The handshake failed, or the peer sent something this client cannot read."""


def _client_frame(payload: bytes, opcode: int) -> bytes:
    """One masked frame. A client always masks; a server never does."""
    mask = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
    elif size < 1 << 16:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
    return header + mask + bytes(byte ^ mask[at % 4] for at, byte in enumerate(payload))


class Stream(Protocol):
    """The byte transport a framed connection sits on. A TLS socket in a run,
    and a scripted one in a test, which is the only way a frame split across
    two reads can be exercised without a server."""

    def recv(self, size: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...

    def settimeout(self, seconds: float | None, /) -> None: ...

    def close(self) -> None: ...


class Framed(Protocol):
    """What a caller needs of a websocket: whole payloads in, whole payloads
    out. `ConsoleChannel` takes this rather than the class, so its framing can
    be checked without a cluster to connect to."""

    def send(self, payload: bytes, opcode: int = ...) -> None: ...

    def read(self) -> bytes: ...

    def close(self) -> None: ...

    @property
    def closed(self) -> bool: ...


class WebSocket:
    """A framed connection. `read()` returns payload bytes, never frames."""

    def __init__(self, sock: Stream) -> None:
        self._sock = sock
        self._buffer = bytearray()
        self._closed = False
        #: Why it closed, for the reader that reports a dropped console.
        self._why = ""

    @classmethod
    def connect(
        cls,
        host: str,
        path: str,
        headers: dict[str, str],
        *,
        port: int = 443,
        context: ssl.SSLContext | None = None,
        timeout: float = 30.0,
    ) -> Self:
        raw = socket.create_connection((host, port), timeout=timeout)
        secure = (context or ssl.create_default_context()).wrap_socket(
            raw, server_hostname=host
        )
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}",
            "Sec-WebSocket-Version: 13",
            "Sec-WebSocket-Protocol: binary",
            *(f"{name}: {value}" for name, value in headers.items()),
        ]
        secure.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = secure.recv(4096)
            if not chunk:
                secure.close()
                raise WebSocketError(f"the handshake closed early: {head[:200]!r}")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        if "101" not in status:
            secure.close()
            raise WebSocketError(f"the server refused the upgrade: {status}")
        client = cls(secure)
        # A server may put the first payload in the same packet as the
        # handshake, and dropping it loses the console's opening line.
        client._buffer += head.split(b"\r\n\r\n", 1)[1]
        return client

    def settimeout(self, seconds: float | None) -> None:
        self._sock.settimeout(seconds)

    def send(self, payload: bytes, opcode: int = _BINARY) -> None:
        self._sock.sendall(_client_frame(payload, opcode))

    def read(self) -> bytes:
        """Payload from whatever whole frames have arrived, possibly empty.

        Empty is not end of stream: it means no frame completed before the
        socket timed out, which is the ordinary state of an idle console.
        """
        if self._closed:
            return b""
        try:
            chunk = self._sock.recv(65536)
        except (TimeoutError, ssl.SSLWantReadError):
            return self._take()
        except OSError as error:
            # Closed, not raised: a reset is one more way for this connection
            # to end, and the reader above reopens a closed one. Raised, it
            # went past every `except ConsoleClosed` and ended two cluster
            # guests at zero minutes with their installs running.
            self._closed = True
            self._why = f"the connection broke: {error}"
            return self._take()
        if not chunk:
            self._closed = True
            return self._take()
        self._buffer += chunk
        return self._take()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def why_closed(self) -> str:
        return self._why

    def _take(self) -> bytes:
        out = bytearray()
        while True:
            frame = self._one()
            if frame is None:
                return bytes(out)
            opcode, payload = frame
            if opcode in (_TEXT, _BINARY):
                out += payload
            elif opcode == _PING:
                self.send(payload, _PONG)
            elif opcode == _CLOSE:
                self._closed = True
                return bytes(out)

    def _one(self) -> tuple[int, bytes] | None:
        """One frame out of the buffer, or None while it is still short."""
        if len(self._buffer) < 2:
            return None
        opcode = self._buffer[0] & 0x0F
        second = self._buffer[1]
        size = second & 0x7F
        at = 2
        if size == 126:
            if len(self._buffer) < 4:
                return None
            size = struct.unpack("!H", self._buffer[2:4])[0]
            at = 4
        elif size == 127:
            if len(self._buffer) < 10:
                return None
            size = struct.unpack("!Q", self._buffer[2:10])[0]
            at = 10
        mask = b""
        if second & 0x80:
            if len(self._buffer) < at + 4:
                return None
            mask = bytes(self._buffer[at : at + 4])
            at += 4
        if len(self._buffer) < at + size:
            return None
        payload = bytes(self._buffer[at : at + size])
        del self._buffer[: at + size]
        if mask:
            payload = bytes(byte ^ mask[n % 4] for n, byte in enumerate(payload))
        return opcode, payload

    def close(self) -> None:
        if not self._closed:
            try:
                self.send(b"", _CLOSE)
            except OSError:
                pass
            self._closed = True
        self._sock.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
