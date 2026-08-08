[正體中文](README.md) | [简体中文](README.zh-CN.md) | English

# gentoo-install

Installs a bootable Gentoo from any Linux live system. Driven by a menu or a
configuration file, with a Chinese environment on by default and every part of
it a switch.

## Requirements

Runs as root. Python 3.11 or newer, standard library only. Target is amd64.

These live media were each tested. `bootstrap.sh` lists the missing commands and
the install line for that distribution:

| Medium | python3 | Install first |
|---|---|---|
| Gentoo minimal 20260712 | 3.14.6 | nothing |
| Arch 2026.08.01 | 3.14.6 | nothing |
| openSUSE Tumbleweed Rescue | 3.13.14 | nothing |
| Fedora Workstation Live 43 | 3.14.0 | `gptfdisk` |
| Debian live 13.6 | 3.13.5 | `dosfstools`, `gdisk` |
| Alpine 3.24.1 | none | `python3` and more |

## Usage

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

`bootstrap.sh` is the only entry point.

```sh
./bootstrap.sh                                       # menu
./bootstrap.sh --config my-install.toml              # unattended
./bootstrap.sh --dry-run --config my-install.toml    # print the operations only
./bootstrap.sh --config my-install.toml --resume     # carry on from where it stopped
```

The menu needs a real terminal. It asks for its language once; `--lang en` skips
that.

## The configuration file

TOML, declaring `config_version`. The disk is a device graph: every device has
an `id`, devices refer to each other by `id`, and paths are resolved at run time.

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "en_US.UTF-8"
init = "systemd"
root_password_hash = "$6$..."   # from openssl passwd -6, never a plaintext

[portage]
profile = "default/linux/amd64/23.0/systemd"   # has to match the init

[bootloader]
kind = "grub"
firmware = "uefi"

[disk]
root = "mnt-root"

[[disk.devices]]
kind = "existing"
id = "disk"
selector = "/dev/disk/by-id/virtio-target0"
wipe = true
```

Examples are in `tests/fixtures/`. Parsing touches no hardware, so `--dry-run`
validates a file on a machine without the target disk.

## Tested

Each of these was installed, shut down, and booted again with the medium
removed. The check covers mounts, fstab, locale, enabled services and failed
units.

- UEFI with `gentoo-kernel-bin`
- A kernel built from a sources package
- ZFS with ZFSBootMenu, plain and with native encryption
- BIOS with MBR and openrc
- systemd-boot
- LUKS2 under btrfs subvolumes
- LVM
- mdraid RAID1
- KDE Plasma with the Chinese environment

## The Chinese environment

Locale, timezone, keyboard, mirrors, fonts and the input method are separate
choices. Selecting an input method is what installs fcitx5 and rime and writes
their configuration; under Wayland `GTK_IM_MODULE` and `QT_IM_MODULE` are left
unset, because setting them makes the candidate window blink. An overlay is
added only when it is selected.

## Binary packages

Optional; compiling is the guaranteed path. The official and gentoo-zh hosts are
separate, with separate keys. Any failure degrades to compiling with a warning,
and `install.jsonl` records where each package came from and why anything
degraded.

## Exit codes

`0` finished, `1` configuration error, `2` preflight failed, `3` integrity check
failed, `4` an external command failed, `5` aborted.

## Contributing

```sh
python3 -m mypy
python3 -m pytest
```

A change touching partitioning, filesystems, chroot, the bootloader or binhost
trust also needs one VM run:

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --boot-installed
```

Needs `qemu-system-x86_64`, KVM, OVMF and `xorriso`.
