English | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

An installer that turns any running Linux live system into a bootable Gentoo machine. A menu or a configuration file drives it. The interface is available in English, Traditional Chinese, Simplified Chinese, Japanese and Korean.

![The menu, showing every decision the installer makes](screenshot.png)

![The cjktty console rendering Simplified Chinese, Traditional Chinese, Japanese and Korean](cjk-console.png)

## Features

**Disks.** GPT and MBR. ext2/3/4, btrfs with subvolumes, xfs, f2fs, vfat, swap and zram. LUKS2, LVM and mdraid at raid0, raid1, raid5 or raid6. ZFS pools and datasets, including native encryption, mirrors and raidz. An existing partition table can be kept: each partition is separately assigned a mountpoint and a decision about formatting.

**Boot.** GRUB on UEFI and BIOS, systemd-boot, and ZFSBootMenu for a ZFS root. The initramfs is dracut, and its module list is derived from the device graph rather than listed by hand. The root can be unlocked over SSH from the initramfs.

**Kernel.** `sys-kernel/gentoo-kernel-bin` and `sys-kernel/gentoo-kernel` from `::gentoo`, and `sys-kernel/gentoo-cjk-kernel-bin` and `sys-kernel/gentoo-cjk-kernel` from the gentoo-zh overlay. The gentoo-zh pair carries [cjktty-patches](https://github.com/gentoo-zh/cjktty-patches), which renders Chinese, Japanese and Korean on the text console where a stock kernel draws blanks. The second screenshot above is `7.1.7-gentoo-dist` with that patch applied.

**System.** systemd or OpenRC. NetworkManager with wpa_supplicant or iwd, systemd-networkd, or no networking. Static addresses, DNS, hostname and timezone. A script or command to run once at first boot, optionally fetched from a URL.

**Desktop.** GNOME, KDE Plasma and Xfce, with gdm, sddm, lightdm or greetd. Graphics for amdgpu, intel, nvidia, nouveau, radeon and virtual machines: `VIDEO_CARDS`, the USE flags a driver needs, and its kernel parameters are set together rather than left to the operator.

**Input methods.** fcitx5 and ibus. Rime with the Pinyin, Bopomofo, Cangjie, Wubi and Cantonese schemes; Anthy and Mozc for Japanese; Hangul for Korean. Fonts are a separate choice from the locale.

**Portage.** Profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, mirror region and repository sync method. The gentoo-zh and gig overlays are opt-in, and selecting one also writes its keys and its `package.accept_keywords`. Binary packages come from the official host and from gentoo-zh, keyed separately.

**Every feature has a dry run.** `--dry-run` prints the operation list that a real run would apply, from the same plan, so a print-only path cannot drift from the real one. An interrupted run resumes from its journal. `install.jsonl` records the source of every package and the reason for every fallback. A configuration can be exported to the pastebin or to a QR code on the console, with the password hashes removed.

## Requirements

Root, an amd64 target, and Python 3.11 or newer. The standard library only.

Network access to `packages.gentoo.org` is required at start. Kernel versions and the kernel ceiling of `sys-fs/zfs` are read live, so no local ebuild tree is needed and the installer runs on the live systems of Alpine, Debian, openSUSE, Fedora and Arch as well as Gentoo. Without network access it stops, apart from `--missing-commands` and `--config` with `--dry-run`.

`bootstrap.sh` reads `/etc/os-release`, lists the commands the chosen layout needs and the machine lacks, and prints the install command for that distribution. It knows `apt-get`, `pacman`, `zypper`, `dnf`, `emerge` and `apk`.

## Usage

```sh
curl -fsSL https://github.com/Zakkaus/gentoo-install/archive/refs/heads/master.tar.gz | tar xz
cd gentoo-install-master
./bootstrap.sh
```

```sh
./bootstrap.sh                                       # the menu
./bootstrap.sh --config my-install.toml              # unattended
./bootstrap.sh --dry-run --config my-install.toml    # print the operations, touch nothing
./bootstrap.sh --config my-install.toml --resume     # carry on from where a run stopped
./bootstrap.sh --config my-install.toml --no-shell   # unmount at the end without asking
```

The menu needs a real terminal at 80x24 or larger. The interface language is asked once at the start; `--lang en` skips that question.

Before unmounting, whether the run finished or failed, the installer offers a root shell inside the new system. It offers it on failure as well, because whether the machine is still recoverable is the operator's judgement, and after the unmount the whole layout has to be mounted again by hand. `--no-shell` removes the question.

## Configuration file

TOML. The first line declares `config_version`. The disk is a device graph: every device carries an `id`, devices refer to each other by `id`, and device paths are resolved at run time.

```toml
config_version = 1

[system]
hostname = "gentoo"
locale = "zh_TW.UTF-8"
init = "systemd"
root_password_hash = "$6$..."   # from openssl passwd -6, never a plaintext password

[portage]
profile = "default/linux/amd64/23.0/systemd"   # has to agree with init

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

`tests/fixtures/` holds working examples covering UEFI, BIOS, LUKS2, LVM, mdraid, ZFS, btrfs subvolumes and desktops. Parsing touches no hardware, so a machine with no target disk can still check a configuration with `--dry-run`.

## Binary packages

Optional, and never the only path. Building from source is the guaranteed one. The official binhost and the gentoo-zh binhost are separate options with separate keys. An unreachable host, a missing signature or an untrusted key falls back to compiling with a warning, and `install.jsonl` records the reason.

## Exit codes

`0` finished, `1` configuration error, `2` preflight failed, `3` integrity check failed, `4` an external command failed, `5` aborted by the operator.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md).
