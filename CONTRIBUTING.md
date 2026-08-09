# Contributing

## Gates

```sh
python3 -m mypy
python3 -m pytest
```

Both pass before every commit. mypy runs in strict mode, and `# type: ignore` is not an answer to a finding.

## Architecture

Three layers, calling downward only.

- `model/` holds the data and its validation, and performs no I/O.
- `plan/` derives the operation sequence as pure functions.
- `exec/` is the only layer that touches disks, runs commands or writes files.

`plan.build()` returns one operation list. `render()` prints each operation's `describe()` and `exec.apply()` calls each operation's `apply()`, both over that same list, so a dry run cannot describe something a real run would not do.

Every external command goes through `exec/runner.py`, which is the only module under `gentoo_install/` that imports `subprocess`. It merges stderr into stdout, so anything reading a command's output checks the exit code first.

`model/compat.py` is the only compatibility table. Both `validate.py` and the menu read it. Every entry in it carries a test holding a configuration that breaks it.

## Virtual machine runs

A change to partitioning, filesystems, chroot, bootloaders or binhost trust is verified by a real installation, not by unit tests.

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --and-boot
python3 -m tests.vm.campaign --stage blocking
```

`qemu-system-x86_64`, KVM, OVMF and `xorriso` are required. Each run boots an install medium, performs the installation, then boots what was installed and logs in: an installer that exits 0 has not proved the machine starts.

Run a campaign from a pinned worktree rather than the branch being edited. The driver CD is built from the working tree at the moment each run starts, so committing while a campaign runs leaves every result naming a different snapshot.

## Text

Everything committed is English, apart from the README set and the interface catalogs under `gentoo_install/data/locale/`.

Commit subjects are one line of at most 69 characters, `module: summary`, imperative. A body is added only when the subject cannot carry the reason, and carries the reason rather than a description of the diff.
