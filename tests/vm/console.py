# SPDX-License-Identifier: GPL-2.0-or-later
"""Serial console client: the primary control channel for a test VM."""

from __future__ import annotations

import itertools
import re
import socket
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Final, Iterator, Protocol, Self

_ANSI = re.compile(
    rb"\x1b\[[0-9;?]*[a-zA-Z]"
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b[()][B0]"
    rb"|\x1b[=>]"
)

CONSOLE_BUFFER_BYTES = 4 * 1024 * 1024

_BEGIN_TEXT = "MARK_{token}_BEGIN"
_DONE_TEXT = "MARK_{token}_DONE"


#: SGR colour, which `systemctl` writes because a serial console is a terminal:
#: `enabled` arrives as `\x1b[0;1;32menabled\x1b[0m`, and an anchored pattern
#: cannot reach the end of that line. Removed where the carriage returns are,
#: because the reason is the same one.
ANSI: Final[re.Pattern[bytes]] = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")


def plain(said: bytes) -> bytes:
    """`said` as the program wrote it, without what the terminal added.

    Carriage returns, SGR colour, and the trailing spaces column alignment
    leaves: `systemctl` answers `enabled enabled ` with a space before the
    newline, and a pattern anchored on `$` cannot reach past it either.
    """
    without = ANSI.sub(b"", said.replace(b"\r", b""))
    return b"\n".join(line.rstrip() for line in without.split(b"\n"))


def marked_command(command: str, token: int) -> str:
    """Wrap a command with markers that cannot match its echoed input."""
    return (
        f"printf 'MARK_%s_BEGIN\\n' {token}; {command}; "
        f"printf 'MARK_%s_DONE\\n' {token}"
    )


def command_begin(token: int) -> str:
    """Return the marker printed before a command's output."""
    return _BEGIN_TEXT.format(token=token)


def command_done(token: int) -> str:
    """Return the marker printed after a command's output."""
    return _DONE_TEXT.format(token=token)


class ConsoleTimeout(Exception):
    """A pattern did not appear on the console within its deadline.

    `waited` and `seen` are what a verdict needs and the message cannot carry:
    the message leads with the pattern, which at the encrypted boot is longer
    than any verdict, so a caller that truncates from the front keeps the one
    fact its reader already has and drops the two it does not.
    """

    def __init__(self, message: str, *, waited: float = 0.0, seen: bytes = b"") -> None:
        super().__init__(message)
        self.waited = waited
        self.seen = seen


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


#: How many passphrase prompts are answered before the boot is called stuck.
#: A layout can hold more than one encrypted device and dracut asks once per
#: device; a prompt that keeps returning means the passphrase is wrong, and
#: answering it for ever would hide that behind the patience ceiling. Here
#: rather than in either runner: the cluster bounded it at four and the local
#: runner at five, so a fifth valid prompt passed one and failed the other.
PASSPHRASE_ATTEMPTS: Final[int] = 5

#: How long a name prompt has to answer with a password prompt. Short,
#: because `login` prints one at once when it read the name at all: the
#: whole minute was spent waiting for a prompt agetty had already
#: replaced with a second `login:`.
NAME_PATIENCE: Final[float] = 20.0

#: How long a boot-time passphrase prompt is given to start reading
#: before it is answered. ZFSBootMenu prints its prompt and sets the
#: terminal up after it: locally `vm-zfs-encrypted` failed three times
#: running, with the answer echoed on its own line and never taken,
#: and the same fixture passed on the cluster, where a websocket adds
#: the delay this does. Measured rather than derived — three seconds
#: is what worked, and it costs one wait per run.
PROMPT_SETTLE: Final[float] = 3.0

#: Added to that wait for each prompt already answered without effect. A fixed
#: wait is a guess about a machine whose load is not ours to choose, and the
#: three seconds above were measured on an idle one.
PASSPHRASE_BACKOFF: Final[float] = 2.0


def passphrase_settle(answered: int) -> float:
    """How long to wait before answering a boot passphrase prompt.

    The one rule every passphrase loop reads. There are three of them, on
    three different transports, and the measurement that produced
    `PROMPT_SETTLE` reached only the one that had failed: the cluster's
    `reach_the_login_past_any_passphrase` answered the moment it read the
    prompt and `unlock_and_login` started from zero.
    """
    return PROMPT_SETTLE + PASSPHRASE_BACKOFF * answered

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

    def __init__(
        self,
        sock: Channel,
        log: IO[bytes],
        errors: Path | None = None,
        buffer_limit: int = CONSOLE_BUFFER_BYTES,
    ) -> None:
        if buffer_limit <= 0:
            raise ValueError("console buffer limit must be positive")
        self._sock = sock
        self._log = log
        #: Where qemu wrote its own stderr. It names whatever killed it, and
        #: without reading it a guest earlyoom took looks like an install that
        #: hung: three rounds were diagnosed by inference instead.
        self._errors = errors
        self._buffer = b""
        self._last_chunk = b""
        self._bytes_read = 0
        self._buffer_limit = buffer_limit
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
        started = time.monotonic()
        ceiling = started + timeout
        idle_deadline = started + idle if idle else ceiling
        deadline = min(ceiling, idle_deadline)
        seen = self._bytes_read
        while time.monotonic() < deadline:
            clean = strip_ansi(self._buffer)
            found = matcher.search(clean)
            if found is not None:
                # Keep what arrived after the match: one recv can carry a prompt and the
                # line the next expect() is waiting for.
                self._buffer = clean[found.end() :]
                return clean[: found.end()]
            self._read_once()
            if idle and self._bytes_read != seen:
                seen = self._bytes_read
                idle_deadline = time.monotonic() + idle
                deadline = min(ceiling, idle_deadline)
        silent = bool(idle) and idle_deadline < ceiling
        error = ConsoleIdle if silent else ConsoleTimeout
        # Which bound was reached, before the screen rather than after it: a
        # verdict is truncated to a few hundred bytes, and `vm-unlock` reported
        # only the pattern and a gcc line, so whether it went quiet or ran out
        # of ceiling could not be told from the verdict at all.
        why = (
            f"nothing arrived for {idle:.0f}s"
            if silent
            else f"{time.monotonic() - started:.0f}s of {timeout:.0f}s elapsed"
        )
        raise error(
            f"never matched {pattern!r}, {why}; "
            f"last output was {strip_ansi(self._buffer)[-600:]!r}",
            waited=time.monotonic() - started,
            seen=strip_ansi(self._buffer),
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
        self.send(marked_command(command, token))
        self.expect(command_done(token), timeout)

    def expect_command(self, command: str, timeout: float = 120.0) -> bytes:
        """Run a command and answer with what it printed.

        `run` waits for the marker and discards the reply, which is enough for
        a step whose only question is whether it finished. A check has to read
        what the machine said.
        """
        token = next(self._tokens)
        self.send(marked_command(command, token))
        said = self.expect(command_done(token), timeout)
        return said.split(command_done(token).encode())[0]

    def expect_output(self, command: str, timeout: float = 120.0) -> bytes:
        """Run a command and answer with what it printed, and nothing else.

        Between the two markers, not up to the last one: the shell echoes the
        line it was given, so what `expect_command` answers begins with the
        command itself. A check whose command names its own answer — `echo
        RESOLVCONF-OK || echo RESOLVCONF-EMPTY` — then matches the question.
        """
        token = next(self._tokens)
        self.send(marked_command(command, token))
        self.expect(command_begin(token), timeout)
        said = self.expect(command_done(token), timeout)
        # Without the carriage returns, for the reason the cluster's own reader
        # gives: a serial line ends every one of them `\r\n`, and `convert.py`
        # applies the same `installed.py` patterns, four of which anchor on `$`.
        return plain(said.split(command_done(token).encode())[0])

    def login(self, user: str, password: str | None, prompt: str) -> None:
        """Log in, answering a name prompt that comes back.

        The name prompt turns the echo off with `TCSAFLUSH`, which discards
        whatever was typed before it: a name sent into that window never
        reaches `login`, agetty prints a fresh `login:` and no `Password:`
        ever arrives. `vm-zfs-encrypted` failed on an install that had
        finished, with two `cryptzfs login:` banners two seconds apart on its
        console and a machine that was otherwise fine. `cluster.py` has
        carried the same handling since `ext3` lost 33.6 minutes to it.
        """
        self.expect(r"login:", timeout=300.0)
        self.answer_login(user, password, prompt)

    def answer_login(self, user: str, password: str | None, prompt: str) -> None:
        """The same, for a caller that has already read the `login:`.

        Split out because `run.py` waits for the name prompt beside a
        passphrase one and cannot wait for it twice. Four call sites did this
        sequence inline; the fix for the reprinted prompt reached one of them
        and `vm-zfs-encrypted` failed exactly as before.
        """
        for attempt in range(PASSPHRASE_ATTEMPTS):
            self.send(user)
            if password is None:
                break
            try:
                self.expect(PASSWORD_PROMPT, timeout=NAME_PATIENCE)
                break
            except ConsoleTimeout:
                if attempt + 1 == PASSPHRASE_ATTEMPTS:
                    raise
                # The fresh prompt agetty printed, which is already in the
                # buffer: waiting for a new one would wait for a third.
                self.expect(r"login:", timeout=NAME_PATIENCE)
        if password is not None:
            self.send(password)
        self.expect(prompt, timeout=60.0)

    def _read_once(self) -> None:
        self._last_chunk = b""
        chunk = self._sock.recv(4096)
        if not chunk:
            # Empty means nothing arrived before the read timed out, which is
            # what an idle console looks like. Only the transport knows the
            # difference between idle and hung up.
            if self._sock.closed:
                raise ConsoleClosed(self._why_closed())
            return
        self._bytes_read += len(chunk)
        self._log.write(chunk)
        self._log.flush()
        self._last_chunk = chunk
        self._buffer = (self._buffer + chunk)[-self._buffer_limit :]

    def set_buffer_limit(self, limit: int) -> None:
        """Set the retained tail size for the next reads."""
        if limit <= 0:
            raise ValueError("console buffer limit must be positive")
        self._buffer_limit = limit
        self._buffer = self._buffer[-limit:]

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
            try:
                self._read_once()
                got += self._last_chunk
            except ConsoleClosed:
                break
        self._buffer = b""
        return bytes(got)

    def read_available(self, seconds: float) -> bytes:
        """What arrived, escapes and all.

        `drain` discards and `expect` strips the escapes to match a pattern.
        Rebuilding the screen needs them: the cursor moves are how `ncurses`
        says which cell it is overwriting, and text with them removed is every
        draft of the menu run together.
        """
        got = bytearray()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self._read_once()
            except ConsoleClosed:
                break
            got += self._last_chunk
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
