# SPDX-License-Identifier: GPL-2.0-or-later
"""Rewrite every golden plan file. Run it, then read the diff before committing."""

from __future__ import annotations

from .test_golden import HERE, fixtures, plan_text


def main() -> None:
    for name in fixtures():
        (HERE / f"{name}.txt").write_text(plan_text(name))
        print(f"wrote {name}.txt")


if __name__ == "__main__":
    main()
