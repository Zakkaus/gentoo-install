"""Serial console client: the primary control channel for a test VM."""

from __future__ import annotations

import itertools
import re
import socket
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Iterator, Protocol, Self

_ANSI = re.compile(
    rb"\x1b\[[0-9;?]*[a-zA-Z]"
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b[()][B0]"
    rb"|\x1b[=>]"
)


class ConsoleTimeout(Exception):
    """A pattern did not appear on the console within its deadline."""


class ConsoleIdle(ConsoleTimeout):
    """A pattern did not appear before the console stopped speaking."""


class ConsoleClosed(Exception):
    """The guest closed the serial connection.

    `write_may_have_reached_guest` is false only when the channel was already
    known closed. Once a transport write starts, its failure cannot prove how
    many bytes reached the peer.
    """

    def __init__(
        self, reason: str, *, write_may_have_reached_guest: bool | None = None
    ) -> None:
        super().__init__(reason)
        self.write_may_have_reached_guest = write_may_have_reached_guest


def strip_ansi(data: bytes) -> bytes:
    return _ANSI.sub(b"", data)


#: A system installed with a Chinese locale asks in Chinese, so the pattern
#: carries the localized forms. This is the one CJK literal the tests allow.
PASSWORD_PROMPT = r"[Pp]assword:|密码：|密碼："

#: What GRUB and the initramfs say when they want a disk passphrase. GRUB asks
#: because `/boot` is inside the container and the initramfs asks to open the
#: root, so one passphrase is prompted for twice unless a keyfile is embedded.
PASSPHRASE_PROMPT = r"[Ee]nter passphrase|Please enter passphrase|password for"

#: The passphrase every encrypted fixture installs. Not the root password: zfs
#: takes at least eight characters, and a real install does not reuse one for
#: the other. Here rather than in each runner, so the local runs and the
#: cluster runs cannot install one and answer with another.
DISK_PASSPHRASE = "install-disk"


class Channel(Protocol):
    """What a console needs of its transport, and nothing more.

    A local run reads a unix socket; a run on the cluster reads a websocket,
    because the node's serial socket is a file on the node and port 22 is
    closed there. The expect logic is the same either way, so the transport is
    the only thing that differs.
    """

    def recv(self, size: int) -> bytes:
        """Whatever has arrived, or empty on a timeout. Empty is not the end;
        `closed` is."""

    def sendall(self, data: bytes) -> None: ...

    def close(self) -> None: ...

    @property
    def closed(self) -> bool:
        """Whether the far end hung up. A transport that cannot tell says no."""


class _Socket:
    """A unix socket as a `Channel`. `recv` on a timeout is empty, not an error."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def recv(self, size: int) -> bytes:
        try:
            chunk = self._sock.recv(size)
        except TimeoutError:
            return b""
        if not chunk:
            self._closed = True
        return chunk

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._sock.close()

    _closed = False

    @property
    def closed(self) -> bool:
        return self._closed


class SerialConsole:
    """Reads and writes a serial port, with expect semantics."""

    def __init__(self, sock: Channel, log: IO[bytes], errors: Path | None = None) -> None:
        self._sock = sock
        self._log = log
        #: Where qemu wrote its own stderr. It names whatever killed it, and
        #: without reading it a guest earlyoom took looks like an install that
        #: hung: three rounds were diagnosed by inference instead.
        self._errors = errors
        self._buffer = b""
        self._tokens: Iterator[int] = itertools.count(1)

    @classmethod
    def connect(cls, path: Path, log_path: Path, timeout: float = 30.0) -> Self:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.connect(str(path))
            except OSError:
                sock.close()
                time.sleep(0.2)
                continue
            sock.settimeout(1.0)
            return cls(_Socket(sock), log_path.open("wb"), path.parent / "qemu.err")
        raise ConsoleTimeout(f"{path} never accepted a connection")

    def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
        """Wait for `pattern`, giving up after `timeout`.

        `idle` measures the wait from the last byte that arrived rather than
        from the start. An install that prints for three hours is working; one
        that prints nothing for twenty minutes is not, and a single ceiling
        cannot tell them apart. Twelve guests of one round were ended at three
        hours with a repository listing still scrolling past.
        """
        matcher = re.compile(pattern.encode())
        # Two deadlines, whichever comes first: the ceiling bounds a guest that
        # prints for ever, and the idle window ends one that stopped.
        ceiling = time.monotonic() + timeout
        idle_deadline = time.monotonic() + idle if idle else ceiling
        deadline = min(ceiling, idle_deadline)
        seen = len(self._buffer)
        while time.monotonic() < deadline:
            clean = strip_ansi(self._buffer)
            found = matcher.search(clean)
            if found is not None:
                # Keep what arrived after the match: one recv can carry a prompt and the
                # line the next expect() is waiting for.
                self._buffer = clean[found.end() :]
                return clean[: found.end()]
            self._read_once()
            if idle and len(self._buffer) != seen:
                seen = len(self._buffer)
                idle_deadline = time.monotonic() + idle
                deadline = min(ceiling, idle_deadline)
        error = ConsoleIdle if idle and idle_deadline < ceiling else ConsoleTimeout
        raise error(
            f"never matched {pattern!r}; "
            f"last output was {strip_ansi(self._buffer)[-600:]!r}"
        )

    def send(self, line: str) -> None:
        self._write(line.encode() + b"\n")

    def send_raw(self, keys: str) -> None:
        """Exactly these bytes, with no newline. GRUB's editor acts on the
        keystroke itself, and a trailing newline there is an extra line."""
        self._write(keys.encode())

    def _write(self, data: bytes) -> None:
        if self._sock.closed:
            raise ConsoleClosed(
                self._why_closed(), write_may_have_reached_guest=False
            )
        try:
            self._sock.sendall(data)
        except OSError as error:
            reason = self._why_closed()
            detail = str(error)
            if detail and detail not in reason:
                reason = f"{reason}: {detail}"
            # sendall does not report how many bytes preceded its error.
            raise ConsoleClosed(
                reason, write_may_have_reached_guest=True
            ) from error
        if self._sock.closed:
            # A websocket records its socket error and returns. Its frame may
            # have reached the peer in whole or in part before that happened.
            raise ConsoleClosed(
                self._why_closed(), write_may_have_reached_guest=True
            )

    def run(self, command: str, timeout: float = 120.0) -> None:
        """Run a shell command and wait for it to finish.

        The guest echoes the command line back on the same console, so a plain marker
        would match its own echo. Arithmetic expansion keeps the echoed text different
        from the expanded marker.
        """
        token = next(self._tokens)
        self.send(f"{command}; echo MARK_$(({token}+0))_END")
        self.expect(rf"MARK_{token}_END", timeout)

    def expect_command(self, command: str, timeout: float = 120.0) -> bytes:
        """Run a command and answer with what it printed.

        `run` waits for the marker and discards the reply, which is enough for
        a step whose only question is whether it finished. A check has to read
        what the machine said.
        """
        token = next(self._tokens)
        self.send(f"{command}; echo MARK_$(({token}+0))_END")
        return self.expect(rf"MARK_{token}_END", timeout)

    def login(self, user: str, password: str | None, prompt: str) -> None:
        self.expect(r"login:", timeout=300.0)
        self.send(user)
        if password is not None:
            self.expect(PASSWORD_PROMPT, timeout=60.0)
            self.send(password)
        self.expect(prompt, timeout=60.0)

    def _read_once(self) -> None:
        chunk = self._sock.recv(4096)
        if not chunk:
            # Empty means nothing arrived before the read timed out, which is
            # what an idle console looks like. Only the transport knows the
            # difference between idle and hung up.
            if self._sock.closed:
                raise ConsoleClosed(self._why_closed())
            return
        self._log.write(chunk)
        self._log.flush()
        self._buffer += chunk

    def _why_closed(self) -> str:
        transport_reason = getattr(self._sock, "why_closed", "")
        if transport_reason:
            return str(transport_reason)
        said = ""
        if self._errors is not None and self._errors.exists():
            lines = self._errors.read_text(errors="replace").strip().splitlines()
            said = lines[-1] if lines else ""
        closed = "the guest closed the serial connection"
        return f"{closed}: {said}" if said else closed

    @property
    def closed(self) -> bool:
        """Whether the transport under this console has hung up.

        A caller can ask before a write and reopen proactively. `send` still
        checks around the write, because a connection can drop between them.
        """
        return self._sock.closed

    def snapshot(self, seconds: float) -> bytes:
        """Everything that arrives in this window, escape codes included.

        `expect` trims the buffer to what follows its match, which loses the
        top of a screen the caller still has to read. A read can end between
        bytes of an escape sequence, so callers must retain terminal state
        across snapshots rather than replaying each window independently.
        """
        got = bytearray(self._buffer)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            before = len(self._buffer)
            try:
                self._read_once()
            except ConsoleClosed:
                break
            got += self._buffer[before:]
        self._buffer = b""
        return bytes(got)

    def drain(self, seconds: float) -> None:
        """Read and discard for a while.

        A guest whose console buffer fills stops writing, and a systemd
        shutdown that cannot write stops shutting down. Nothing is looking for
        a pattern here; the point is that somebody is reading.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self._read_once()
            except ConsoleClosed:
                return
        self._buffer = b""

    def close(self) -> None:
        self._sock.close()
        self._log.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
