[正體中文](README.md) | [简体中文](README.zh-CN.md) | English

# gentoo-install

A text installer that turns a machine into a bootable Gentoo system from any Linux live medium. It is driven by a menu or a configuration file, and a Chinese environment works out of the box with every part of it a switch you can turn off. It is the text counterpart of the Calamares installer on the Gig-OS Live ISO: no desktop, no mouse, and usable over a serial console or SSH.

## Supported environments

The installer runs on a live system and installs Gentoo onto a target disk. These six live media were each booted and tested:

| Live system | Version | python3 | Has to be installed first |
|---|---|---|---|
| Gentoo minimal | 20260712 | 3.14.6 | nothing |
| Arch | 2026.08.01 | 3.14.6 | nothing |
| openSUSE Tumbleweed Rescue | current | 3.13.14 | nothing |
| Fedora Workstation Live | 43 | 3.14.0 | `gptfdisk` |
| Debian live standard | 13.6 | 3.13.5 | `dosfstools`, `gdisk` |
| Alpine standard | 3.24.1 | none | `python3` and more |

Python 3.11 is the floor, set by `tomllib` in the standard library. The installer uses the standard library only and pulls in no third-party dependency. The target architecture is amd64.

The launcher works out which commands are missing and prints the install line for that distribution, so you do not have to map them yourself.

## Usage

The installer has to run as root. `bootstrap.sh` is the only entry point: it checks the Python version, lists the missing commands, then runs the installer.

Read what the run would do, without touching a disk:

```sh
./bootstrap.sh --dry-run --config tests/fixtures/vm-binpkg.toml
```

Install through the menu:

```sh
./bootstrap.sh
```

Install unattended from a configuration file:

```sh
./bootstrap.sh --config my-install.toml
```

The menu needs a real terminal; run into a pipe it reports so and points at `--config`. The interface language comes from `LC_ALL`, `LC_MESSAGES` and `LANG` in that order, and `--lang en` overrides it.

## The configuration file

The file is TOML and has to declare `config_version`. The disk section is a device graph: every device carries an `id`, devices refer to each other by `id`, and paths are resolved at run time, so a run that stops halfway resumes against the same devices.

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "en_US.UTF-8"
init = "systemd"
# A crypt(3) hash, not a plaintext password. Produce one with openssl passwd -6.
root_password_hash = "$6$..."

[portage]
# The profile has to match the init system or validation refuses it.
profile = "default/linux/amd64/23.0/systemd"

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

Complete examples are in `tests/fixtures/`, one per tested path.

Parsing a configuration touches no hardware, so `--dry-run` validates a file on a machine that does not have the target disk.

## Tested install paths

Each of these ten was installed, shut down, and booted again with the install medium removed. The check covers mounts, fstab, locale, enabled services and whether any unit failed.

- UEFI with `gentoo-kernel-bin`
- A kernel built from a sources package
- ZFS root with ZFSBootMenu, both plain and with native encryption
- BIOS with MBR and openrc
- systemd-boot
- LUKS2 under btrfs subvolumes
- LVM
- mdraid RAID1
- A KDE Plasma desktop with the Chinese environment

## The Chinese environment

Locale, timezone, keyboard, mirrors, fonts and the input method are separate options rather than one bundle. Selecting an input method is what installs fcitx5 and rime and writes the configuration into `/etc/skel` and each user's home directory. Under a Wayland session `GTK_IM_MODULE` and `QT_IM_MODULE` are left unset, because the compositor drives fcitx over the text-input protocol and setting them makes the candidate window blink.

An overlay is added only when it is selected. `gentoo-zh` and `gig` are independent choices, and selecting one also configures its keys and its `package.accept_keywords`.

## Binary packages

`binpkg` is an option and compiling from source is always the guaranteed path. The official and community binary hosts are separate switches with separate keys. Any failure — an unreachable host, a missing signature, an untrusted key — degrades to compiling with a warning, and `install.jsonl` records where each package came from and the reason for every degradation.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | finished |
| 1 | configuration error: parsing, validation, incompatible version |
| 2 | a preflight check failed |
| 3 | integrity check failed: GPG, checksum, fingerprint |
| 4 | an external command failed, or a download did not complete |
| 5 | the operator aborted |

## Contributing

```sh
python3 -m mypy
python3 -m pytest
```

Both have to pass. A change touching partitioning, filesystems, chroot, the bootloader or binhost trust also needs one `tests/vm/run.py` run behind it:

```sh
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml
python3 -m tests.vm.run --medium official-minimal --firmware uefi --install fixtures/vm-binpkg.toml --boot-installed
```

The VM tests need `qemu-system-x86_64`, KVM, OVMF and `xorriso`. ISOs are cached under `lab/vm/`.
