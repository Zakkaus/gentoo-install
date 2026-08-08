[正體中文](README.md) | [简体中文](README.zh-CN.md) | English

# gentoo-install

An installer that turns a machine into a bootable Gentoo from any Linux live
system. Driven by a menu or a configuration file, with a Chinese environment on
by default and every part of it a switch.

## Requirements

Runs as root, targets amd64. Python 3.11 or newer, standard library only.

It has to reach `packages.gentoo.org` at startup. Kernel versions and the
kernel ceiling of `sys-fs/zfs` are read live, so the installer needs no ebuild
tree on the machine it runs from and works on the live systems of Alpine,
Debian, openSUSE, Fedora and Arch. It stops when it cannot reach the site;
`--missing-commands` and `--config` with `--dry-run` are the two answers it
gives offline.

`bootstrap.sh` reads `/etc/os-release`, lists the commands this layout needs
and the machine lacks, and prints the install line for that distribution. It
supports `apt-get`, `pacman`, `zypper`, `dnf`, `emerge` and `apk`.

## Usage

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

```sh
./bootstrap.sh                                       # the menu
./bootstrap.sh --config my-install.toml              # unattended
./bootstrap.sh --dry-run --config my-install.toml    # print the operations, touch no disk
./bootstrap.sh --config my-install.toml --resume     # carry on from where a run stopped
./bootstrap.sh --config my-install.toml --no-shell   # unmount at the end without asking
```

The menu needs a real terminal, at least 80x24. The interface language is asked
once at the start; `--lang en` skips that question.

When the install finishes, and when it stops partway, it offers a root shell in
the target before unmounting. It offers one after a failure for the same
reason: whether the machine is fixable is the operator's judgement, and once
the target is unmounted the whole layout has to be mounted again by hand.
`--no-shell` turns the question off.

## The configuration file

TOML, with `config_version` on the first line. A disk is a device graph: every
device carries an `id`, devices refer to one another by `id`, and a device path
is resolved when the install runs.

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "zh_TW.UTF-8"
init = "systemd"
root_password_hash = "$6$..."   # from openssl passwd -6; no plaintext here

[portage]
profile = "default/linux/amd64/23.0/systemd"   # has to match init

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

`tests/fixtures/` holds thirteen working examples covering UEFI, BIOS, LUKS2,
LVM, mdraid, ZFS and a desktop. Parsing touches no hardware, so `--dry-run`
checks a file on a machine that has no target disk.

## Layouts

Partition tables GPT and MBR. Filesystems ext2/3/4, btrfs with subvolumes, xfs,
f2fs, vfat, swap and zram. Stacks LUKS2, LVM, mdraid, and ZFS pools and
datasets with native encryption. Bootloaders GRUB and systemd-boot, with
ZFSBootMenu for a ZFS root. Existing partitions can be kept: no partition table
is written, and each partition gets its own mount point and its own answer to
whether it is formatted.

## The Chinese environment

Locale, timezone, keyboard, mirror, font and input method are six separate
options. fcitx5 and rime are installed and configured only when an input method
is chosen; under Wayland `GTK_IM_MODULE` and `QT_IM_MODULE` are left unset,
because setting them makes the candidate window flicker. Each rime schema is
its own group: `luna_pinyin` comes with the engine, and `bopomofo`, `cangjie5`,
`wubi86` and `jyut6ping3` are ticked separately. An overlay is added only when
something selected needs it.

CJK on the console needs `sys-kernel/gentoo-cjk-kernel`, which carries the
cjktty patch and lives in gentoo-zh. The 16x32 console font is a choice only
under that kernel.

## Binary packages

Optional; building from source is the guaranteed path. The official binhost and
gentoo-zh are separate, with separate keys. A failure to fetch a key, verify a
signature or download a package degrades to compiling and prints a warning, and
`install.jsonl` records where each package came from and the reason for every
degradation.

## Exit codes

`0` finished, `1` bad configuration, `2` preflight failed, `3` integrity check
failed, `4` an external command failed, `5` the operator aborted.

## Contributing

```sh
python3 -m mypy
python3 -m pytest
```

A change to partitioning, filesystems, the chroot, the bootloader or binhost
trust also needs one VM run:

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --boot-installed
```

It needs `qemu-system-x86_64`, KVM, OVMF and `xorriso`.
