# SPDX-License-Identifier: GPL-2.0-or-later
"""Keeping the kernel from writing over the interface.

On a serial console the kernel and the menu share one screen. A guest running
the installer drew `[ 3915.800938] clocksource: Watchdog remote CPU 1 read ti`
across the middle of a panel: the text is unreadable and an operator cannot
tell a corrupted screen from a broken installer.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Final, Iterator

#: `console_loglevel current default minimum boot`. Only the first decides
#: what reaches the console; the file takes one number and changes that one.
PRINTK: Final[Path] = Path("/proc/sys/kernel/printk")

#: `KERN_ALERT` and above, which is what a machine about to stop says. Not 0:
#: an operator whose disk is failing under the menu should still see it.
WHILE_DRAWING: Final[str] = "1"


@contextmanager
def kernel_messages_held(printk: Path = PRINTK) -> Iterator[None]:
    """Lower the console log level for the duration, then put it back.

    Silent when the file is absent or unwritable -- a menu that refuses to
    open because it could not quieten the kernel is worse than a noisy one --
    and the level is restored even when the walk raises.
    """
    try:
        before = printk.read_text(encoding="utf-8").split()[0]
        printk.write_text(f"{WHILE_DRAWING}\n", encoding="utf-8")
    except (OSError, IndexError):
        yield
        return
    try:
        yield
    finally:
        try:
            printk.write_text(f"{before}\n", encoding="utf-8")
        except OSError:
            pass
