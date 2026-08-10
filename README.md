English | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

gentoo-install is an installer that installs an amd64 Gentoo system from a supported Linux live environment. An interactive menu or a TOML configuration file specifies the installation. The interface is available in English, Traditional Chinese, Simplified Chinese, Japanese and Korean.

![The menu showing the installation decisions](screenshot.png)

![The cjktty console rendering Simplified Chinese, Traditional Chinese, Japanese and Korean](cjk-console.png)

## Capabilities

**Storage.** The device graph supports GPT and MBR; ext2/3/4, btrfs subvolumes, xfs, f2fs and vfat; swap and zram; and LUKS2, LVM and mdraid. Existing partition tables can be retained, with a separate keep, format or delete decision for each partition.

**Boot and system.** GRUB supports UEFI and BIOS, and systemd-boot supports UEFI. The installer configures systemd or OpenRC, dracut, locale, keyboard layout, timezone, hostname, DNS, static addresses and the selected network manager.

**Desktop and language support.** GNOME, KDE Plasma and Xfce are available with gdm, sddm or lightdm. Graphics settings cover AMD, Intel, NVIDIA and virtual machines. The package catalog includes fcitx5, Rime, Anthy, Mozc, Hangul and CJK fonts. Patched kernels from gentoo-zh can render Chinese, Japanese and Korean on the Linux text console.

**Portage.** The configuration covers the profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, mirrors and repository synchronization. The gentoo-zh and gig overlays are opt-in. Official and gentoo-zh binary package sources have separate settings and keys.

**Plan and records.** A dry run and a real installation use the same operation plan. `install.log` records command output, and `install.jsonl` records operations, package sources and binary-package degradation reasons. The menu can upload a redacted configuration to `paste.gentoozh.org` and display the resulting page address as text and as a QR code.

## Verification status

The last recorded end-to-end baseline covers selected UEFI and BIOS installations, systemd and OpenRC, ext4, btrfs, xfs, LUKS2, LVM, mdraid, Plasma and the official binhost. Each recorded run names its installer revision and boots the installed system before it counts as evidence.

ZFS and ZFSBootMenu, initramfs SSH unlock, greetd desktop sessions and ibus outside GNOME are not part of that baseline at the current revision. Installation from the six non-default live media and binary-host failure fallback are also outside the baseline. Files under `tests/fixtures/` exercise the configuration model; their presence does not by itself establish end-to-end support.

## Requirements

A real installation requires root privileges, an amd64 target and Python 3.11 or newer. A configuration-file dry run does not require root privileges. The installer has no third-party Python runtime dependency.

The menu reads every version from `packages.gentoo.org` and requires network access to it. An installation from a configuration file requires the mirror that configuration names instead; `--missing-commands` and `--config FILE --dry-run` require neither. Kernel versions and the maximum kernel version supported by `sys-fs/zfs` are read at run time.

IPv4-only, IPv6-only and dual-stack networks are all supported; the menu refuses a mirror the machine's address family cannot reach.

`bootstrap.sh` reads `/etc/os-release`, reports missing commands and prints a candidate package-manager command. It recognizes these distribution families: Debian and Ubuntu; Arch; openSUSE; Fedora, RHEL and CentOS; Gentoo; and Alpine. The printed command must be reviewed before it is run.

## Safety

A real run writes to the selected disks. A configuration-file run starts without a second erase confirmation; `wipe = true`, partition deletion and filesystem creation can destroy existing data.

Before a real run, the disk selectors and every destructive operation must be checked in the dry-run output. Stable `/dev/disk/by-id/` selectors are preferable to names such as `/dev/sda`, and required data must have a separate backup.

## Installation

The following commands download the current `master` archive and open the menu:

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

The menu requires an interactive terminal of at least 80x24 cells. It asks for the interface language once; `--lang en` selects English without that question.

The menu can save its answers as `my-install.toml` and exit. The configuration-file workflow below prints the complete plan before the real run:

```sh
./bootstrap.sh --config my-install.toml --dry-run
# Then one of the two below. Each writes the selected disks.
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # the same run, without the root-shell prompt
```

Before unmounting, an interactive run offers a root shell in the target after either success or failure. `--no-shell` suppresses that question.

## Resuming an interrupted run

`--resume` skips operations recorded as complete in the journal:

```sh
./bootstrap.sh --config my-install.toml --resume
```

Resume is limited to the same live session. The default journal is `/run/gentoo-install/install.jsonl`, so it does not survive a reboot. Each journal entry records a digest of the operation's implementation and of its own values, so an operation whose code or payload changed is performed again rather than skipped. The journal carries no digest of the configuration as a whole.

## Configuration files

Configuration files use TOML. The top-level `config_version` field selects the schema version. Storage is a device graph: every device has an `id`, devices refer to other devices by `id`, and selectors are resolved only during a real run.

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) is a complete UEFI and ext4 schema reference. Other files under [`tests/fixtures/`](tests/fixtures/) cover BIOS, LUKS2, LVM, mdraid, ZFS, btrfs subvolumes and desktops. They contain virtual-machine disk selectors and test credentials, so they must not be installed unchanged on a real machine.

Parsing and planning do not probe storage hardware. A machine without the target disk can therefore check a configuration with `--dry-run`.

## Binary packages

Binary packages are optional. Disabling them keeps source builds available. The official binhost and the gentoo-zh binhost are separate options with separate trust configuration. The current end-to-end baseline does not cover an unreachable binhost, a missing signature or an untrusted key, so those degradation paths remain listed under verification status.

## Exit codes

`0` means completed, `1` means configuration error, `2` means preflight failure, `3` means integrity failure, `4` means external-command failure and `5` means operator abort.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) describes the development setup, architecture and required checks.
