"""Serial console client: the primary control channel for a test VM."""

from __future__ import annotations

import itertools
import re
import socket
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Iterator, Self

_ANSI = re.compile(
    rb"\x1b\[[0-9;?]*[a-zA-Z]"
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b[()][B0]"
    rb"|\x1b[=>]"
)


class ConsoleTimeout(Exception):
    """A pattern did not appear on the console within its deadline."""


class ConsoleClosed(Exception):
    """The guest closed the serial connection."""


def strip_ansi(data: bytes) -> bytes:
    return _ANSI.sub(b"", data)


#: A system installed with a Chinese locale asks in Chinese, so the pattern
#: carries the localized forms. This is the one CJK literal the tests allow.
PASSWORD_PROMPT = r"[Pp]assword:|密码：|密碼："


class SerialConsole:
    """Reads and writes a QEMU unix-socket serial port, with expect semantics."""

    def __init__(self, sock: socket.socket, log: IO[bytes], errors: Path | None = None) -> None:
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
            return cls(sock, log_path.open("wb"), path.parent / "qemu.err")
        raise ConsoleTimeout(f"{path} never accepted a connection")

    def expect(self, pattern: str, timeout: float) -> bytes:
        matcher = re.compile(pattern.encode())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            clean = strip_ansi(self._buffer)
            found = matcher.search(clean)
            if found is not None:
                # Keep what arrived after the match: one recv can carry a prompt and the
                # line the next expect() is waiting for.
                self._buffer = clean[found.end() :]
                return clean[: found.end()]
            self._read_once()
        raise ConsoleTimeout(
            f"never matched {pattern!r}; last output was {strip_ansi(self._buffer)[-600:]!r}"
        )

    def send(self, line: str) -> None:
        self._sock.sendall(line.encode() + b"\n")

    def run(self, command: str, timeout: float = 120.0) -> None:
        """Run a shell command and wait for it to finish.

        The guest echoes the command line back on the same console, so a plain marker
        would match its own echo. Arithmetic expansion keeps the echoed text different
        from the expanded marker.
        """
        token = next(self._tokens)
        self.send(f"{command}; echo MARK_$(({token}+0))_END")
        self.expect(rf"MARK_{token}_END", timeout)

    def login(self, user: str, password: str | None, prompt: str) -> None:
        self.expect(r"login:", timeout=300.0)
        self.send(user)
        if password is not None:
            self.expect(PASSWORD_PROMPT, timeout=60.0)
            self.send(password)
        self.expect(prompt, timeout=60.0)

    def _read_once(self) -> None:
        try:
            chunk = self._sock.recv(4096)
        except TimeoutError:
            return
        if not chunk:
            raise ConsoleClosed(self._why_closed())
        self._log.write(chunk)
        self._log.flush()
        self._buffer += chunk

    def _why_closed(self) -> str:
        said = ""
        if self._errors is not None and self._errors.exists():
            lines = self._errors.read_text(errors="replace").strip().splitlines()
            said = lines[-1] if lines else ""
        closed = "the guest closed the serial connection"
        return f"{closed}: {said}" if said else closed

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
