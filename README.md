English | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install runs in a Linux live environment to install an amd64 Gentoo system. An interactive menu or a TOML configuration file specifies the installation. The interface is available in English, Traditional Chinese, Simplified Chinese, Japanese and Korean.

![The menu showing the installation decisions](screenshot-en.png)

## Capabilities at a glance

<!-- fact: capability-summary -->

The implementation covers normal disk installation, storage and boot configuration, desktop and language profiles, and special modes.

- **Storage.** The device graph covers partition tables, filesystems, LUKS2, LVM, mdraid and ZFS.
- **Boot and system.** GRUB, systemd-boot and ZFSBootMenu configure UEFI or BIOS as their configurations allow.
- **Desktop and language.** GNOME, KDE Plasma, Xfce, CJK fonts and input methods are configuration choices.
- **Special modes.** Memory environments, in-place conversion, sparse images and `dd` have separate constraints.

[The reference](REFERENCE.md#capabilities) defines the models, limits and special-mode procedures.

## Verification status

<!-- fact: verification-scope -->

[`TESTED.md`](TESTED.md) records each exercised path, its installer revision and its environment. A run counts only when the recorded revision matches the installer, its installation exits `0`, the installed system boots, and the post-boot configuration checks pass.

Unit, plan and fixture coverage describes implementation behavior; it does not establish that an installed system booted. [`tests/fixtures/`](tests/fixtures/) exercise the configuration model, not an installed machine. The record names unverified combinations.

<!-- fact: verification-architecture -->

`gentoo_install/model/architecture.py` carries rows for amd64, arm64 and x86, and the GRUB target, the `CPU_FLAGS_*` variable, the binary host subdirectory and the EFI executable names are composed from that row. Only amd64 is verified: [`TESTED.md`](TESTED.md) records no arm64 or x86 run, and `tests/vm/` exercises amd64 alone.

## Requirements

<!-- fact: requirements-runtime -->

A real installation requires root privileges, an amd64 target and Python 3.11 or newer. A configuration-file dry run does not require root privileges. The installer has no third-party Python runtime dependency.

## Safety

<!-- fact: safety-destructive -->

A real run writes to the selected disks. A configuration-file run has no second erase confirmation; `wipe = true`, partition deletion and filesystem creation can destroy existing data.

<!-- fact: safety-review-backup -->

Before a real run, the disk selectors and every destructive operation must be checked in the dry-run output. Stable `/dev/disk/by-id/` selectors are preferable to names such as `/dev/sda`, and required data must have a separate backup.

## Installation

<!-- fact: install-download -->

The following commands download the current `master` archive and open the menu:

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

<!-- fact: install-terminal -->

The menu requires an interactive terminal of at least 80x24 cells.

<!-- fact: install-config-workflow -->

The menu saves its answers as `my-install.toml`. The configuration-file workflow is save, dry-run, inspect, then install:

```sh
./bootstrap.sh --config my-install.toml --dry-run
# Inspect the rendered plan before choosing one real command.
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # the same run, without the root-shell prompt
```

<!-- fact: install-root-shell -->

An interactive run offers a target root shell before unmounting. `--no-shell` suppresses that prompt.

## Configuration files

<!-- fact: configuration-reference -->

Configuration files use TOML, and `config_version` selects the schema version. [The configuration reference](REFERENCE.md#configuration-files) lists every persisted key and validated examples. [`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) is a schema reference; its virtual-machine disk selector and test credentials must not be installed unchanged on a real machine.

## Resuming an interrupted run

<!-- fact: resume-limits -->

`--resume` is limited to the same live session, installer revision and configuration file. The installer rejects mismatches. The default journal is `/run/gentoo-install/install.jsonl`, so it does not survive a reboot.

```sh
./bootstrap.sh --config my-install.toml --resume
```

## Binary packages

<!-- fact: binary-packages -->

Binary packages are optional. Disabling them keeps source builds available. The official binhost and the gentoo-zh binhost are separate options with separate trust configuration. No current end-to-end evidence covers an unreachable binhost, a missing signature or an untrusted key; these degradation paths remain unverified.

## Reference

<!-- fact: reference -->

[REFERENCE.md](REFERENCE.md) contains runtime requirements, command-line options, memory environments, in-place conversion, capability and validation detail, configuration files, binary-package trust and exit codes.

## Contributing

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md) describes the development setup, architecture and required checks.

## License

<!-- fact: license -->

gentoo-install is distributed under the GNU General Public License, either version 2 or, at the recipient's option, any later version. The version 2 text is in [LICENSE](LICENSE), and every source file carries `SPDX-License-Identifier: GPL-2.0-or-later`.
