# SPDX-License-Identifier: GPL-2.0-or-later
"""QEMU's monitor socket, for the console the serial port cannot reach.

GRUB unlocks an encrypted BIOS disk before it reads `grub.cfg`, so its
passphrase prompt is on the VGA console whatever `GRUB_TERMINAL` says. Proved
with a screendump: `Enter passphrase for hd0,msdos2` while the serial log was
empty. `sendkey` is the only way to answer it.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: What `sendkey` calls each character that is not a letter or a digit, on the
#: US layout its key names are taken from. The whole printable set, not the
#: subset a passphrase needed: `console=ttyS0,115200` typed at a GRUB prompt
#: needs `equal` and `comma`, and a missing name is refused rather than sent as
#: itself and silently dropped.
NAMED: dict[str, str] = {
    " ": "spc",
    "\n": "ret",
    "\t": "tab",
    "-": "minus",
    "_": "shift-minus",
    "=": "equal",
    "+": "shift-equal",
    "[": "bracket_left",
    "{": "shift-bracket_left",
    "]": "bracket_right",
    "}": "shift-bracket_right",
    "\\": "backslash",
    "|": "shift-backslash",
    ";": "semicolon",
    ":": "shift-semicolon",
    "'": "apostrophe",
    '"': "shift-apostrophe",
    "`": "grave_accent",
    "~": "shift-grave_accent",
    ",": "comma",
    "<": "shift-comma",
    ".": "dot",
    ">": "shift-dot",
    "/": "slash",
    "?": "shift-slash",
    "!": "shift-1",
    "@": "shift-2",
    "#": "shift-3",
    "$": "shift-4",
    "%": "shift-5",
    "^": "shift-6",
    "&": "shift-7",
    "*": "shift-8",
    "(": "shift-9",
    ")": "shift-0",
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


#: How long qemu is given to write the framebuffer out. It is a memory copy
#: and a file write; the wait is for the monitor to get to the command.
DUMP_PATIENCE: Final[float] = 10.0

#: What VGA text mode gives, measured rather than assumed: `screendump` on a
#: guest with `-vga std` and nothing booted wrote `P6\n720 400\n255\n`, which
#: is 80 columns of 9 pixels and 25 rows of 16.
TEXT_CELL_WIDTH: Final[int] = 9
TEXT_CELL_HEIGHT: Final[int] = 16


@dataclass(frozen=True)
class Framebuffer:
    """What the guest is drawing, out of a `screendump` PPM.

    The serial console cannot answer whether a glyph was drawn: it carries the
    bytes the guest wrote, not what the console made of them. A CJK kernel
    that builds the wrong font size prints the same bytes and draws nothing,
    and every check passes.
    """

    width: int
    height: int
    pixels: bytes

    @classmethod
    def read(cls, path: Path) -> Framebuffer:
        raw = path.read_bytes()
        fields: list[bytes] = []
        at = 0
        while len(fields) < 4:
            while at < len(raw) and raw[at : at + 1].isspace():
                at += 1
            start = at
            while at < len(raw) and not raw[at : at + 1].isspace():
                at += 1
            if start == at:
                raise MonitorError(f"{path} ended inside its PPM header")
            fields.append(raw[start:at])
        at += 1
        magic, width, height, depth = fields
        if magic != b"P6" or depth != b"255":
            raise MonitorError(f"{path} is not an 8-bit binary PPM: {magic!r} {depth!r}")
        pixels = raw[at:]
        wanted = int(width) * int(height) * 3
        if len(pixels) != wanted:
            raise MonitorError(
                f"{path} declares {int(width)}x{int(height)} and carries "
                f"{len(pixels)} bytes, not {wanted}"
            )
        return cls(width=int(width), height=int(height), pixels=pixels)

    def inked_columns(self, row: int) -> frozenset[int]:
        """Which pixel columns of this text row are not the background.

        The background is whatever the top-left pixel is, so a theme that
        draws light on dark and one that draws dark on light both answer.
        """
        top = row * TEXT_CELL_HEIGHT
        if top + TEXT_CELL_HEIGHT > self.height:
            raise MonitorError(f"row {row} is past the {self.height}-pixel screen")
        background = self.pixels[0:3]
        found: set[int] = set()
        for line in range(top, top + TEXT_CELL_HEIGHT):
            base = line * self.width * 3
            for column in range(self.width):
                at = base + column * 3
                if self.pixels[at : at + 3] != background:
                    found.add(column)
        return frozenset(found)


def screendump(path: Path, into: Path, patience: float = DUMP_PATIENCE) -> Framebuffer:
    """Ask qemu for the guest's screen and read it back.

    Measured against a live monitor: the command writes a binary PPM and says
    nothing useful on the socket, so the file appearing is the answer.
    """
    into.unlink(missing_ok=True)
    sock = connect(path)
    try:
        sock.sendall(f"screendump {into}\n".encode())
    finally:
        sock.close()
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        # Size as well as existence: qemu creates the file and then writes it,
        # and a header read between the two is a PPM that ends in its header.
        if into.exists() and into.stat().st_size > len(b"P6\n0 0\n255\n"):
            try:
                return Framebuffer.read(into)
            except MonitorError:
                pass
        time.sleep(0.2)
    raise MonitorError(f"qemu wrote no readable screen to {into} in {patience:.0f}s")


def type_text(path: Path, text: str, pause: float = 0.05) -> None:
    """Type `text` and press return, through the monitor."""
    sock = connect(path)
    try:
        for key in (*keys_for(text), "ret"):
            sock.sendall(f"sendkey {key}\n".encode())
            time.sleep(pause)
    finally:
        sock.close()
