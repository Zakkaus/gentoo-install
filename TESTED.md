# Verification record

Every claim this project makes about a path working is one row here. A row
names what was exercised, the installer revision it ran at, and where it ran.
Nothing is listed as tested because it is implemented; the README carries the
boundary in prose and points here for the detail.

A run counts only when its recorded revision matches the installer, its
installation exit code is `0`, the installed system boots, and the post-boot
configuration checks pass. `tests/fixtures/` files exercise the configuration
model; their presence establishes nothing about an installed machine.

A package count in a row written before `d75aaa0f94806` is about twice what
the machine installed. `VerifyPackages` runs `emerge --pretend --quiet` before
anything is merged and it prints the same `[binary]` and `[ebuild]` lines the
merge does, and the journal counted both until that revision: one guest
recorded 63 packages for the 40 it installed. Rows from before it are not
comparable with rows after it.

Three modes are planned and this file has a section for each. All three exist
today; the third's own section says which of its paths carry a record.

## Mode 1: install onto a disk

The ordinary path: partition, format, unpack a stage3, configure, boot.

### Cluster records

| Revision | Fixtures |
|---|---|
| `a8bf2f3837b6` | `vm-luks`, `vm-mdraid`, `vm-xfs`, `vm-btrfs`, `vm-f2fs` |
| `073997aa74d2` | `vm-lvm` |
| `40ea3d90f1cc` | `openrc-sdboot` |
| `6ba5530fd3c8` | `vm-binpkg`, `vm-btrfs`, `vm-desktop`, `vm-gnome` |
| `304dffa41602` | `vm-xfs` |
| `7ac43a1d5050` | `vm-f2fs`, `vm-mdraid`, `vm-proxy-dead`, `vm-xfs`, `vm-zram` |
| `d2bed50eed48` | `vm-lvm`, `vm-sdboot`, `vm-unlock` — the first cluster record of the initramfs SSH unlock |
| `7cf09c2f9d9c` | `vm-btrfs`, `vm-binpkg`, `vm-luks`, `vm-mdraid`, `vm-f2fs`, `vm-zram`, `vm-zfs-encrypted` — the first cluster record of a ZFS root, of native ZFS encryption, and of the `gentoo-zh` overlay, which no earlier row's fixtures use |
| `08015b221d73` | `vm-sdboot`, `vm-cjk-kernel` — the first cluster record of the patched CJK console kernel and of `system.console_cjk` |
| `4ccd3aa34687` | `vm-xfs` |
| `0d7ddcb22b8e` | `vm-zram`, `vm-sdboot`, `vm-cjk-kernel`, `vm-f2fs`, `zfs-zbm`, `vm-openrc-desktop` — `zfs-zbm` is the first record of a ZFS root under ZFSBootMenu since the key file that image cannot open was removed from that path |
| `6d386174b295` | `vm-unlock`, `vm-mdraid`, `vm-xfs`, `zfs-zbm`, `vm-binpkg`, `vm-luks`, `btrfs-luks` — the first cluster record of a LUKS btrfs root, which took 196 minutes |
| `1aafd75c4359` | `openrc-sdboot`, `vm-lvm`, `vm-btrfs`, `vm-luks` — the first record of `openrc-sdboot` and `vm-lvm` since the installed-system login stopped being typed on a timer; both had failed in the four rounds before it |
| `7e79962bcb0a` | `vm-lvm`, `vm-xfs`, `openrc-sdboot`, `vm-btrfs`, `vm-f2fs`, `vm-mdraid` — `vm-bios` and `ext4-bios` failed in the same round against a prompt pattern that this revision predates, so that round says nothing about BIOS |
| `4f8bd68b8d09` | `ext4-bios`, `vm-bios-luks` — the first BIOS records since the blind serial login was measured rather than timed |
| `a2db147436af` | `ext2`, `ext3`, `mbr-edit`, `static-ip`, `vm-lvm`, `vm-xfs`, `vm-f2fs` — the first records of ext2, of ext3, of a machine that configures its own address, and of an edited MBR table on the cluster |
| `c345afa30b7c` | `ext2`, `vm-binpkg`, `openrc-sdboot`, `vm-mdraid`, `vm-luks`, `vm-btrfs`, `vm-sdboot`, `vm-zram` — eight of the round's ten; `ext3` was refused by its own login, which `3bdd6a0e78a8` addresses, and the source-built kernel was still compiling when the round was ended |
| `77edc8352186` | `vm-luks`, `vm-zram`, `vm-zfs`, `zfs-zbm`, `vm-desktop` — five of ten; `ext3` and `vm-unlock` failed at the installed system's login, `vm-sdboot` lost a marker to a console session and `vm-gnome` was ended mid-compile |
| `e2986684da66` | `ext3`, `vm-unlock`, `vm-zfs-mirror`, `vm-raidz` — `ext3` and `vm-unlock` are the two the login work fixed: `login` prints `Maximum number of tries exceeded` instead of a third refusal, and it prints all three in the installed system's own locale |
| `7fcc7edcec6b` | `openrc-sdboot`, `mbr-edit` — `mbr-edit`'s first record at all: it installed in 46.6 minutes in an earlier round and lost the login every time |
| `18f150282cb1` | `vm-lvm`, `static-ip`, `ext2` — three of three |
| `3692e4b29743` | `vm-zfs`, `vm-f2fs`, `vm-luks`, `vm-lvm`, `vm-mdraid`, `vm-unlock`, `vm-xfs`, `vm-zram` — eight of ten; `ext3` was refused by its own login again, which `2849e42a151a` addresses, and `vm-desktop` was ended mid-compile by a reconnect grant that replaced the run's remaining ceiling with fifteen minutes |
| `2849e42a151a` | `ext3` — the first record of `ext3` since the harness waits for the name's echo before the password prompt, the race that had left `Password:` in the buffer and typed the password into the name field |
| `02141f7a9902` | `vm-xfs`, `vm-luks`, `vm-sdboot`, `vm-greetd`, `vm-zfs-encrypted` — five of the round's six. `vm-greetd` is the first record of greetd and tuigreet, and the first of ibus-pinyin under a display manager; the check before it refused every machine for a word its own configuration file carries in a comment. The sixth, `vm-binhost-fallback`, also passed and is not counted: `--site` had moved it off the host whose index answers 404 |
| `ce95e7df72cd` | `static-ip`, `vm-btrfs`, `vm-mdraid`, `vm-zram`, `vm-f2fs` |
| `74ba411eaea9` | `vm-lvm`, `vm-unlock` |
| `cc84e970ddba` | `vm-cjk-kernel`, `vm-binpkg`, `zfs-zbm`, `vm-gnome`, `ext3` |
| `8dbfc12f35e0` | `ext4-bios`, `vm-xfs`, `vm-lvm`, `vm-f2fs`, `vm-btrfs` — five of six. `vm-xfs` is the first record since a UEFI guest whose console delivered nothing gets a second boot, which is what ended its previous round at 2.2 minutes; `zbm-unlock` lost its initramfs address to a lease that expired under a live guest, which `52b373dca9a2` addresses |
| `6739b3abf2db` | `vm-zram`, `static-ip`, `vm-mdraid` |
| `52b373dca9a2` | `vm-binpkg`, `openrc-sdboot`, `zbm-unlock`, `ext2`, `zfs-zbm` — five of six. `zbm-unlock` is the first record of a ZFSBootMenu pool opened over ssh; `vm-unlock` lost 10.31.0.151 to a round that started 92 minutes later and its initramfs answered `Duplicate address detected`, which `ffcb8561250d` addresses |
| `2dd80ae4534f` | `vm-proxy-dead`, `vm-cjk-kernel`, `vm-raidz`, `vm-zfs-encrypted`, `ext4-bios`, `vm-zfs-mirror` — six of six |
| `2dd80ae4534f` | `ext3`, `vm-lvm`, `vm-xfs` — three of three, and the only round that ran **without** `--distfiles`: the guests fetched from `nju` itself, so this row is the one that says the direct path works |
| `5dbf23a387c3e` | `vm-sdboot`, `mbr-edit`, `vm-gnome`, `ext4-bios`, `vm-raidz`, `vm-zfs-encrypted`, `vm-zfs-mirror`, `zfs-zbm` — eight of ten, and every ZFS layout this installer offers in one round: a raidz, a mirror, an encrypted pool and one booted by ZFSBootMenu. The two that failed were a GRUB editor screen read before the kernel line arrived and a resolver that stopped answering for six packages |
| `7962d2205e401` | `static-ip`, `vm-luks` — `vm-luks` is the fixture that stopped mid-write on an idle node in the round before, and installed without incident here in 42.9 minutes |
| `34cb398890c7d` | `ext2`, `vm-zram`, `vm-btrfs`, `vm-mdraid`, `mbr-edit`, `ext4-bios`, `vm-f2fs`, `vm-sdboot`, `vm-lvm` — nine of ten. The tenth, `vm-luks`, stopped mid-write while unpacking the kernel sources with its node at 0%, which is the first time a verdict could say a stalled guest was not a starved one |
| `1980922a0c04e` | `vm-cjk-kernel`, `static-ip`, `vm-zfs` — three of three, and the two that matter are re-measurements: `static-ip` and `vm-zfs` were both ended earlier the same day at `cpu 0.00` mid-compile on nodes reading 94% to 100%, and both installed without incident when the cluster had cores to give, in 32.1 and 80.6 minutes |
| `e16f57a39199d` | `ext3`, `vm-xfs`, `vm-luks`, `openrc-sdboot`, `vm-binhost-fallback`, `vm-binpkg` — six of the round's ten so far. `vm-binhost-fallback` is the first record with a machine behind the binary-host fallback: its journal holds one `degraded` event, `gentoo answered no package index at https://mirror.xtom.com.hk/gentoo/releases/amd64/binpackages/23.0/x86-64`, and the install finished from source in 56.4 minutes |
| `43b5737b04478` | `openrc-sdboot`, `static-ip`, `ext3`, `vm-luks`, `vm-zram`, `vm-source-kernel` — six of six, and the first `vm-source-kernel` that ever finished: 400.3 minutes, 44 packages from a binary host and 8 compiled beside the kernel it built from source, 54 operations. Its three earlier attempts died reading the results back, twice on a marker that arrived after the reader had moved on and once on 900 seconds of collection for an 80 MB log; `#696` and `#736` are those two |
| `d75aaa0f94806` | `vm-cjk-kernel`, `vm-binpkg`, `vm-luks`, `ext2`, `vm-xfs`, `ext4-bios`, `ext3`, `vm-mdraid`, `openrc-sdboot`, `vm-desktop` — ten of ten, and the first round in which a configuration that turns both binary hosts off installed without one: `ext2`, `ext3` and `ext4-bios` each ran nine emerges carrying `--usepkg=n --getbinpkg=n`, and none of them recorded the `no package index at https://distfiles.gentoo.org/...` degradation their predecessors did |
| `b0578c15c0645` | `vm-zram`, `vm-btrfs`, `vm-zfs-mirror`, `ext4-bios`, `vm-raidz`, `vm-sdboot`, `vm-lvm`, `vm-f2fs` — eight of eight, and the first round whose checks read two features back from the machine: `vm-zram` answered `/dev/zram0` to `swapon --show` and `zramctl`, and `zpool status rpool` drew `raidz1-0` on one guest and `mirror-0` on another. The same two fixtures at `5dbf23a387c3e` never ran that command |
| `63a006da72efa` | `vm-greetd`, `vm-binhost-fallback`, `vm-unlock`, `vm-lvm`, `mbr-edit`, `vm-zfs-encrypted`, `vm-raidz` — seven of eight. `zbm-unlock` unlocked its pool and then hung on the second connection carrying `zfs get keystatus`, which is the race `#750` closes: ZFSBootMenu's dropbear goes with the image it boots from |
| `ffcb8561250d` | `vm-btrfs`, `mbr-edit`, `vm-f2fs`, `vm-bios-luks`, `vm-sdboot`, `vm-greetd` — six of six. `vm-bios-luks` is the first BIOS fixture recorded on the cluster rather than under local QEMU, and `vm-greetd` the second greetd record |

Every row from `08015b221d73` onward ran `--region cn --site nju --sync
webrsync --distfiles http://10.31.0.2/gentoo`, with one exception named in the
table above, so what they establish about
mirror selection is limited to that region, that site and a distfiles cache on
the guests' own segment. The parameters the earlier rows used are not
recorded.

### Records from QEMU on one machine

These cover what the cluster could not drive when each row was written. That
is no longer the whole BIOS story: on 2026-08-25 `vm-bios` (22.5m), `mbr-edit`
(29.2m), `vm-bios-luks` (29.6m) and `ext4-bios` (40.4m) all installed and
booted on the cluster at `122d1cf603a85` and `ed67574b26765`. What a BIOS guest
there still cannot show is its firmware and bootloader screens — nothing
reaches the serial port before the kernel starts, and a non-root API token has
neither a screenshot endpoint nor a way to pass firmware arguments — so a
failure before the kernel is diagnosable only on this machine.

| Revision | Fixtures | Why not the cluster |
|---|---|---|
| `304dffa41602` | `vm-bios`, `vm-bios-luks`, `ext4-bios`, `mbr-edit` | BIOS serial console |
| `15d45598637a` | `zfs-zbm`, `vm-proxy`, `vm-proxy-http` | the proxy fixtures reach a proxy on the host through QEMU's user-mode network, which a bridged cluster guest does not have |
| `4d8512a496d` | `vm-proxy` (SOCKS5, password), `vm-proxy-http` | 57 operations, 93 packages from a binary host, 14 compiled |
| `7b08c47f383a` | `vm-bios` | BIOS serial console; 53 operations, 55 packages from a binary host, 20 compiled, then the disk it wrote booted, logged in on the console and passed every installed-state check |
| `90516741321b` | `vm-proxy` (SOCKS5, password), `vm-proxy-http` | the proxy runs on the host, which a bridged cluster guest cannot reach; each installed 57 operations, 69 packages from a binary host and 46 compiled, and each disk then booted and passed every installed-state check |
| `f466b89d8206` | `vm-bios-luks` | BIOS serial console; 57 operations, 59 packages from a binary host, 20 compiled, then the disk it wrote booted, unlocked its root and passed every installed-state check |
| `6ec5d840ff973` | `vm-binpkg`, killed at the stage3 and finished with `--resume` | the cluster has no way to interrupt an install; the resumed run skipped 8 operations an earlier one had recorded as done, reached `[53/53]`, and the disk it wrote booted, mounted its layout and had no failed unit. Zero skipped is a failure there: it would mean the resume repartitioned a disk it had already installed onto |
| `122d1cf603a85` | `vm-binpkg` plain and `vm-binpkg` killed at the stage3 and finished with `--resume`, in one campaign invocation | the cluster has no way to interrupt an install. Both finished in 5.7 minutes; the resumed one skipped 8 operations an earlier run had recorded as done and installed 58 operations, 30 packages from a binary host and 11 compiled, and each disk then booted and passed every installed-state check. Zero skipped would be a failure: it would mean the resume repartitioned a disk it had already installed onto |
| `d05e75025c080` | `vm-image` | the same image mode at the revision the campaign runs, through one `--and-boot` invocation: 54 operations, 30 packages from a binary host, 10 compiled, and `losetup -Pf` inside the live medium answered `loop1p1 vfat` and `loop1p2 ext4`. Nothing booted it |
| `cbd22d88417a6` | `vm-image` | the product is a file on a filesystem this runner mounts on the spare disk, and the cluster has none to offer; 54 operations, 55 packages from a binary host, 12 compiled, then the image was attached with `losetup -Pf` inside the live medium and answered `loop1p1 vfat` and `loop1p2 ext4`, the two filesystems the layout declares. Nothing booted it: reading a file on the guest's own disk is what this record covers |

### Historical records

Revisions `a71f91b4735469bae8ec76af170201acb967a5fe` and
`f7257793f95df4b21ebf2ac6a775a343f6205f1b` on the amd64 Gentoo minimal ISO
covered selected UEFI and BIOS installations, systemd and OpenRC, ext4, btrfs,
xfs, LUKS2, LVM, mdraid, Plasma and the official binhost. Later
installation-path changes made them historical evidence only.

Revision `b931ef46fc15ed50385f70467f2bfb0a8d1fd154`, dated 2026-08-11, covers
one installation and boot from each of Arch Linux, openSUSE, Debian, Fedora and
a self-built gentoo-cjk minimal ISO. The gentoo-cjk record uses ZFS and
ZFSBootMenu; the other four use ext4.

### Install media

| Medium | Revision | Result |
|---|---|---|
| `alpine-standard-3.24.1`, UEFI, systemd | `0827931289d0` | root shell on the serial console in 59s; 56 operations, 57 packages from a binary host, 12 compiled; booted with no failed unit |
| `alpine-standard-3.24.1`, BIOS, OpenRC, ext4 | `bc8ab3a0edcf` | root shell in 59s; 51 operations, 29 from a binary host, 51 compiled; booted with no failed unit |
| `install-amd64-minimal-20260816T170110Z`, UEFI, systemd, xfs | `86cca05b314f` | installed `vm-xfs` in one run: 56 operations, 61 packages from a binary host, 12 compiled, then the disk it wrote booted, logged in on the console and passed every installed-state check |

Both target media have a record at a current revision, the first either has:

| Medium | Fixture | Revision | Result |
|---|---|---|---|
| `gig-os-20260818`, UEFI | `vm-binpkg` | `d3493b505430f` | 55 operations, 30 packages from a binary host, 11 compiled; the disk it wrote booted, mounted its layout and had no failed unit |
| `install-amd64-cjk-minimal-20260820T064553Z`, UEFI | `vm-cjk-kernel` | `b03f8eafa7501` | 59 operations, 30 packages from a binary host, 11 compiled; same result |

Both ISOs are in `lab/vm/iso/`, fetched from `iso.gentoozh.org`. `media.py` had
named files the download site no longer carries — `gig-os-20260807` against
`20260818`, and a CJK minimal eleven days older — and `GENTOO_CJK` also carried
a `volume_label` stamped with the build date, so repinning the ISO without the
label would have produced a medium that boots and is then refused for being the
wrong one. Both are read out of the ISO now and a test holds them to it.

What these rows do not cover: the Gig-OS ISO also runs the installer by script
from its own session, and that path has no record.

### Network modes

The IPv4-only, IPv6-only and dual-stack check stops before disk access. It
covers address-family detection, `bootstrap.sh --missing-commands` and stage3
pointer retrieval. It does not cover stage3 download, repository
synchronization, binhost access, package installation, or a booted target
system on those network modes.

### Proxy

Focused unit and plan coverage exists for SOCKS5 DNS mode, redaction in dry-run
output and in published configuration, and the credential-free endpoint
retained in the installed system.

The negative direction has a revision-tagged cluster run: the `vm-proxy-dead`
fixture points the proxy at a port where nothing listens and the install stops
at the stage3 download with `Connection refused`, so a run that reached the
mirror would show the proxy had been bypassed.

At `122d1cf603a85` both positive directions ran under the campaign rather than
by hand, against a proxy the harness starts itself: `vm-proxy` (SOCKS5 with a
password) in 8.9 minutes and `vm-proxy-http` in 8.7 minutes, each installing
and then booting the disk it wrote and passing every installed-state check.
Before that revision the run was skipped unless somebody had started a proxy,
so nothing measured this direction automatically.

What that pair establishes and what it does not: an install finishing does not
by itself prove the fetches went through the proxy, because QEMU's user-mode
network also gives the guest a direct route. It is the pair that carries the
claim — the same configuration stops when the proxy is dead and finishes when
it answers, and the guest's own `make.conf` names `socks5h://10.0.2.2:1080`.

An HTTP or HTTPS proxy that requires a password cannot check the tree
snapshot's signature: `emerge-webrsync` hands gemato only the credential-free
endpoint. dirmngr has no SOCKS support at all, so under SOCKS5 the key refresh
needs a direct route to the keyserver.

## Mode 2: convert a running system in place

Replaces the userland of a running distribution instead of partitioning a disk.

### Records

| Revision | Machine | Result |
|---|---|---|
| `bcc090fab621` | Debian 12 genericcloud, ext4 root on a partition, BIOS | converted and booted as Gentoo |
| `71e751cf14a1` | Arch Linux cloud image, btrfs root, BIOS | converted and booted as Gentoo; `/swap/swapfile` carried into the new fstab |
| `83b5a2fb4fa17` | `vm-convert` on the cluster: this installer built `xfsbox`, then converted it | rebooted as `convertedbox` and answered the shared installed-state checks to the end; `ok vm-convert 70.3m`, the first cluster record of the whole path |
| `639c5cd4069f` | Debian 12 genericcloud, ext4 root on a partition, **UEFI** | converted, booted as Gentoo, and kept `/home` |
| `6fa74c99ab94` | Fedora 41 Cloud, btrfs root with subvolumes, UEFI | converted, booted as Gentoo, and carried `/home` and `/var` into the new fstab as subvolumes of the same filesystem |

The first two were read on the machine afterwards: `uname -r` gave
`6.18.43-gentoo-dist-bin`, `emerge` was present and the original
distribution's package manager was gone, the root device was unchanged, and
the run's log was in `/var/log/gentoo-install`.

The third is the first on UEFI and the first to check that the conversion
kept anything: a marker written under `/home` before the swap is read back
after the machine boots, since `/home` surviving is the promise this mode
rests on and one tuple held it. `efibootmgr -v` in that guest put `Gentoo
… \EFI\Gentoo\grubx64.efi` first in `BootOrder`; what the next boot then
read is a separate question, and until `6fa74c99ab94` the answer was a
variable store that had kept nothing, so that machine booted through the
removable path rather than through the entry the row names.

The fourth is the first Fedora conversion to boot at all, and the first on a
btrfs root whose `/home` and `/var` are subvolumes. It needed the firmware
fix: with the store discarded, the machine started `Boot0001 "Fedora"` and
Fedora's own GRUB stopped at `file '/vmlinuz-6.11.4-301.fc41.x86_64' not
found`, because the conversion had removed the kernel that entry names. Its
own boot order, read the same way, was `BootOrder: 0004,0003,0001,0000,0002`
with `Boot0004* Gentoo` first.

It is also the first that could pass. The three rounds before it exited zero
and produced a machine that stops in GRUB, because a conversion unmounts
nothing and its last writes — the bootloader configuration above all — were
still in the page cache when the machine went away.

All four records above are from QEMU on one machine.

### Refusals with a machine behind them

Each of these was met on a real machine and is refused before anything is
written.

| Machine | Refusal |
|---|---|
| Alpine 3.21 cloud image | root is a whole disk with no partition, so `grub-install --target=i386-pc` answers `will not proceed with blocklists` |
| Alpine 3.21 cloud image, before `apk add util-linux` | `findmnt` and `lsblk` are absent, so the layout reads back empty |
| Fedora 41 cloud image | a replaced directory that is its own mount used to be refused; it is now replaced by its contents instead |

### On the cluster

| Revision | Fixture | Result |
|---|---|---|
| `2a37189a898a0` | `vm-convert` | the same again in 116.1 minutes, and the first at a revision that counts GRUB's modules before the reboot: the count passed, so this machine had them and booted |
| `d32b9d4aa6fb4` | `vm-convert` | the same again in 125.8 minutes, and the first row where the `/etc` sentinel was measured: a file written there before the swap was gone afterwards, so `/etc` was replaced rather than merged |
| `d97a5eb98c743` | `vm-convert` | the same again in 95.4 minutes, at a revision carrying the verdict that names the installer's own reason. The home-marker check that changed the same day belongs to the local conversion runner and is not in this row |
| `ce95e7df72cd` | `vm-convert` | installed Gentoo onto a cluster guest, converted that machine in place with the same driver CD, and the converted machine booted as `convertedbox` and answered every installed-state check, in 137.6 minutes |

The cluster runs a conversion by installing a system first and converting what
that produced, so these rows cover both halves and the reboot between them.

The conversion is not reliable. Six cluster conversions have reached the
reboot: five booted, and one stopped at `grub rescue>` for a missing
`/boot/grub/x86_64-efi/normal.mod` while the conversion itself exited `0`. Two
earlier ones never reached a login prompt at revisions that did not record the
console, so what they stopped at is unknown. The open defect is row 238 of
`docs/tasks.md`.

### The interface

| Revision | What ran | Result |
|---|---|---|
| `d348174548035` | `python3 -m tests.vm.tui --lang en` on the official minimal medium, UEFI | opened all nineteen rows of the menu on an 80x24 serial console and read each screen back: 20 screens drawn, no line wider than the terminal, and every row returned to the menu. Nothing was installed; this record covers the interface, not an installation |
| `1df424c0d0840` | the same walk with `--lang ja`, `--lang ko` and `--lang zh-CN` | 20 screens and no finding each, which with the two rows below covers every language the interface offers. Both scripts are drawn in double-width cells, and Korean adds spacing between words, so the row that runs out of terminal is theirs rather than the English one |
| `1df424c0d0840` | the same walk with `--lang zh-TW` | 20 screens and no finding again, on a medium carrying no CJK font: every title was drawn from the catalog rather than the English source. What this measures is width, because a Han character takes two cells and a translated label is the one that overflows 80 columns |

The walk is what `FakeScreen` cannot do. It found the Hostname screen had no
way back, because backspace deletes while the field has content and escape is
Cancel, which ends the run: `#785`.

### Not verified

Nothing named. UEFI conversion and a btrfs subvolume root left this list when
`6fa74c99ab94` converted Fedora 41 and the machine booted; the `vm-convert`
cluster fixture left it with the row above.

## Mode 3: boot into RAM, then install or write an image

Implemented and wired into `cli.py`, and every path in it has a record:
machines rebooted into both environments it arms, and an image written and
read back. `--ram` fetches the Gentoo CJK ISO, `--lowram` the
Alpine netboot archive; both place a kernel where the machine's own bootloader
reads one, deliver the configuration and the installer's own tree in a cpio
appended to the initramfs, and arm a single boot. `--bypass` replaces the
default entry instead of arming one boot.

`dd` writes a prepared image over a whole disk from inside that environment,
streamed rather than staged, and is offered only on a live medium or in a
memory environment because writing over the running root is writing over
yourself. It takes nothing over afterwards: the disk carries the image's own
layout and bootloader. One disk has been written this way and read back.

| Revision | What ran | Result |
|---|---|---|
| `6762496a41dd` | `--lowram` on a Debian 12 genericcloud machine, UEFI | armed one boot, left the default entry alone, rebooted, came up in Alpine from RAM with the delivered configuration and asked `install or shell>` |
| `2f03f85139d2` | `--ram` on the same machine | the same, through the Gentoo CJK ISO: `root=live:CDLABEL=Gentoo-CJK-amd64-20260813`, the live image copied into RAM, `livecd login: root (automatic login)`, then `install or shell>` |
| `3bdd6a0e78a8` | `dd` on the official minimal medium: `vm-dd-raw` and `vm-dd-gz` | wrote a 4 MiB image onto a second disk and read it back byte-for-byte, both formats, in 23s each |
| `7fcc7edcec6b` | `--lowram` on a Debian 12 genericcloud machine, UEFI, answering `install` | installed Gentoo from inside the memory environment, powered the machine off, booted the disk it had written and passed the shared installed-state checks: `the installed system booted, mounted its layout and has no failed unit` |
| `7fcc7edcec6b` | `--lowram` on a second machine, its armed entry's initramfs removed | the delivered screen never appeared, and the cloud system's own marker and `/etc/os-release` `ID` were read back on the two boots that followed |
| `6502c213269e` | the same, with the guarded entry | GRUB read the machine's own menu on the failed boot itself and booted Debian from it: `the failed one-shot returned to the cloud system` at 237.3s, `the second reboot still reached the cloud system` at 246.0s |
| `6fa74c99ab94` | `--bypass`, `--lowram`, on a Debian 12 genericcloud machine, UEFI | re-measured once the firmware kept its variables: `the first boot came up in the memory environment` at 97.0s and `the second boot came up in the memory environment` at 114.7s, so replacing the default entry survives a power cycle |
| `f5b6d5e142fd` | `--ram` on a Debian 12 genericcloud machine, UEFI, answering `install` | installed Gentoo from inside the Gentoo CJK ISO environment and passed the same installed-state checks: `logged into the installed system (console)` at 532.8s, `the installed system booted, mounted its layout and has no failed unit` |
| `e430b3fadbfe` | the same, once the firmware kept its variables | installed from the Gentoo CJK ISO environment again and booted the disk it wrote: `logged into the installed system (console)` at 531.8s, `memory install booted its disk and passed the shared installed-state checks` at 534.5s |
| `3e085f078c9f` | `--lowram` on a Debian 12 genericcloud machine, UEFI, answering `install` | installed from the Alpine netboot environment and booted the disk it wrote: `logged into the installed system (console)` at 457.6s, `memory install booted its disk and passed the shared installed-state checks` at 460.1s. The run before it stopped at `make a vfat filesystem on espfs labelled ESP` with `vdc1` and `vdc2` in `/proc/partitions` and neither in `/dev`, so this path is not reliable yet |

Every row above installed `vm-ram`, the fixture `tests/vm/ram.py` hands the
environment unless `--config` names another.

| a tree verified to hold `49844ad5883ad` and `8d17a0c4a9924` | `--lowram` on a Debian 12 genericcloud machine, UEFI, three rounds | two installed Gentoo from the memory environment and booted the disk they wrote (`memory install booted its disk and passed the shared installed-state checks`); the third stopped at `mkfs.vfat -F 32 -n ESP /dev/vdc1` with `No such file or directory`, `Probe.wait_for` having returned that node a moment earlier. **The path installs; it fails about one round in three on the device nodes.** |

| `468ac83d267f2` | `--lowram` with the memory credentials in the payload rather than on the kernel command line, Debian 12 genericcloud, UEFI, three rounds | all three installed Gentoo from the memory environment and booted the disk they wrote. Three rounds rather than one because the device-node fault in the row above fails about one round in three, so a single pass would not distinguish this change from that fault. |

| the tree that became `#980` | `--lowram` on a Debian 12 genericcloud machine, UEFI, five rounds | all five installed Gentoo from the memory environment and booted the disk they wrote. The same five fixtures on the tree before the change went 2 pass, 3 fail. The device-node fault recorded above is the one this closes: mdev is the `/proc/sys/kernel/hotplug` helper, so the kernel spawns one per event and a remove event's handler unlinked the node `mkfs` was opening. |

Five rounds after the change and five before it, because a single pass could
not separate the fix from a fault that only appeared in three rounds out of
five. The five that passed were measured **after** the diagnostic commands were
removed: they ran two extra commands before every `mkfs`, and this fault is
entirely about timing, so the run that carried them could not stand as the
record.

The row above is three runs rather than one because the failure is
intermittent, and one green round would have recorded a fix that is not one:
`partx --update` and `mdev -sf` were added for exactly this symptom and the
second round failed with both of them in the tree.

The row names two commits rather than a revision because the cluster runner
printed none when it was recorded: what the tree held was read before the
campaign started. Both runners print the revision now — `tests/vm/run.py` at
the top of a run and `tests/vm/cluster.py` as `installer revision: <sha>
dirty=<n> driver-sha256=<sum>` before the first guest — so a row recorded
after that can name one.

| `01278fe0860cf` | `vm-binpkg` on the cluster, `ok vm-binpkg 18.3m` | the binhost path installs and boots with the official entry the stage3 ships removed before any trust operation and written back only when the official source is trusted, and with the official key's local signature read back rather than inferred from `getuto`'s exit status. **What this does not establish:** that a machine which *fails* that verification degrades to source — the fixture's official source is trusted, so the degradation branch did not run. |

| `5a9b9f220e651` | `vm-luks` on a local UEFI guest, the first run with the bounded mount check | installed and booted, and `mounted its layout` here means what it says: the check derives each configured mountpoint and its filesystem type from the configuration rather than matching any line containing a slash. This row also carries the rerun-cleanup change in `ReleaseTarget` and the btrfs scratch mount, whose **re-entry** path is still unverified — an ordinary install exercises the release at its head, not a second attempt over a first one's leftovers. |

| `a8dce6c253876` | eight local guests on `official-minimal a058ca1e178c63d1`, one per fixture: `ext4-bios`, `btrfs-luks`, `vm-btrfs`, `vm-xfs`, `ext2`, `vm-luks`, `openrc-sdboot`, `vm-zram` | every one installed, booted, mounted the layout its configuration describes and reached a running system with no failed unit. The set is the first full round after `PYTHONPATH` stopped reaching the commands the installer runs, and it spans the branches an install forks on: three openrc, two LUKS, two BIOS boots and six UEFI, one zram. **What this does not establish:** anything about the Gig-OS Live ISO or the cluster, since all eight ran on the official minimal image under local qemu. |

| `2c9728a81d97e` | `openrc-sdboot`, `vm-zram` and `vm-luks` on local UEFI guests, all on `official-minimal a058ca1e178c63d1` | the three fixtures that between them exercise every destination `destinations()` now derives: openrc's `/etc/env.d/02locale`, `/etc/conf.d/keymaps`, `/etc/conf.d/hostname` and `/etc/conf.d/hwclock`, the systemd zram generator, and the crypttab entries. All three installed, booted, mounted the layout their configuration describes and reached a running system with no failed unit, so the indirection did not move where a real machine's files land. **What this does not establish:** the static address path, which `static-ip` covers only on the cluster — a local guest presents `enp0s2` where the fixture pins `ens18`. |

| `7f55e45a53102` | `vm-unlock`, `vm-sdboot`, `ext4-bios` and `btrfs-luks` on local guests, `ext4-bios` under BIOS | four of four installed and booted with no failed unit. `vm-unlock` is the record behind the authorized-keys change: its journal holds `write SHA256:Hv58Q4mbOPnZ69ggsTohAVI63u1xQl1rBDpIJ+HYcbQ into authorized_keys for root` and its console holds `installed root unlocked by remote SSH session`, so the `0600` write through `destinations()` reaches a machine that then authenticates with that key. **What this does not establish:** that the booted system accepts the key — the unlock happens in the initramfs, and the check that reads the file back off the installed machine landed after this round. |

| `7f55e45a53102` | `static-ip`, `vm-binpkg`, `vm-lvm`, `vm-mdraid` and `vm-f2fs` on the cluster, five of six | each installed, booted and passed its post-boot checks. `static-ip` is the record that the static address path works where the interface it pins exists: the same fixture cannot pass on a local guest, which presents `enp0s2` against the `ens18` it names. The sixth, `vm-binhost-fallback`, failed and is described above: its host served packages, so it measured nothing it exists to measure. |

| `8d4d7d730ad6c` and the four revisions before it | eleven local guests driven through `tests/vm/campaign.py` rather than `run.py`: `vm-proxy-dead`, `mbr-edit`, `vm-greetd`, `vm-bios-luks`, `vm-openrc-desktop`, `vm-gnome`, `vm-lvm`, `vm-mdraid`, `vm-bios`, `vm-f2fs`, `ext3` | all eleven passed. `vm-proxy-dead` passes by failing and is the first run of the expectation table added in `#996` against a real guest rather than a constructed `Outcome`. `ext3` ran alone and took 11.0m, which is what separates its two cluster stalls from the fixture. **What the three desktop rows do not establish:** that a session draws. The checks read the packages, the service state and the configuration; the harness has no display, so nothing in these runs saw a screen. |

| `0528b451a10c4` | `vm-zfs`, `vm-btrfs`, `vm-xfs`, `vm-cjk-kernel` and `vm-convert` on the cluster, five of six | each installed, booted and passed its post-boot checks. `vm-convert` at 71.4m is the record for the in-place conversion, the heaviest path this installer has. The sixth, `ext3`, ended as `ERROR`: the console was silent for 1200s with the guest at 0% CPU and the node idle, mid-compile of `sys-boot/grub`. |

| `8d4d7d730ad6c` | `vm-zfs-encrypted`, `vm-zfs-mirror`, `vm-raidz`, `vm-desktop` and `vm-source-kernel` on the cluster, five of six | all five installed and booted. `vm-source-kernel` took 322.6m, which is the record for a kernel built from source. `ext3` ended the same way as the round before, on a different node — two nodes, one stopping point, and the same fixture installs and boots in 11.0m on a local guest. **What this does not establish:** why. The pair of cluster failures is recorded in `docs/tasks.md` row 402 with the counters. |

| `332c852cfcb64` | `zfs-zbm` on a local UEFI guest, the run behind the `canmount` fix | installed, booted, and `/home/zakk/.ssh/authorized_keys` is there with `600` on the file, `700` on `.ssh` and `755` on the home directory. `zfs list` reports `zpcala/ROOT/gentoo/home` at 256K against 192K on the run before, which is the same dataset holding the files rather than being covered while they went to the root dataset. **What this does not establish:** `zbm-unlock`, whose guest was killed by `earlyoom` in the same round and which also carries the remote-unlock path. |

| `12d9eb6aaae2e` | `vm-proxy-dead`, `vm-binhost-fallback` and `vm-proxy` on local guests, the three entries of `tests/vm/expectations.py` | each produced the verdict its entry asks for: `vm-proxy-dead` passes by failing, `vm-binhost-fallback` passes because it recorded the degradation — its journal names the 404 and all 35 packages read `compiled` — and `vm-proxy` reads `SKIP` because this workstation runs no SOCKS5 listener. Before `#1013` that last one printed `FAIL`. **What this does not establish:** that the fixture's premise holds anywhere else. The same fixture at this revision took binary packages from the same URL on the cluster, so what it measures depends on the network the guest is on. |

| `5d42214c87366` | `vm-binpkg` killed partway and finished with `--resume`, on a local UEFI guest | the first measurement of the resume path. `ok 6.0m`, and the run's `skipped.txt` reads `8`: the second attempt skipped eight operations the first had finished rather than partitioning a disk it had already installed onto. Two defects had kept it unreachable — `--only` selected one run per fixture, and once both were selected they shared a work directory. **What this does not establish:** resuming across a reboot, or across a different installer revision, which `README.md` already marks as unverified. |

**Rows recorded before `5a9b9f220e651` and saying `mounted its layout` overstate
one of their checks.**
`tests/vm/installed.py`'s `mounts` check expects the pattern `/` and is matched
with `re.search`, so any `findmnt` output containing a slash satisfies it — a
machine with only a root filesystem and none of the configured layout passes.
The other checks in the same set do real work: `os-release`, `fstab`, `locale`,
the failed-unit list and the per-filesystem device readings each name a value
the machine computed. What is not established by these rows is that every
configured mountpoint was mounted with the filesystem the configuration asked
for. The rows are otherwise unchanged; the qualification belongs here rather
than in a silent correction of each one.

**No row here is newer than `3e085f078c9f`, and that is a property of the
harness rather than of these paths.** `9e88ecda73ea9` reworded the delivered
profile's question from `install or shell> ` to `install now? [yes/no] ` and
left `tests/vm/ram.py` waiting for the old text, so every `--ram` and
`--lowram` run after it timed out on a machine that had in fact come up
correctly. Nobody ran one, so nobody knew. The question now has one
definition both sides read, and neither environment has been measured again
since that fix: what these rows establish stops at the revisions they name.

Both environments install, and they needed different work to get there: the
CJK ISO answered `live system: gentoo` with `python3.14`, `mount`, `tar` and
`swapon` already on it and installed nothing of its own, while the Alpine
netboot root had to be given an interpreter and thirteen commands first.

The `--lowram` install record took four more runs, and every one of them stopped on
something the environment lacks rather than on the installer: no interpreter
(`this installer needs python 3.11 or newer; found: none`), no tools
(`missing commands: blkid findmnt gpg … sgdisk … xz`), no firmware variables,
and underneath all three, no kernel modules at all — `initramfs-init` adds
`modloop`, `mdev`, `hwdrivers` and `sshd` to the runlevels only when no apkovl
was loaded or `/etc/.default_boot_services` is inside the one that was. The
install then stopped once more at `/dev/vdc2 did not appear within 15s` on a
machine whose `/proc/partitions` held both new partitions: Alpine runs mdev,
and `udevadm settle` there has no daemon behind it.

That fallback record was measured across the power cycle the harness performs
between guests, and the failed boot itself did not recover: GRUB answered
``error: file `/gentoo-install-ram/initramfs' not found.`` and `Press any key
to continue...`, so a machine armed from far away waited at that menu. The
entry is now guarded — it loads the kernel only when both files are there and
rereads the machine's own `grub.cfg` otherwise — and the row below is the same
fixture measured again with the guard: the console holds `INITRAMFS-BROKEN`,
then ``Booting `Debian GNU/Linux'`` on that same boot, with no keypress and no
reset in between.

The two paths deliver the payload differently and each was measured on its
own: `--ram` through a dracut `pre-pivot` hook on a medium that logs root in
by itself, `--lowram` through an apkovl on one that asks for a login. The
`--ram` record followed the `--lowram` one by twenty minutes, because by then
everything the two share had been found.

The first record took nine runs, each one step further than the last, and
every step was a defect nothing in the tree could see: the image written to a
124 MiB esp, `tar` restoring ownership onto vfat, the archive deleted before
the entry read its name, an instrument that changed between two readings of
the boot order, a GRUB entry naming `/boot` on a machine that boots the esp,
a payload delivered by a dracut hook to an initramfs that runs none, and an
appended cpio starting three bytes off a four-byte boundary.

Five mechanisms underneath it were measured one guest at a time rather than
assumed, and each contradicted a reading of the source that preceded it:

| What was asked | How it was answered |
|---|---|
| Does an appended newc cpio reach the live system? | A `cmdline` hook inside the appended segment printed to `/dev/kmsg` 3.5s into the boot. `lsinitrd` reads only the first segment and shows nothing, so the static tool answers the opposite way |
| Does `iso-scan/filename` find an ISO stored as a file? | The console printed `Copying live image to RAM...` and reached `livecd login:` with the ISO on a plain ext4 filesystem |
| Is the disk holding the ISO free afterwards? | `/proc/mounts` held 17 mounts and none named `vda`, so `--ram` needs to unmount nothing before erasing. The design text said the opposite, derived from reading `dmsquash-live-root` |
| Where can the first screen be hung? | Not `/etc/local.d`: that medium's `/etc/init.d/local` discards the output and runs with no controlling terminal, so the question was invisible and the `read` beside it answered itself. `/root/.bash_profile` is where the medium's auto-login and its ssh login both arrive |
| Does an appended segment reach the initramfs at all? | Only from a four-byte boundary. Alpine's `initramfs-lts` is 27951899 bytes, 3 mod 4, and a guest booted with the segment appended to it printed no sign of the payload; one zero byte in front of the same segment answered `Loading user settings from /gentoo-install.apkovl.tar.gz: ok.` |

What has no record yet: a machine that goes on to install Gentoo from inside
the environment it came up in, and a deliberately failed arming proving the
machine returns to its own system because the entry is one-shot.

## The interface alone, from the menu to a booted system

An operator agent was given only `session screen` and `session key`, the
sentence describing what to build, and no access to this source. It read the
menu, answered the rows, started the install, and the machine was then booted
from the disk it had written and read out on its own console. Revision
`d7a1f864d8359`.

| Spec | Asked for | On the booted machine |
|---|---|---|
| 5 | the whole disk, xfs root, 4 GiB swap | `/dev/vda3 xfs /`, host `lab5`, `LANG=zh_TW.UTF-8`, `systemctl is-system-running` answered `running`, no failed unit, 10.272s to userspace |
| 6 | partition by hand: 512 MiB EFI, 20 GiB ext4 root | `/dev/vda2 ext4 /`, host `lab6`, `LANG=zh_TW.UTF-8`, `running`, no failed unit, 11.562s |
| 7 | two disks, root on ZFS across both, ZFSBootMenu | `rpool/ROOT/gentoo zfs /`, the pool `ONLINE` with both members, host `lab7`, `running`; 74 operations, 28 packages from a binary host and 24 compiled |
| 9 | the whole disk on btrfs, KDE Plasma | `/dev/vda2[/@] btrfs /`, host `lab9`, `LANG=zh_TW.UTF-8`, `running`, no failed unit, 14.528s; 350 packages from a binary host and 43 compiled |

Spec 2 installed in the same round and is not counted here: its root is a
cryptodisk and the passphrase prompt at boot has no record yet. Spec 8 built a
BIOS and OpenRC machine that reached runlevel 3 with every service `[ ok ]`,
which the check could not read back until `_open_a_serial_login` stopped
counting Gentoo's commented inittab entries as a login.

Three defects in the interface were found by the same round and are fixed:
a refused public key quoted what it had read, a timezone chosen after going
back ended the run on an index error, and a conversion left `Drive` required
with the screen behind it refusing to open, so the install could never start.

## Not covered by any record

A desktop session that actually draws, and ibus outside GNOME. `vm-greetd`,
`vm-gnome` and `vm-openrc-desktop` all install and boot, so the packages, the
service state and the configuration are covered; the harness has no display
and no run has ever seen a screen. Binary-host failure
fallback left this list on `e16f57a39199d`: `vm-binhost-fallback` installed
against a host whose index answered 404, recorded the degradation and finished
from source. **It is back on this list as of `7f55e45a53102`**: the same host
served packages that day, the run finished with nothing degraded, and the
check added in `#996` reported it rather than recording an ordinary binary
package install as coverage. A fixture whose failure is borrowed from the
outside world stops measuring on a day nobody chooses.

CJK text-console rendering is not covered either, and the `vm-cjk-kernel` row
above does not cover it: that run establishes that the patched kernel merges,
that `system.console_cjk` reaches the target and that the machine boots with no
failed unit. Whether the console draws a CJK glyph is a different question, and
one build answered it wrongly with only `CONFIG_FONT_CJK_16x16` while every
check passed.
