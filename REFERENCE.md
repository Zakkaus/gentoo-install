# gentoo-install reference

This English lookup reference holds command, configuration and special-mode detail that does not belong in the normal installation flow.

## Runtime requirements

The menu reads Gentoo main-tree package versions from `packages.gentoo.org` and gentoo-zh patched-kernel versions from `api.github.com/repos/gentoo-zh/overlay/contents`. It reads the maximum kernel version accepted by `sys-fs/zfs` from `gitweb.gentoo.org`. An installation from a configuration file requires the mirror that configuration names instead; `--missing-commands` and `--config FILE --dry-run` require none of these version endpoints.

The menu disables recorded IPv4-only Gentoo mirrors when the live environment has IPv6 but no IPv4.

`bootstrap.sh` reads `/etc/os-release`, reports missing commands and prints a candidate package-manager command. It recognizes these distribution families: Debian and Ubuntu; Arch; openSUSE; Fedora, RHEL and CentOS; Gentoo; and Alpine. The printed command must be reviewed before it is run.

## Command line

| Option | Argument and default | Effect |
| --- | --- | --- |
| `--config` | file or URL; unset | Loads that source instead of opening the menu. |
| `--dry-run` | none; `false` | Renders the derived operations and summary, then exits without applying operations. |
| `--mirror` | stage3 mirror string; installer default | Selects the stage3 source for a normal installation. Memory arming derives its region from configuration. |
| `--lang` | language tag; `""` | Overrides `LC_ALL`, `LC_MESSAGES`, and `LANG` while the menu creates configuration. |
| `--target` | path; `/mnt/gentoo` | Selects the normal installation mount target. Conversion and memory arming use `/`. |
| `--work` | path; `/run/gentoo-install` | Holds run state, including the report and journal. |
| `--missing-commands` | none; `false` | Prints missing host commands, one per line, then exits. |
| `--resume` | none; `false` | Uses the existing journal to skip compatible completed operations. |
| `--no-shell` | none; `false` | Suppresses the target root-shell offer. It also makes memory arming unattended. |
| `--skip-preflight` | none; `false` | Skips normal-installation preflight checks. |
| `--ram` | none; unset memory mode | Arms one boot into the Gentoo CJK ISO held in memory. It conflicts with `--lowram`. |
| `--lowram` | none; unset memory mode | Arms one boot into the Alpine netboot environment held in memory. It conflicts with `--ram`. |
| `--ssh-key` | key, file, HTTP(S) URL, or `github:`/`gitlab:` reference; `""` | Sets a memory-environment authorised public key. It requires a memory mode. |
| `--ssh-port` | integer; unset | Sets the memory environment's `sshd` port. It requires a memory mode. |
| `--root-password` | string; `""` | Sets the memory environment root password. It requires a memory mode. |
| `--bypass` | none; `false` | Replaces the default boot entry instead of arming a one-shot entry. It requires a memory mode. |
| `--disarm` | none; `false` | Removes a previously armed memory boot and files it placed before configuration loading. |

## Memory environment

`--ram` and `--lowram` arm one boot into a live environment held in memory, which is what a rented machine with no console and no rescue image needs before its own disk can be installed over. The installer, the chosen configuration and the authorised keys travel inside the initramfs, so the environment comes up running the revision that armed it:

```sh
./bootstrap.sh --ram --ssh-key github:zakkaus --root-password 'replace this'
reboot
ssh root@the-machine
```

The default boot entry is not changed, so a machine that does not come up in the environment boots what it booted before; `--disarm` takes the arming back. `--bypass` replaces the default entry instead, for firmware that drops a one-shot entry, and it is the one path where an environment that fails to come up leaves a machine that does not boot at all.

The first screen asks `install now? [yes/no]` and has no timeout, and nothing is erased until it is answered. `--ram` boots the Gentoo CJK ISO, which carries ZFS and needs about 2 GiB of RAM; `--lowram` boots the Alpine netboot bundle, which is smaller and has no `zfs.ko`. `--ssh-port` moves the daemon off 22. The first page names the command that starts the install later, so answering `no` or losing the connection before answering does not make rebooting the only way back to it.

`--ram` reaches wifi and `--lowram` does not. The Gentoo CJK ISO carries NetworkManager and `linux-firmware`, so `nmcli device wifi connect <SSID> password <PASSWORD>` brings a link up and the install runs after it; the first page says so in Traditional Chinese, Simplified Chinese and English. The Alpine netboot environment has no wireless driver and no supplicant, and its full module set is in a `modloop` that itself has to be fetched, so a machine whose only link is wifi cannot use `--lowram` at all.

**Memory environment.**
- `--ram` and `--lowram` arm one boot into a live environment held in memory, then ask whether to reboot; there is no interface on this path, because the machine it addresses usually has one SSH session and no console.
- `--ram` uses the Gentoo CJK ISO, which carries ZFS and needs about 2 GiB of RAM, because its initramfs stops at an emergency shell when memory less the 824 MiB live image falls under 1 GiB.
- `--lowram` uses the Alpine netboot bundle, which is smaller and has no `zfs.ko`.
- Neither pins a version: both publishers list the current image with its checksum, which is fetched and verified before anything is armed.

- The default boot entry is never changed, so an environment that does not come up leaves a machine that still boots.
- `--bypass` replaces it instead, for firmware that drops a one-shot entry; it is the one path where an environment that does not come up leaves a machine that does not boot at all, and nothing selects it automatically.

**Watching a memory install over SSH.**
- `--ssh-key` accepts a literal public key (`ssh-ed25519`, `ssh-rsa`, `ecdsa-sha2-nistp256`, `-384`, `-521`, and the `sk-` variants), a path, an `http` or `https` URL, or `github:user` and `gitlab:user`; `--ssh-port` and `--root-password` set the rest.
- The installer, the chosen configuration and the keys travel inside the initramfs, so the environment runs the revision that wrote that configuration and reaches `authorized_keys` before the first login.
- The operator reconnects over SSH and watches the install rather than holding a console open.
- Nothing is erased until the first screen is answered: it asks `install now? [yes/no]`, and has no timeout.

## In-place conversion

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

**In-place conversion.**
- Setting `mode = "in-place"` in the `[disk]` table replaces the userland of the running distribution with Gentoo instead of partitioning a disk.
- The layout is read from the machine, so the table carries no device list.
- `/bin`, `/sbin`, `/etc`, `/lib`, `/lib64`, `/usr` and `/var` are replaced; `/home`, `/root`, `/srv`, `/opt` and every other path are left alone, and `/etc` is replaced rather than merged.

- The staged system is built under `/gentoo-install.new` while the running one is untouched, each directory is then exchanged with `rename(2)`, and only the writes to the esp or the boot sector follow the exchange.
- A root below LUKS, LVM or mdraid, a root filesystem this installer cannot describe, a machine with less than 10 GiB free on the root filesystem, and a live medium are each refused by name before anything is written.

## Capabilities

The paths below are implemented and have automated unit or plan coverage unless the verification status identifies a narrower boundary.

**Storage.**
- The device graph covers GPT and MBR; ext2, ext3, ext4, btrfs subvolumes, xfs, f2fs and vfat; swap; and LUKS2, LVM and mdraid.
- ZFS belongs to the same graph: a pool is a stripe, a mirror, or raidz1, raidz2 or raidz3 over its vdevs, native encryption is a property of the pool, and each dataset is a node of its own.
- Existing partition tables can be retained, with a separate keep, format or delete decision for each partition.

| Model node | Fields beyond `id` | Result |
| --- | --- | --- |
| `Existing` | `selector`, `wipe` | Selects a pre-existing device. |
| `PartitionTable` | `disk`, `table`, `create`, `remove` | Creates or edits a partition table. |
| `Partition` | `table`, `index`, `role`, `size`, `label`, `min`, `max` | Defines a partition. `size` is an absolute size, a share such as `"40%"`, or `"rest"`; see the sizes below. |
| `Luks` | `backing`, `name`, `passphrase_file` | Defines a LUKS container. |
| `MdRaid` | `members`, `level`, `name`, `metadata` | Defines an mdraid array. |
| `VolumeGroup` | `members`, `name` | Defines an LVM volume group. |
| `LogicalVolume` | `group`, `name`, `size` | Defines an LVM logical volume. |

### Partition sizes

`size` takes three forms, and `min` and `max` bound the two that are derived.

| `size` | Meaning |
| --- | --- |
| `"20G"` | Exactly that much. `min` and `max` are refused beside it. |
| `"40%"` | That share of what the table has left after every absolute size on it. |
| `"rest"` | Whatever the others leave. Only the last partition may ask for it; an omitted `size` means the same and is still accepted. |

A share is of what the fixed sizes leave, so an ESP of `512MiB`, a swap of
`8G`, a root of `40%` and a home of `60%` describe a disk that fits. `min` and
`max` are what let one file install a fleet of unlike disks: four tenths of a
240 GB disk is 96 GB and four tenths of an 8 TB disk is 3.2 TB, and neither is
what the operator meant on the other machine.

[`tests/fixtures/vm-shares.toml`](tests/fixtures/vm-shares.toml) is a complete
layout written this way: a fixed ESP, a root at `40%` bounded either side, and
a home taking the rest. Its disk selector and credentials are for a virtual
machine and must not be installed unchanged on a real one.

A share below its `min` is refused rather than raised to it: the space would
come from another partition, and a layout that stops adding up without anybody
being told is what the bounds exist to prevent. Shares are resolved from the
disk's own capacity before the plan is built, so `--dry-run` prints the bytes
the install will write; a machine that does not report the disk's size says so
rather than printing a number the install would not use.

| `ZfsPool` | `vdevs`, `name`, `topology`, `encrypted`, `passphrase_file` | Defines a ZFS pool. |
| `ZfsDataset` | `pool`, `name` | Defines a ZFS dataset. |
| `Filesystem` | `device`, `kind`, `label`, `create` | Formats a device, or verifies it when `create = false`. |
| `Subvolume` | `filesystem`, `name` | Defines a Btrfs subvolume. |
| `Swap` | `device` | Defines swap on a referenced device. |
| `Mountpoint` | `source`, `path`, `options` | Mounts a filesystem, Btrfs subvolume, or ZFS dataset. |

| Choice | Values |
| --- | --- |
| Partition table | `gpt`, `mbr` |
| Partition role | `esp`, `bios-boot`, `swap`, `raid`, `lvm`, `zfs`, `data` |
| mdraid level | `raid0`, `raid1`, `raid5`, `raid6` |
| mdraid metadata | `0.90`, `1.0`, `1.1`, `1.2` |
| Filesystem | `ext2`, `ext3`, `ext4`, `btrfs`, `xfs`, `f2fs`, `vfat` |
| ZFS topology | `stripe`, `mirror`, `raidz1`, `raidz2`, `raidz3` |

The system configuration can configure zram independently of the device graph and swap partitions.

**Disk images.**
- `mode = "image"` installs into the sparse file named by `disk.image` and sized by `disk.size` instead of onto a disk, so the product is a file that can be copied elsewhere and written later.
- `mode = "dd"` installs nothing: it streams the image at `disk.source` onto the whole disk at `disk.destination`, decoding `raw`, `gz`, `xz`, `zst` or `tar` as it reads, and keeps whatever layout and bootloader that image already carries.
- Neither mode accepts the keys of the other, and `partition` mode accepts neither set.

**Boot and system.**
- GRUB supports UEFI and BIOS, systemd-boot supports UEFI, and ZFSBootMenu boots a ZFS root on UEFI, taking each kernel from the boot environment's own `/boot` inside the pool.
- The installer configures systemd or OpenRC, dracut, locale, keyboard layout, timezone, hostname, DNS, static addresses and the selected network manager.

**Unlocking an encrypted root over SSH.**
- `[kernel.remote_unlock]` puts an SSH daemon in the boot path, for a machine whose passphrase prompt nobody is sitting in front of.
- `enabled` turns it on, `port` defaults to 222 rather than 22 so a client's `known_hosts` entry for the running system does not collide with the initramfs one, and `address`, `gateway` and `interface` give that daemon a static address; an empty address asks for DHCP.
- A LUKS root is opened by `sys-kernel/dracut-crypt-ssh` in the system initramfs, and a ZFS root by the dropbear ZFSBootMenu builds into its own image.
- The authorised keys are the ones in `system.authorized_keys`: a configuration that enables the unlock and lists no key is refused by name, because the daemon it describes is one nobody can log in to.

**Desktop and language support.**
- GNOME, KDE Plasma and Xfce are available with gdm, sddm, lightdm, or greetd and its tuigreet console greeter.
- Graphics settings cover AMD, Intel, NVIDIA and virtual machines.
- The package catalog includes fcitx5, Rime, Anthy, Mozc, Hangul and CJK fonts.
- The kernel choices include `sys-kernel/gentoo-cjk-kernel-bin` and `sys-kernel/gentoo-cjk-kernel`, both of which carry the cjktty patch.

**Portage.**
- The configuration covers the profile, `MAKEOPTS`, `USE`, `ACCEPT_KEYWORDS`, `L10N`, mirrors and repository synchronization.
- The gentoo-zh and gig overlays can be selected independently.
- Selecting `zh-TW`, `zh-CN`, `ja` or `ko` as the interface language also selects the gentoo-zh patched binary kernel and its overlay; selecting `en` does not.
- Official and gentoo-zh binary package sources have separate settings and keys.

**Session proxy.**
- The `[proxy]` table accepts `kind`, `host`, `port`, optional `username` and `password`, and `bypass`.
- `kind` is `http`, `https` or `socks5`; an empty host selects direct connection, which is the default.
- SOCKS5 derives `socks5h://`, so host names resolve at the proxy for intranet access.
- The interface has one field per value and a menu for the proxy kind.
- The bypass value is comma-separated in the interface and a list in TOML.

- The configured proxy is used for stage3 and its signing key, main-tree and overlay version lookups, the `gitweb.gentoo.org` ZFS ebuild lookup, Portage downloads through `make.conf` and `FETCHCOMMAND`/`RESUMECOMMAND`, `wget`, `curl`, `git`, GnuPG, the binhost, overlays and paste upload.
- The clock, the initial connectivity check and the pre-menu mirror check run before the configuration is available and are therefore not covered by this setting.
- The installer keeps the credential out of dry-run descriptions and publishes a credential-free proxy endpoint with the bypass list; the installed system receives that endpoint and list.

**Plan and records.**
- A dry run prints an operation plan without probing storage hardware.
- A real installation uses the same planner after adding probed mdraid metadata for reused devices, so hardware-dependent validation can change the result.
- `install.log` records command output, and `install.jsonl` records operations, package sources and binary-package degradation reasons.
- Before uploading a configuration to `paste.gentoozh.org`, the menu replaces `password_hash` and `root_password_hash` with `removed-before-publishing` and omits the proxy `username` and `password` keys entirely; the other configuration values remain in the upload.
- The menu displays the resulting page address as text and as a QR code.

## Validation

Validation rejects these combinations:

| Refused combination |
| --- |
| An empty, locked, or malformed root hash with no password-authenticating user and no usable SSH-key login. |
| A ZFS root with GRUB. |
| `/boot` on ZFS with GRUB. |
| A ZFS root with BIOS boot. |
| A ZFS root with a LUKS root chain. |
| ZFSBootMenu with a root that is not ZFS. |
| UEFI boot without a mounted ESP. |
| UEFI boot with an encrypted ESP. |
| systemd-boot with BIOS boot. |
| systemd-boot with a kernel or initramfs inaccessible from the ESP because `/boot` is encrypted or not vfat. |
| An ESP on mdraid with metadata `1.1` or `1.2`. |
| BIOS boot on a GPT disk without a `bios-boot` partition. |
| CJK console rendering with a kernel lacking cjktty. |
| Remote unlock without an authorised SSH key. |
| Remote unlock without an encrypted root container or pool. |
| Remote unlock with GRUB that must unlock `/boot` before initramfs SSH starts. |
| Remote unlock of native ZFS encryption by a GRUB or systemd-boot system initramfs. |
| A CJK kernel without the `gentoo-zh` overlay. |
| CJK console rendering with a console font other than `8x16`. |
| ZFSBootMenu without the `gentoo-zh` overlay. |
| A gentoo-zh community binhost without the `gentoo-zh` overlay. |

The rules are implemented by [the compatibility model](gentoo_install/model/compat.py) and covered by [its unit tests](tests/unit/test_compat.py). They define accepted configuration, not an end-to-end boot record.

## Configuration files

Configuration files use TOML. The top-level `config_version` field selects the schema version. Storage is a device graph: every device has an `id`, devices refer to other devices by `id`, and selectors are resolved only during a real run.

A single-disk layout is written as `[disk.simple]` instead of a device graph. The parser expands it with the same template the menu uses, so both forms produce the same graph. The graph form remains for LUKS, ZFS, RAID and hand-made partition tables.

```toml
config_version = 1

[system]
hostname = "workstation"
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

[disk.simple]
disk = "/dev/disk/by-id/virtio-target0"
filesystem = "ext4"
swap = "2GiB"
```

The hash above is an example and must be replaced before execution. Only `disk` is required. An omitted key takes the installer default: `whole-disk`, `uefi`, a partition table chosen by the firmware, `xfs`, no swap, no encryption. A file must not carry both `[disk.simple]` and `[[disk.devices]]`.

[`tests/fixtures/vm-binpkg.toml`](tests/fixtures/vm-binpkg.toml) is a complete UEFI and ext4 schema reference. Other files under [`tests/fixtures/`](tests/fixtures/) cover BIOS, LUKS2, LVM, mdraid, ZFS, btrfs subvolumes and desktops. They contain virtual-machine disk selectors and test credentials, so they must not be installed unchanged on a real machine.

Parsing and planning probe storage hardware only for a layout that asks for a share of the disk: a share is a share of a capacity, so `--dry-run` reads that one disk's size rather than printing a number the install would not write. A machine without the target disk can therefore check any configuration with `--dry-run` except one using shares.

The following reference names every persisted key. A table path is TOML table notation, and `[[disk.devices]]` carries the graph nodes.

| Key | Meaning and default or choices |
| --- | --- |
| `config_version` | Persisted schema version; `1`. |
| `proxy.kind` | Proxy scheme: `http`, `https`, or `socks5`; `http`. |
| `proxy.host` | Proxy host; `""` disables the proxy. |
| `proxy.port` | Proxy port; `0`. |
| `proxy.username` | Optional proxy user name; `""`. |
| `proxy.password` | Optional proxy password; `""`. |
| `proxy.bypass` | Hosts that bypass the proxy; `[]`. |

| `system` key | Meaning and default or choices |
| --- | --- |
| `hostname` | Target hostname; `gentoo`. |
| `timezone` | Target timezone; `Asia/Shanghai`. |
| `locales` | Generated locales; `en_US.UTF-8`, `zh_CN.UTF-8`, `zh_TW.UTF-8`. |
| `locale` | Selected locale; `zh_CN.UTF-8`. |
| `keymap` | Installed-system keymap; `us`. |
| `keymap_initramfs` | Initramfs keymap; `""` follows `keymap`. |
| `interface` | Network-interface pattern; `""` matches `en*` and `eth*`. |
| `addresses` | Static CIDR addresses; `[]` selects DHCP or router advertisements. |
| `gateways` | Gateways, at most one per address family; `[]`. |
| `dns` | Resolver addresses; `[]`. |
| `authorized_keys` | Keys for root and sudo users; `[]`. |
| `console_cjk` | Requests CJK console rendering; `false`; it needs cjktty. |
| `console_font` | Console cell size: `8x8`, `8x16`, or `16x32`; `8x16`. |
| `init` | Init system: `openrc` or `systemd`; `systemd`. |
| `zram` | Compressed-RAM swap size; unset disables it. |
| `hardware_clock_utc` | RTC stores UTC; `true`. |
| `users` | User records; `[]`. |
| `root_password_hash` | Root `crypt(3)` hash; `""` locks root. |
| `logger` | Logger: `none`, `sysklogd`, `syslog-ng`, or `metalog`; `sysklogd`. |
| `cron` | Installs `sys-process/cronie`; `true`. |
| `sshd` | Installs and configures SSH daemon support; `false`. |
| `sshd_password_login` | SSH daemon accepts passwords; `false`. |
| `sshd_root_login` | Root may log in through SSH; `false`. |
| `networking` | Link management: `builtin`, `networkmanager-wpa`, `networkmanager-iwd`, or `none`; `builtin`. |
| `firewall` | Packet-filter package: `none`, `nftables`, or `iptables`; `none`. |
| `first_boot` | First-boot record; empty record by default. |

| `system.users` item | Meaning and default |
| --- | --- |
| `name` | User name; required. |
| `groups` | Supplementary groups; `[]`. |
| `shell` | Login shell; `/bin/bash`. |
| `sudo` | Makes the user a sudo user; `false`. |
| `password_hash` | User `crypt(3)` hash; `""` locks the account. |

| `system.first_boot` key | Meaning and default |
| --- | --- |
| `commands` | Shell lines run in order after the fetched script; `[]`. |
| `url` | Script URL; `""` omits a fetched script. |

| `portage` key | Meaning and default or choices |
| --- | --- |
| `profile` | Portage profile; `default/linux/amd64/23.0/systemd`. |
| `keywords` | Global keyword channel: `stable` or `testing`; `stable`. |
| `sync` | Ongoing sync: `git`, `webrsync`, or `rsync`; `git`. First sync uses webrsync. |
| `testing_packages` | Atoms accepted as testing while the system remains stable; `[]`. |
| `makeopts` | `MAKEOPTS`; `""`. |
| `common_flags` | Common compiler flags; `-O2 -pipe`. |
| `use` | `USE` flags; `[]`. |
| `video_cards` | `VIDEO_CARDS` values; `[]`. |
| `l10n` | `L10N`; `[]` derives values from generated locales. |
| `input_devices` | `INPUT_DEVICES`; `["libinput"]`. |
| `accept_license` | Accepted licenses; `["@FREE"]`. |
| `cpu_flags` | CPU flags; `[]` preserves the profile value. |
| `build_in_ram` | `/var/tmp/portage` tmpfs size; unset builds on disk. |
| `mirrors` | Mirror record; defaults shown below. |
| `binhost` | Binary-host record; defaults shown below. |
| `overlays` | Overlay records; `[]`. |

| `portage.mirrors` key | Meaning and default or choices |
| --- | --- |
| `region` | Gentoo mirror region: `cn` or `global`; `global`. |
| `speed_test` | Speed-tests offered mirrors; `false`. |
| `distfiles` | Custom distfile bases; a nonempty list replaces the built-in list. |
| `repo_sync_uri` | Explicit repository sync URI; `""`. |
| `site` | Site key in `region`; `""` selects the region's first site. |
| `gentoo_distfiles` | Writes `GENTOO_MIRRORS`; `true`. |
| `gentoo_zh` | gentoo-zh mirror: `upstream`, `cernet`, `nju`, `nyist`, or `ha`; `upstream`. |
| `gentoo_zh_distfiles` | Appends gentoo-zh distfiles to `GENTOO_MIRRORS`; `true`. |

| Mirror region | Site keys |
| --- | --- |
| `cn` | `ustc`, `nju`, `bfsu`, `tuna`, `zju`, `sdu`, `hust`, `sustech`, `hit`, `lzu`, `aliyun`, `netease`, `cernet`, `cicku-hk`, `planetunix-hk`, `xtom-hk`, `rackspace-hk`, `aditsu-hk`, `nchc-tw`, `cicku-tw`, `freedif-sg`, `cicku-sg`, `planetunix-sg` |
| `global` | `gentoo`, `osuosl` |

| `portage.binhost` key | Meaning and default or choices |
| --- | --- |
| `official` | Enables the official binary host; `true`. |
| `subarch` | Official binary-host subarchitecture; `x86-64`. |
| `community` | gentoo-zh channel: `off`, `stable`, or `unstable`; `off`. |

| `portage.overlays` item | Meaning |
| --- | --- |
| `name` | Overlay name; required. |
| `sync_uri` | Overlay synchronization URI; required. |

| `kernel` key | Meaning and default or choices |
| --- | --- |
| `source` | Kernel choice: `dist-bin`, `dist-source`, `cjk-bin`, `cjk`, or `xanmod`; `dist-bin`. |
| `package` | Overrides the package implied by `source`; `""`. |
| `version` | Version pin; `""` lets Portage choose the newest keyword-allowed version. |
| `dracut_modules` | Adds dracut modules required by the disk layout; `[]`. |
| `remote_unlock` | Initramfs SSH-unlock record; empty record by default. |

| `kernel.source` | Package and CJK status |
| --- | --- |
| `dist-bin` | `sys-kernel/gentoo-kernel-bin`; not a CJK kernel. |
| `dist-source` | `sys-kernel/gentoo-kernel`; not a CJK kernel. |
| `cjk-bin` | `sys-kernel/gentoo-cjk-kernel-bin`; CJK kernel. |
| `cjk` | `sys-kernel/gentoo-cjk-kernel`; CJK kernel. |
| `xanmod` | `sys-kernel/xanmod-kernel`; CJK kernel, built from source. |

| `kernel.remote_unlock` key | Meaning and default |
| --- | --- |
| `enabled` | Enables initramfs SSH unlocking; `false`. |
| `port` | Initramfs SSH port; `222`. |
| `address` | Static CIDR address; `""` uses DHCP. |
| `gateway` | Static-address gateway; `""`. |
| `interface` | Initramfs network interface; `""`. |

| `bootloader` key | Meaning and default or choices |
| --- | --- |
| `kind` | Bootloader: `grub`, `systemd-boot`, or `zfsbootmenu`; `grub`. |
| `firmware` | Firmware: `uefi` or `bios`; `uefi`. |
| `kernel_params` | Additional kernel command-line parameters; `[]`. |

| `packages` key | Meaning and default |
| --- | --- |
| `desktop` | Desktop profile name; `""` selects no desktop. |
| `applications` | Package-group names; `[]`. |
| `graphics` | Graphics-driver group names; `[]`; multiple groups fit hybrid hardware. |
| `display_manager` | Display-manager group; `""` selects console login. |
| `extra` | Package atoms merged after other selections; `[]`. |

| `disk` key | Meaning and default or choices |
| --- | --- |
| `graph` | Device graph represented by `[[disk.devices]]`; required. |
| `root` | Root graph-node identifier; required outside conversion; `""` uses the running layout in conversion. |
| `mode` | `partition`, `in-place`, `image`, or `dd`; `partition`. |
| `image` | Sparse image path in image mode; `""`. |
| `size` | Sparse image size in image mode; unset. |
| `wipe` | Disk-level wipe setting; `false`. |
| `source` | Prepared-image source in `dd` mode; `""`. |
| `source_format` | Source encoding: `raw`, `gz`, `xz`, `zst`, or `tar`; `raw`. |
| `destination` | Whole-disk `dd` destination; `""`. |

| Template input | Meaning and default or choices |
| --- | --- |
| `disk` | Whole-disk selector; required. |
| `layout` | `whole-disk`, `whole-disk-btrfs`, `whole-disk-zfs`, or `reuse`; `whole-disk`. |
| `firmware` | Template firmware; `uefi`. |
| `table` | Partition-table override; unset derives GPT for UEFI and MBR for BIOS. |
| `filesystem` | Root filesystem for non-Btrfs and non-ZFS whole-disk layouts; `xfs`. |
| `swap` | Swap-partition size; unset. |
| `passphrase_file` | Installing-system passphrase-file path; `""` leaves the layout unencrypted. |
| `pool` | ZFS pool name; `rpool`. |

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

Binary packages are optional. Disabling them keeps source builds available. The official binhost and the gentoo-zh binhost are separate options with separate trust configuration. One degradation path has end-to-end evidence: `TESTED.md` records `vm-binhost-fallback` on `e16f57a39199d` installing from source after its host served no package index, with the reason in the journal. That fixture no longer reproduces it — the same host answered on 2026-08-24 — so the path is recorded rather than currently exercised. A missing signature and an untrusted key have no end-to-end evidence and remain unverified.

A verified binhost runs `getuto`, imports its signing key, locally signs that key with `lsign`, and enables signature verification. A failed host, missing signature, or untrusted key degrades that binhost to source compilation and records the reason.

## Exit codes

| Code | `gentoo-install` |
| --- | --- |
| `0` | successful completion |
| `1` | configuration error, including an `argparse` usage error |
| `2` | preflight failure |
| `3` | integrity failure |
| `4` | download, external-command, OS or uncategorized installer failure |
| `5` | operator abort |

`bootstrap.sh` can also exit `1` before the Python CLI starts when its Python, required-command or root checks fail.
