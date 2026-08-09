"""QEMU's monitor socket, for the console the serial port cannot reach.

GRUB unlocks an encrypted BIOS disk before it reads `grub.cfg`, so its
passphrase prompt is on the VGA console whatever `GRUB_TERMINAL` says. Proved
with a screendump: `Enter passphrase for hd0,msdos2` while the serial log was
empty. `sendkey` is the only way to answer it.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

#: What `sendkey` calls the characters a passphrase is made of. Everything
#: else is the character itself.
NAMED: dict[str, str] = {
    "-": "minus",
    "_": "shift-minus",
    ".": "dot",
    "/": "slash",
    " ": "spc",
    "\n": "ret",
}


class MonitorError(Exception):
    """The monitor socket did not answer."""


def connect(path: Path, timeout: float = 30.0) -> socket.socket:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.connect(str(path))
        except OSError:
            sock.close()
            time.sleep(0.2)
            continue
        sock.settimeout(5.0)
        return sock
    raise MonitorError(f"{path} never accepted a connection")


def keys_for(text: str) -> list[str]:
    """One `sendkey` argument per character.

    An unnamed character above the printable range would be sent as itself and
    silently dropped, so it is refused here where the reason is visible.
    """
    out: list[str] = []
    for char in text:
        named = NAMED.get(char)
        if named is not None:
            out.append(named)
        elif char.isdigit() or (char.isalpha() and char.isascii() and char.islower()):
            out.append(char)
        elif char.isalpha() and char.isascii():
            out.append(f"shift-{char.lower()}")
        else:
            raise MonitorError(f"{char!r} has no sendkey name")
    return out


def type_text(path: Path, text: str, pause: float = 0.05) -> None:
    """Type `text` and press return, through the monitor."""
    sock = connect(path)
    try:
        for key in (*keys_for(text), "ret"):
            sock.sendall(f"sendkey {key}\n".encode())
            time.sleep(pause)
    finally:
        sock.close()
