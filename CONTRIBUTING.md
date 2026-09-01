# Contributing

gentoo-install is written from scratch. Reference installers are studied for behavior and failure modes, and code is taken from one only under [Derived code](#derived-code): a compatible licence, and the credit that licence asks for.

## Architecture

The source has three layers, and calls only move downward:

- `model/` holds typed configuration data and validation without I/O.
- `plan/` derives the ordered operation sequence as pure functions.
- `exec/` touches disks, runs external commands and writes files.

`plan.build()` returns one operation list. `render()` prints each operation's `describe()` result, while `exec.apply()` calls `apply()` on the same objects. A feature is incomplete until both dry-run and real execution use that plan.

Every external command goes through `gentoo_install/exec/runner.py`. It is the only module under `gentoo_install/` that imports `subprocess`.

The runner merges stderr into stdout, so code that reads command output checks the exit code before parsing it.

`gentoo_install/model/compat.py` is the compatibility table used by validation and the menu. Every rule requires a configuration that breaks it and a test proving the case set covers the table.

## Development checks

Run both checks before every commit:

```sh
python3 -m mypy
python3 -m pytest
```

mypy runs in strict mode. Do not hide a finding with `# type: ignore` or `# noqa`, and do not weaken an assertion to make a test pass.

Run `pytest` with no path. `tests/unit` does not reach `tests/golden`, so a changed `describe()` left seven golden files stale on `master` for four merges before the whole suite was run again.

Delete `__pycache__` and `.mypy_cache` before a run that decides anything. Both gates have reported success against an earlier analysis of code that had since been broken.

A green mypy here is evidence, not the verdict. CI pins Python 3.13, and a newer interpreter narrows some unions the older one does not, so a clean local run has still failed CI in under a minute.

One check skips itself rather than failing when it cannot run.
`test_every_package_a_group_names_exists_where_it_says` reads
`/var/db/repos/gentoo` and `/var/db/repos/gentoo-zh` to confirm that every atom
a package group names exists in the repository it claims. Those trees are not
on the CI runner, so a package group naming a misspelled atom passes CI and
fails an hour into an installation. Run the suite on a machine that has both
trees synced before adding or editing anything under
`gentoo_install/data/packages/`, and say in the pull request that you did.

Changes to partitioning, filesystems, chroot handling, bootloaders or binhost trust also require an installation in a virtual machine. For example:

VM runs require `qemu-system-x86_64`, KVM, OVMF and `xorriso`.

```sh
python3 -m tests.vm.run \
  --medium official-minimal \
  --firmware uefi \
  --install fixtures/vm-binpkg.toml \
  --and-boot
```

The VM run must install the system, remove the installation medium, boot the installed disk and complete the post-boot checks. A successful installer exit alone does not establish that the system boots.

Campaigns run from a pinned worktree. The driver image is built from the selected worktree when each run starts, so a campaign performed against a changing branch does not provide one reproducible result.

```sh
python3 -m tests.vm.campaign --stage blocking
```

## Code and text

Python code is fully annotated and uses only the standard library at run time. Data belongs in dataclasses and enums, and external state belongs in the `exec/` layer.

Source code, comments, commit messages and this guide are English. The five README files and the interface catalogs are the only localized files in the repository. All five README files carry the same content and language switcher and change together.

Comments explain only a non-obvious invariant, constraint, trade-off or workaround. Identifiers, commands, paths and configuration keys use their exact names.

## Commits

Each commit contains one logical change and all code, tests, generated files and documentation needed to make that change true. The subject uses `module: summary`, is imperative and is at most 69 characters.

A body is added only when the subject cannot carry the reason. It states the cause and effect without narrating the diff or listing routine checks.

## Derived code

Code may be taken from a project whose licence is compatible with GPL-2 or
later: `distro2gentoo` and `catalyst` are GPL-2, `oddlama/gentoo-install` and
`gentoo-install-zh` are MIT, and `archinstall` is GPL-3. Taking from a GPL-3
project is lawful under the `or later` clause and changes what the release goes
out under: a release carrying any GPL-3 code is distributed under GPL-3.

A derivation is recorded in three places in the commit that introduces it: a
comment at the top of the file naming the source project, file and licence and
saying that it was modified, a line in [CREDITS.md](CREDITS.md), and the reason
in the commit body. Derived code still has to satisfy everything else here: the
three layers, the annotations, the named exceptions and a test that fails
without it.
