English | [正體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md)

# gentoo-install

<!-- fact: identity -->

gentoo-install runs in a Linux live environment to install an amd64 Gentoo system. An interactive menu or a TOML configuration file specifies the installation. The interface is available in English, Traditional Chinese, Simplified Chinese, Japanese and Korean.

![The menu showing the installation decisions](screenshot-en.png)

![The cjktty console rendering Simplified Chinese, Traditional Chinese, Japanese and Korean](cjk-console.png)

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

## Installing from memory

<!-- fact: install-memory -->

`--ram` and `--lowram` arm one boot into a live environment held in memory, which is what a rented machine with no console and no rescue image needs before its own disk can be installed over. The installer, the chosen configuration and the authorised keys travel inside the initramfs, so the environment comes up running the revision that armed it:

```sh
./bootstrap.sh --ram --ssh-key github:zakkaus --root-password 'replace this'
reboot
ssh root@the-machine
```

The default boot entry is not changed, so a machine that does not come up in the environment boots what it booted before; `--disarm` takes the arming back. `--bypass` replaces the default entry instead, for firmware that drops a one-shot entry, and it is the one path where an environment that fails to come up leaves a machine that does not boot at all.

The first screen offers the install and a rescue shell and has no timeout, and nothing is erased until it is answered. `--ram` boots the Gentoo CJK ISO, which carries ZFS and needs about 2 GiB of RAM; `--lowram` boots the Alpine netboot bundle, which is smaller and has no `zfs.ko`. `--ssh-port` moves the daemon off 22.

## Converting a running system

<!-- fact: install-in-place -->

`mode = "in-place"` in the `[disk]` table replaces the userland of the running distribution instead of partitioning a disk. The table carries no device list, because the layout is read from the machine:

```toml
config_version = 1

[system]
hostname = "converted"
timezone = "UTC"
locales = ["en_US.UTF-8"]
locale = "en_US.UTF-8"
init = "systemd"
root_password_hash = "$6$gentooinst$IR3GrdJ862XljQYDqocr4tKniIRDIT.jQNFzIrHE3U75H6B6YSWZoSYoVd5edSHpqaYBdiNfXHCoIPRVgb9lT/"

[portage]
profile = "default/linux/amd64/23.0/systemd"
makeopts = "-j4"

[bootloader]
kind = "grub"
firmware = "uefi"

[disk]
mode = "in-place"
```

The hash above is an example and must be replaced before execution. An interactive run prints what the conversion replaces and requires the word `convert` before anything is written; a run with no terminal is not asked, because `mode = "in-place"` in the file is the authorisation and a question there would hold a serial console open for ever.

**The session that started the run is the lifeline.** A new SSH login stops working once `/usr` and `/etc` belong to the new system, while the session that started the run keeps the binaries it already mapped.

## Resuming an interrupted run

<!-- fact: resume-behavior -->

`--resume` skips an operation recorded as complete only when its journal position and identity match the current plan and its effect is marked as surviving a reboot:

```sh
./bootstrap.sh --config my-install.toml --resume
```

<!-- fact: resume-limits -->

Resume is limited to the same live session, the same installer and the same configuration file, and the installer refuses the other cases instead of documenting them.

- The journal opens with a digest of the configuration, the machine's boot id and a digest of the installer's own source; `--resume` compares all three and stops with an explanatory message on any mismatch. Where the kernel publishes no boot id, only the other two are compared.
- The default journal is `/run/gentoo-install/install.jsonl`, so it does not survive a reboot in any case.
- Each operation record also carries an identity derived from that operation's class source and field values, so an operation whose identity changed is performed again rather than skipped. A change to a shared helper or constant is outside that per-operation identity and is covered by the installer digest instead.

## Capabilities

<!-- fact: capability-scope -->

The paths below are implemented and have automated unit or plan coverage unless the verification status identifies a narrower boundary.

<!-- fact: storage-device-graph -->

**Storage.**
- The device graph covers GPT and MBR; ext2, ext3, ext4, btrfs subvolumes, xfs, f2fs and vfat; swap; and LUKS2, LVM and mdraid.
- ZFS belongs to the same graph: a pool is a stripe, a mirror, or raidz1, raidz2 or raidz3 over its vdevs, native encryption is a property of the pool, and each dataset is a node of its own.
- Existing partition tables can be retained, with a separate keep, format or delete decision for each partition.

<!-- fact: zram-system -->

The system configuration can configure zram independently of the device graph and swap partitions.

<!-- fact: in-place-conversion -->

**In-place conversion.**
- Setting `mode = "in-place"` in the `[disk]` table replaces the userland of the running distribution with Gentoo instead of partitioning a disk.
- The layout is read from the machine, so the table carries no device list.
- `/bin`, `/sbin`, `/etc`, `/lib`, `/lib64`, `/usr` and `/var` are replaced; `/home`, `/root`, `/srv`, `/opt` and every other path are left alone, and `/etc` is replaced rather than merged.

- The staged system is built under `/gentoo-install.new` while the running one is untouched, each directory is then exchanged with `rename(2)`, and only the writes to the esp or the boot sector follow the exchange.
- A root below LUKS, LVM or mdraid, a root filesystem this installer cannot describe, a machine with less than 10 GiB free on the root filesystem, and a live medium are each refused by name before anything is written.

<!-- fact: prepared-image -->

**Disk images.**
- `mode = "image"` installs into the sparse file named by `disk.image` and sized by `disk.size` instead of onto a disk, so the product is a file that can be copied elsewhere and written later.
- `mode = "dd"` installs nothing: it streams the image at `disk.source` onto the whole disk at `disk.destination`, decoding `raw`, `gz`, `xz`, `zst` or `tar` as it reads, and keeps whatever layout and bootloader that image already carries.
- Neither mode accepts the keys of the other, and `partition` mode accepts neither set.

<!-- fact: boot-system -->

**Boot and system.**
- GRUB supports UEFI and BIOS, systemd-boot supports UEFI, and ZFSBootMenu boots a ZFS root on UEFI, taking each kernel from the boot environment's own `/boot` inside the pool.
- The installer configures systemd or OpenRC, dracut, locale, keyboard layout, timezone, hostname, DNS, static addresses and the selected network manager.

<!-- fact: remote-unlock -->

**Unlocking an encrypted root over SSH.**
- `[kernel.remote_unlock]` puts an SSH daemon in the boot path, for a machine whose passphrase prompt nobody is sitting in front of.
- `enabled` turns it on, `port` defaults to 222 rather than 22 so a client's `known_hosts` entry for the running system does not collide with the initramfs one, and `address`, `gateway` and `interface` give that daemon a static address; an empty address asks for DHCP.
- A LUKS root is opened by `sys-kernel/dracut-crypt-ssh` in the system initramfs, and a ZFS root by the dropbear ZFSBootMenu builds into its own image.
- The authorised keys are the ones in `system.authorized_keys`: a configuration that enables the unlock and lists no key is refused by name, because the daemon it describes is one nobody can log in to.

<!-- fact: desktop-language -->

**Desktop and language support.**
- GNOME, KDE Plasma and Xfce are available with gdm, sddm, lightdm, or greetd and its tuigreet console greeter.
- Graphics settings cover AMD, Intel, NVIDIA and virtual machines.
- The package catalog includes fcitx5, Rime, Anthy, Mozc, Hangul and CJK fonts.
- The kernel choices include `sys-kernel/gentoo-cjk-kernel-bin` and `sys-kernel/gentoo-cjk-kernel`, both of which carry the cjktty patch.

<!-- fact: portage -->

**Portage.**
- The configuration covers the profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, mirrors and repository synchronization.
- The gentoo-zh and gig overlays can be selected independently.
- Selecting `zh-TW`, `zh-CN`, `ja` or `ko` as the interface language also selects the gentoo-zh patched binary kernel and its overlay; selecting `en` does not.
- Official and gentoo-zh binary package sources have separate settings and keys.

<!-- fact: proxy -->

**Session proxy.**
- The `[proxy]` table accepts `kind`, `host`, `port`, optional `username` and `password`, and `bypass`.
- `kind` is `http`, `https` or `socks5`; an empty host selects direct connection, which is the default.
- SOCKS5 derives `socks5h://`, so host names resolve at the proxy for intranet access.
- The interface has one field per value and a menu for the proxy kind.
- The bypass value is comma-separated in the interface and a list in TOML.

- The configured proxy is used for stage3 and its signing key, main-tree and overlay version lookups, the `gitweb.gentoo.org` ZFS ebuild lookup, Portage downloads through `make.conf` and `FETCHCOMMAND`/`RESUMECOMMAND`, `wget`, `curl`, `git`, GnuPG, the binhost, overlays and paste upload.
- The clock, the initial connectivity check and the pre-menu mirror check run before the configuration is available and are therefore not covered by this setting.
- The installer keeps the credential out of dry-run descriptions and publishes a credential-free proxy endpoint with the bypass list; the installed system receives that endpoint and list.

<!-- fact: memory-environment -->

**Memory environment.**
- `--ram` and `--lowram` arm one boot into a live environment held in memory, then ask whether to reboot; there is no interface on this path, because the machine it addresses usually has one SSH session and no console.
- `--ram` uses the Gentoo CJK ISO, which carries ZFS and needs about 2 GiB of RAM, because its initramfs stops at an emergency shell when memory less the 824 MiB live image falls under 1 GiB.
- `--lowram` uses the Alpine netboot bundle, which is smaller and has no `zfs.ko`.
- Neither pins a version: both publishers list the current image with its checksum, which is fetched and verified before anything is armed.

- The default boot entry is never changed, so an environment that does not come up leaves a machine that still boots.
- `--bypass` replaces it instead, for firmware that drops a one-shot entry; it is the one path where an environment that does not come up leaves a machine that does not boot at all, and nothing selects it automatically.

<!-- fact: memory-environment-access -->

**Watching a memory install over SSH.**
- `--ssh-key` accepts a literal public key (`ssh-ed25519`, `ssh-rsa`, `ecdsa-sha2-nistp256`, `-384`, `-521`, and the `sk-` variants), a path, an `http` or `https` URL, or `github:user` and `gitlab:user`; `--ssh-port` and `--root-password` set the rest.
- The installer, the chosen configuration and the keys travel inside the initramfs, so the environment runs the revision that wrote that configuration and reaches `authorized_keys` before the first login.
- The operator reconnects over SSH and watches the install rather than holding a console open.
- Nothing is erased until the first screen is answered: it offers the install and a rescue shell, and has no timeout.

<!-- fact: plan-records -->

**Plan and records.**
- A dry run prints an operation plan without probing storage hardware.
- A real installation uses the same planner after adding probed mdraid metadata for reused devices, so hardware-dependent validation can change the result.
- `install.log` records command output, and `install.jsonl` records operations, package sources and binary-package degradation reasons.
- Before uploading a configuration to `paste.gentoozh.org`, the menu replaces `password_hash` and `root_password_hash` with `removed-before-publishing` and omits the proxy `username` and `password` keys entirely; the other configuration values remain in the upload.
- The menu displays the resulting page address as text and as a QR code.

## Verification status

<!-- fact: verification-scope -->

[`TESTED.md`](TESTED.md) is the verification record: one row for each exercised path, naming the installer revision it ran at and where it ran. A run counts only when its recorded revision matches the installer, its installation exit code is `0`, the installed system boots, and the post-boot configuration checks pass.

| Path | Record |
| --- | --- |
| Installing onto a disk | ext4, ext2, ext3, xfs, btrfs, f2fs, ZFS, LVM, mdraid and LUKS2, on both firmwares and both init systems |
| ZFS pools | a stripe, a mirror, a raidz, an encrypted pool, and a pool booted by ZFSBootMenu |
| Unlocking over SSH | a LUKS root opened from the system initramfs, and a ZFS pool opened from ZFSBootMenu's own image |
| Static addressing and greetd | cluster records of their own |
| Converting a running system | four QEMU records: two on BIOS, one on UEFI that retained `/home`, and one on UEFI whose btrfs root carries `/home` and `/var` as subvolumes |
| Converting a machine this installer produced | six cluster conversions reached the reboot; five booted and one stopped in GRUB's rescue shell for a missing module while the conversion exited `0`, so this path is not yet reliable |
| `--ram` and `--lowram` | a Debian 12 machine armed one boot, kept its default entry and came up in the delivered environment, carrying the configuration it was given; answering `install` there installed Gentoo and booted the disk it had written |
| `--bypass` | replacing the default entry survived a power cycle: both boots came up in the memory environment |
| An armed boot that does not come up | a machine whose armed initramfs was removed never showed the delivered screen and reached its own cloud system on the two boots that followed |
| `dd` | one prepared image written onto a whole disk from a live medium and read back byte for byte, raw and gzipped |
| Installing into a file | one record: the image was attached with `losetup -Pf` and read back as the two filesystems its layout declares, and nothing has booted from that file |
| The menu | opened row by row on an 80x24 serial console in English, Traditional and Simplified Chinese, Japanese and Korean, with no row wider than the terminal |

A source-built kernel and binary-package degradation have runner-level tests only, and a runner-level test is not an end-to-end record. Files under `tests/fixtures/` exercise the configuration model; their presence establishes nothing about an installed machine.

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

| Code | `gentoo-install` |
| --- | --- |
| `0` | successful completion |
| `1` | configuration error |
| `2` | `argparse` usage error or preflight failure |
| `3` | integrity failure |
| `4` | download, external-command, OS or uncategorized installer failure |
| `5` | operator abort |

`bootstrap.sh` can also exit `1` before the Python CLI starts when its Python, required-command or root checks fail.

## Questions

<!-- fact: faq-customisation -->

**Does an installer of this kind take away what makes Gentoo Gentoo?**

No. It performs the base installation and stops there: partitions, a stage3, Portage configuration, a kernel, a bootloader and an optional desktop. Every decision after that remains the operator's, on a system that is an ordinary Gentoo installation with no component of this project left running on it. What it removes is the cost of the first hour, which is what makes Gentoo hard to start with and hard to deploy across many machines or on a VPS.

## Contributing

<!-- fact: contributing -->

[CONTRIBUTING.md](CONTRIBUTING.md) describes the development setup, architecture and required checks.

## License

<!-- fact: license -->

gentoo-install is distributed under the GNU General Public License, either version 2 or, at the recipient's option, any later version. The version 2 text is in [LICENSE](LICENSE), and every source file carries `SPDX-License-Identifier: GPL-2.0-or-later`.
