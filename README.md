English | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install is an installer that installs an amd64 Gentoo system from a recognized Linux live environment. An interactive menu or a TOML configuration file specifies the installation. The interface is available in English, Traditional Chinese, Simplified Chinese, Japanese and Korean.

![The menu showing the installation decisions](screenshot.png)

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

**Desktop and language support.** GNOME, KDE Plasma and Xfce are available with gdm, sddm or lightdm. Graphics settings cover AMD, Intel, NVIDIA and virtual machines. The package catalog includes fcitx5, Rime, Anthy, Mozc, Hangul and CJK fonts. Patched kernels from gentoo-zh can render Chinese, Japanese and Korean on the Linux text console.

<!-- fact: portage -->

**Portage.** The configuration covers the profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, mirrors and repository synchronization. The gentoo-zh and gig overlays can be selected independently. Selecting `zh-TW`, `zh-CN`, `ja` or `ko` as the interface language also selects the gentoo-zh patched binary kernel and its overlay; selecting `en` does not. Official and gentoo-zh binary package sources have separate settings and keys.

<!-- fact: plan-records -->

**Plan and records.** A dry run and a real installation use the same operation plan. `install.log` records command output, and `install.jsonl` records operations, package sources and binary-package degradation reasons. The menu can upload a redacted configuration to `paste.gentoozh.org` and display the resulting page address as text and as a QR code.

## Verification status

<!-- fact: verification-history -->

Historical end-to-end records used the amd64 Gentoo minimal ISO at installer revisions `a71f91b4735469bae8ec76af170201acb967a5fe` and `f7257793f95df4b21ebf2ac6a775a343f6205f1b`. Those records covered selected UEFI and BIOS installations, systemd and OpenRC, ext4, btrfs, xfs, LUKS2, LVM, mdraid, Plasma and the official binhost, but later installation-path changes made them historical evidence only.

<!-- fact: verification-current -->

No revision-tagged end-to-end run currently verifies the current installer revision. Every implemented installation-and-boot combination is therefore currently unverified end to end, including all storage, boot, desktop, live-medium and binhost combinations described above. ext2 and ext3 additionally have no focused automated configuration test. Files under `tests/fixtures/` exercise the configuration model; their presence does not establish an end-to-end installation and boot result.

<!-- fact: verification-network -->

The IPv4-only, IPv6-only and dual-stack VM check stops before disk access. It checks address-family detection, `bootstrap.sh --missing-commands` and stage3 pointer retrieval; it does not verify stage3 download, repository synchronization, binhost access, package installation or a booted target system on those network modes.

## Requirements

<!-- fact: requirements-runtime -->

A real installation requires root privileges, an amd64 target and Python 3.11 or newer. A configuration-file dry run does not require root privileges. The installer has no third-party Python runtime dependency.

<!-- fact: requirements-version-sources -->

The menu reads Gentoo main-tree package versions from `packages.gentoo.org` and gentoo-zh patched-kernel versions from `api.github.com/repos/gentoo-zh/overlay/contents`. It reads the maximum kernel version accepted by `sys-fs/zfs` from `gitweb.gentoo.org`. An installation from a configuration file requires the mirror that configuration names instead; `--missing-commands` and `--config FILE --dry-run` require none of these version endpoints.

<!-- fact: requirements-network-filter -->

The menu rejects a mirror when none of the detected address families match the mirror's declared IPv4 or IPv6 availability.

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

`--resume` skips operations recorded as complete in the journal:

```sh
./bootstrap.sh --config my-install.toml --resume
```

<!-- fact: resume-limits -->

Resume is limited to the same live session, the same installer revision and the same configuration file. The default journal is `/run/gentoo-install/install.jsonl`, so it does not survive a reboot. Each entry records a digest of that operation's own class source and of its own field values, so an operation whose class or fields changed is performed again rather than skipped. A change to a shared helper or constant is outside that digest, and the journal carries no digest of the configuration as a whole, so resuming across a different revision or configuration is not supported.

## Configuration files

<!-- fact: config-model -->

Configuration files use TOML. The top-level `config_version` field selects the schema version. Storage is a device graph: every device has an `id`, devices refer to other devices by `id`, and selectors are resolved only during a real run.

<!-- fact: config-fixtures -->

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) is a complete UEFI and ext4 schema reference. Other files under [`tests/fixtures/`](tests/fixtures/) cover BIOS, LUKS2, LVM, mdraid, ZFS, btrfs subvolumes and desktops. They contain virtual-machine disk selectors and test credentials, so they must not be installed unchanged on a real machine.

<!-- fact: config-dry-run -->

Parsing and planning do not probe storage hardware. A machine without the target disk can therefore check a configuration with `--dry-run`.

## Binary packages

<!-- fact: binary-packages -->

Binary packages are optional. Disabling them keeps source builds available. The official binhost and the gentoo-zh binhost are separate options with separate trust configuration. No current end-to-end evidence covers an unreachable binhost, a missing signature or an untrusted key, so those degradation paths remain listed under verification status.

## Exit codes

<!-- fact: exit-codes -->

After the Python CLI successfully parses its arguments, `0` means completed, `1` means configuration error, `2` means preflight failure, `3` means integrity failure, `4` means external-command failure and `5` means operator abort. Before that point, `argparse` uses `2` for invalid arguments, while `bootstrap.sh` uses `1` for launcher failures such as an insufficient Python version, missing commands or insufficient privileges.

## Contributing

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md) describes the development setup, architecture and required checks.
