# Credits

gentoo-install is licensed under GPL-2. This file records every place its code
came from something else, so that a reader can find the original and its terms.

## Derived code

Nothing yet. Every line in this repository was written for it.

A derivation is recorded here in the same commit that introduces it, one row
per file, naming the source project, the file it came from and what was taken.
The file itself carries the same statement in a comment at the top, because a
reader of that file cannot be assumed to have read this one.

| File here | Source project | Source file | Licence | Taken |
|---|---|---|---|---|

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

`archinstall` is GPL-3. Nothing may be copied from it while this repository is
GPL-2 without an `or later` clause.
