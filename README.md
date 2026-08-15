English | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install runs in a Linux live environment to install an amd64 Gentoo system. An interactive menu or a TOML configuration file specifies the installation. The interface is available in English, Traditional Chinese, Simplified Chinese, Japanese and Korean.

![The menu showing the installation decisions](screenshot-en.png)

![The cjktty console rendering Simplified Chinese, Traditional Chinese, Japanese and Korean](cjk-console.png)

## Capabilities

<!-- fact: capability-scope -->

The paths below are implemented and have automated unit or plan coverage unless the verification status identifies a narrower boundary.

<!-- fact: storage-device-graph -->

**Storage.** The device graph covers GPT and MBR; ext2, ext3, ext4, btrfs subvolumes, xfs, f2fs and vfat; swap; and LUKS2, LVM and mdraid. Existing partition tables can be retained, with a separate keep, format or delete decision for each partition.

<!-- fact: zram-system -->

The system configuration can configure zram independently of the device graph and swap partitions.

<!-- fact: boot-system -->

**Boot and system.** GRUB supports UEFI and BIOS, and systemd-boot supports UEFI. The installer configures systemd or OpenRC, dracut, locale, keyboard layout, timezone, hostname, DNS, static addresses and the selected network manager.

<!-- fact: desktop-language -->

**Desktop and language support.** GNOME, KDE Plasma and Xfce are available with gdm, sddm or lightdm. Graphics settings cover AMD, Intel, NVIDIA and virtual machines. The package catalog includes fcitx5, Rime, Anthy, Mozc, Hangul and CJK fonts. The kernel choices include `sys-kernel/gentoo-cjk-kernel-bin` and `sys-kernel/gentoo-cjk-kernel`, both of which carry the cjktty patch.

<!-- fact: portage -->

**Portage.** The configuration covers the profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, mirrors and repository synchronization. The gentoo-zh and gig overlays can be selected independently. Selecting `zh-TW`, `zh-CN`, `ja` or `ko` as the interface language also selects the gentoo-zh patched binary kernel and its overlay; selecting `en` does not. Official and gentoo-zh binary package sources have separate settings and keys.

<!-- fact: proxy -->

**Session proxy.** The `[proxy]` table accepts `kind`, `host`, `port`, optional `username` and `password`, and `bypass`. `kind` is `http`, `https` or `socks5`; an empty host selects direct connection, which is the default. SOCKS5 derives `socks5h://`, so host names resolve at the proxy for intranet access. The interface has one field per value and a menu for the proxy kind. The bypass value is comma-separated in the interface and a list in TOML.

After the proxy is selected, the configured proxy is used for stage3 and its signing key, main-tree and overlay version lookups, the `gitweb.gentoo.org` ZFS ebuild lookup, Portage downloads through `make.conf` and `FETCHCOMMAND`/`RESUMECOMMAND`, `wget`, `curl`, `git`, GnuPG, the binhost, overlays and paste upload. The clock, initial connectivity check and pre-menu mirror check run before the configuration is available and therefore are not covered by this setting. The installer keeps the credential out of dry-run descriptions and publishes a credential-free proxy endpoint with the bypass list; the installed system receives that endpoint and list.

<!-- fact: plan-records -->

**Plan and records.** A dry run prints an operation plan without probing storage hardware. A real installation uses the same planner after adding probed mdraid metadata for reused devices, so hardware-dependent validation can change the result. `install.log` records command output, and `install.jsonl` records operations, package sources and binary-package degradation reasons. Before uploading a configuration to `paste.gentoozh.org`, the menu replaces only `password_hash` and `root_password_hash` values with `removed-before-publishing`; the other configuration values remain in the upload. The menu displays the resulting page address as text and as a QR code.

## Verification status

<!-- fact: verification-history -->

Historical end-to-end records used the amd64 Gentoo minimal ISO at installer revisions `a71f91b4735469bae8ec76af170201acb967a5fe` and `f7257793f95df4b21ebf2ac6a775a343f6205f1b`. Those records covered selected UEFI and BIOS installations, systemd and OpenRC, ext4, btrfs, xfs, LUKS2, LVM, mdraid, Plasma and the official binhost, but later installation-path changes made them historical evidence only.

<!-- fact: verification-current -->

Revision-tagged end-to-end records dated 2026-08-11 cover one installation and boot from each of Arch Linux, openSUSE, Debian, Fedora and a self-built gentoo-cjk minimal ISO. The records cover installer revision [`b931ef46fc15ed50385f70467f2bfb0a8d1fd154`](https://github.com/Zakkaus/gentoo-install/commit/b931ef46fc15ed50385f70467f2bfb0a8d1fd154). The gentoo-cjk record uses ZFS and ZFSBootMenu; the other four use ext4. A run counts as current evidence only when its recorded revision matches the installer, its installation exit code is `0`, the installed system boots and the post-boot configuration checks pass.

Other implemented combinations remain unverified end to end. Current evidence does not cover initramfs SSH unlock, greetd desktop sessions or ibus outside GNOME. It also does not cover the official Gentoo minimal ISO, Alpine or Gig-OS live media, or binary-host failure fallback.

The proxy path has focused unit and plan coverage, including SOCKS5 DNS mode, redaction in dry-run output and published configuration, and the credential-free endpoint retained in the installed system. A revision-tagged cluster run covers the negative direction: the `vm-proxy-dead` fixture points the proxy at a port where nothing listens, and the install stops at the stage3 download with `Connection refused`, so a run that reached the mirror would show the proxy had been bypassed. 

Two runs at revision `4d8512a496d` cover the positive direction: `vm-proxy` completes an installation through a SOCKS5 proxy that demands a password, and `vm-proxy-http` through an HTTP proxy, each writing 57 operations with 93 packages from a binary host and 14 compiled. An HTTP or HTTPS proxy that requires a password cannot check the tree snapshot's signature: `emerge-webrsync` hands gemato only the credential-free endpoint. dirmngr has no SOCKS support at all, so under SOCKS5 the key refresh needs a direct route to the keyserver.

CJK text-console rendering has no current verification evidence. ext2 and ext3 additionally have no focused automated configuration test. Files under `tests/fixtures/` exercise the configuration model; their presence does not establish an end-to-end installation and boot result.

<!-- fact: verification-network -->

The IPv4-only, IPv6-only and dual-stack VM check stops before disk access. It checks address-family detection, `bootstrap.sh --missing-commands` and stage3 pointer retrieval; it does not verify stage3 download, repository synchronization, binhost access, package installation or a booted target system on those network modes.

## Requirements

<!-- fact: requirements-runtime -->

A real installation requires root privileges, an amd64 target and Python 3.11 or newer. A configuration-file dry run does not require root privileges. The installer has no third-party Python runtime dependency.

<!-- fact: requirements-version-sources -->

The menu reads Gentoo main-tree package versions from `packages.gentoo.org` and gentoo-zh patched-kernel versions from `api.github.com/repos/gentoo-zh/overlay/contents`. It reads the maximum kernel version accepted by `sys-fs/zfs` from `gitweb.gentoo.org`. An installation from a configuration file requires the mirror that configuration names instead; `--missing-commands` and `--config FILE --dry-run` require none of these version endpoints.

<!-- fact: requirements-network-filter -->

The menu disables recorded IPv4-only Gentoo mirrors when the live environment has IPv6 but no IPv4.

<!-- fact: requirements-bootstrap -->

`bootstrap.sh` reads `/etc/os-release`, reports missing commands and prints a candidate package-manager command. It recognizes these distribution families: Debian and Ubuntu; Arch; openSUSE; Fedora, RHEL and CentOS; Gentoo; and Alpine. The printed command must be reviewed before it is run.

## Safety

<!-- fact: safety-destructive -->

A real run writes to the selected disks. A configuration-file run starts without a second erase confirmation; `wipe = true`, partition deletion and filesystem creation can destroy existing data.

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

The menu requires an interactive terminal of at least 80x24 cells. It asks for the interface language once; `--lang en` selects English without that question.

<!-- fact: install-config-workflow -->

The menu can save its answers as `my-install.toml` and exit. The configuration-file workflow below prints the complete plan before the real run:

```sh
./bootstrap.sh --config my-install.toml --dry-run
# Then one of the two below. Each writes the selected disks.
./bootstrap.sh --config my-install.toml
./bootstrap.sh --config my-install.toml --no-shell   # the same run, without the root-shell prompt
```

<!-- fact: install-root-shell -->

Before unmounting, an interactive run offers a root shell in the target after either success or failure. `--no-shell` suppresses that question.

## Resuming an interrupted run

<!-- fact: resume-behavior -->

`--resume` skips an operation recorded as complete only when its journal position and identity match the current plan and its effect is marked as surviving a reboot:

```sh
./bootstrap.sh --config my-install.toml --resume
```

<!-- fact: resume-limits -->

Resume is limited to the same live session, the same installer revision and the same configuration file. The default journal is `/run/gentoo-install/install.jsonl`, so it does not survive a reboot. Each operation record contains an identity derived from that operation's class source and field values, so an operation whose identity changed is performed again rather than skipped. A change to a shared helper or constant is outside that identity, and the journal carries no digest of the configuration as a whole, so a different revision or configuration is outside the documented resume scope.

## Configuration files

<!-- fact: config-model -->

Configuration files use TOML. The top-level `config_version` field selects the schema version. Storage is a device graph: every device has an `id`, devices refer to other devices by `id`, and selectors are resolved only during a real run.

<!-- fact: config-fixtures -->

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) is a complete UEFI and ext4 schema reference. Other files under [`tests/fixtures/`](tests/fixtures/) cover BIOS, LUKS2, LVM, mdraid, ZFS, btrfs subvolumes and desktops. They contain virtual-machine disk selectors and test credentials, so they must not be installed unchanged on a real machine.

<!-- fact: config-dry-run -->

Parsing and planning do not probe storage hardware. A machine without the target disk can therefore check a configuration with `--dry-run`.

The following complete configuration demonstrates a proxy with credentials and two bypass hosts. The credential is an example and must be replaced before execution:

```toml
config_version = 1

[proxy]
kind = "socks5"
host = "proxy.example"
port = 1080
username = "operator"
password = "secret"
bypass = ["localhost", "intranet.example"]

[system]
hostname = "proxy-target"
timezone = "UTC"
locales = ["en_US.UTF-8"]
locale = "en_US.UTF-8"
init = "openrc"
root_password_hash = "$6$gentooinst$IR3GrdJ862XljQYDqocr4tKniIRDIT.jQNFzIrHE3U75H6B6YSWZoSYoVd5edSHpqaYBdiNfXHCoIPRVgb9lT/"

[portage]
profile = "default/linux/amd64/23.0"
makeopts = "-j4"

[portage.binhost]
official = false

[bootloader]
firmware = "bios"

[disk]
root = "mnt-root"

[[disk.devices]]
kind = "existing"
id = "disk"
selector = "/dev/disk/by-id/virtio-target0"
wipe = true

[[disk.devices]]
kind = "table"
id = "table"
disk = "disk"
table = "mbr"

[[disk.devices]]
kind = "partition"
id = "rootpart"
table = "table"
index = 1
role = "data"

[[disk.devices]]
kind = "filesystem"
id = "rootfs"
device = "rootpart"
type = "ext4"

[[disk.devices]]
kind = "mountpoint"
id = "mnt-root"
source = "rootfs"
path = "/"
```

## Binary packages

<!-- fact: binary-packages -->

Binary packages are optional. Disabling them keeps source builds available. The official binhost and the gentoo-zh binhost are separate options with separate trust configuration. No current end-to-end evidence covers an unreachable binhost, a missing signature or an untrusted key; these degradation paths remain unverified.

## Exit codes

<!-- fact: exit-codes -->

For `gentoo-install`, `0` means successful completion and `1` means configuration error. `2` means an `argparse` usage error or preflight failure, and `3` means integrity failure. `4` means download, external-command, OS or uncategorized installer failure, and `5` means operator abort. `bootstrap.sh` can also exit `1` before the Python CLI starts when its Python, required-command or root checks fail.

## Contributing

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md) describes the development setup, architecture and required checks.

## License

<!-- fact: license -->

gentoo-install is distributed under the GNU General Public License, either version 2 or, at the recipient's option, any later version. The version 2 text is in [LICENSE](LICENSE), and every source file carries `SPDX-License-Identifier: GPL-2.0-or-later`.
