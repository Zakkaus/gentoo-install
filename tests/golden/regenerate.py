# SPDX-License-Identifier: GPL-2.0-or-later
"""Rewrite every golden plan file. Run it, then read the diff before committing."""

from __future__ import annotations

from gentoo_install.errors import GentooInstallError

from .test_golden import HERE, golden_names, golden_text


def main() -> None:
    """Every fixture, and the failures at the end rather than at the first one.

    `vm-convert` raised here and took the run down with it, so the twelve
    fixtures after it alphabetically kept a golden file nobody had rewritten
    and the suite stayed red on files this command claimed to have written.
    """
    refused: list[str] = []
    for name in golden_names():
        try:
            text = golden_text(name)
        except GentooInstallError as error:
            refused.append(f"{name}: {error}")
            continue
        (HERE / f"{name}.txt").write_text(text)
        print(f"wrote {name}.txt")
    if refused:
        raise SystemExit("no plan for:\n  " + "\n  ".join(refused))


if __name__ == "__main__":
    main()
