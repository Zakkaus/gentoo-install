# Credits

gentoo-install is licensed under GPL-2 or, at the recipient's option, any later
version. This file records every place its code came from something else, so
that a reader can find the original and its terms.

## Derived code

A derivation is recorded here in the same commit that introduces it, one row
per file, naming the source project, the file it came from and what was taken.
The file itself carries the same statement in a comment at the top, because a
reader of that file cannot be assumed to have read this one.

None of it is installed: the ebuild installs `gentoo_install/` and the one
derivation below is under `tests/`, which no package carries.

| File here | Source project | Source file | Licence | Taken |
|---|---|---|---|---|
| `tests/vm/cluster.py` | [shadow](https://github.com/shadow-maint/shadow) | `po/zh_TW.po`, `po/zh_CN.po` | BSD GPL-2 | The wordings `login` prints when it refuses a password, so a harness driving a Chinese console recognises a refusal instead of waiting out its budget |

## Projects read for behaviour

These were read to learn what a working installer does and where it fails. No
code was taken from them; they are listed because the knowledge came from
somewhere and the reader deserves to know where.

| Project | Licence | What it taught |
|---|---|---|
| [distro2gentoo](https://gitlab.com/cwittlut/distro2gentoo) | GPL-2 | How an in-place conversion replaces a running userland |
| [oddlama/gentoo-install](https://github.com/oddlama/gentoo-install) | MIT | The real install steps and the pitfalls in their order |
| [gentoo-install-zh](https://github.com/zozx/gentoo-install-zh) | MIT | Chinese terminology |
| [archinstall](https://github.com/archlinux/archinstall) | GPL-3 | Menu structure and configuration schema layering |
| [catalyst](https://github.com/gentoo/catalyst) | GPL-2 | How a Gentoo installation medium is built |
| [releng](https://gitweb.gentoo.org/proj/releng.git/) | — | What the official install CD contains |
| [bin456789/reinstall](https://github.com/bin456789/reinstall) | MIT | What a cloud image's GRUB needs before it will take an added entry |

`archinstall` is GPL-3. The `or later` clause makes taking code from it lawful,
and a release that carries any of it is distributed under GPL-3 rather than
GPL-2. A derivation from it says so in this file, because the terms the whole
release goes out under change with it.
