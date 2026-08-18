# Verification record

Every claim this project makes about a path working is one row here. A row
names what was exercised, the installer revision it ran at, and where it ran.
Nothing is listed as tested because it is implemented; the README carries the
boundary in prose and points here for the detail.

A run counts only when its recorded revision matches the installer, its
installation exit code is `0`, the installed system boots, and the post-boot
configuration checks pass. `tests/fixtures/` files exercise the configuration
model; their presence establishes nothing about an installed machine.

Three modes are planned and this file has a section for each. Only the first
two exist today, and the third's table is empty on purpose.

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

Every row from `08015b221d73` onward ran `--region cn --site nju --sync
webrsync --distfiles http://10.31.0.2/gentoo`, so what they establish about
mirror selection is limited to that region, that site and a distfiles cache on
the guests' own segment. The parameters the earlier rows used are not
recorded.

### Records from QEMU on one machine

These cover what the cluster cannot drive. A BIOS guest there writes nothing to
its serial port before the kernel starts, and neither a screenshot endpoint nor
a way to pass firmware arguments is available to a non-root API token.

| Revision | Fixtures | Why not the cluster |
|---|---|---|
| `304dffa41602` | `vm-bios`, `vm-bios-luks`, `ext4-bios`, `mbr-edit` | BIOS serial console |
| `15d45598637a` | `zfs-zbm`, `vm-proxy`, `vm-proxy-http` | the proxy fixtures reach a proxy on the host through QEMU's user-mode network, which a bridged cluster guest does not have |
| `4d8512a496d` | `vm-proxy` (SOCKS5, password), `vm-proxy-http` | 57 operations, 93 packages from a binary host, 14 compiled |

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

The official Gentoo minimal ISO and the Gig-OS ISO are not tested here: they
run the installer by script, and the gentoo-cjk minimal ISO record above covers
that path.

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
| `639c5cd4069f` | Debian 12 genericcloud, ext4 root on a partition, **UEFI** | converted, booted as Gentoo through its own firmware entry, and kept `/home` |

The first two were read on the machine afterwards: `uname -r` gave
`6.18.43-gentoo-dist-bin`, `emerge` was present and the original
distribution's package manager was gone, the root device was unchanged, and
the run's log was in `/var/log/gentoo-install`.

The third is the first on UEFI and the first to check that the conversion
kept anything: a marker written under `/home` before the swap is read back
after the machine boots, since `/home` surviving is the promise this mode
rests on and one tuple held it. Its firmware chose `Boot0001* Gentoo
… \EFI\Gentoo\grubx64.efi`, first in `BootOrder`, read from the machine
rather than assumed.

It is also the first that could pass. The three rounds before it exited zero
and produced a machine that stops in GRUB, because a conversion unmounts
nothing and its last writes — the bootloader configuration above all — were
still in the page cache when the machine went away.

All three records are from QEMU on one machine.

### Refusals with a machine behind them

Each of these was met on a real machine and is refused before anything is
written.

| Machine | Refusal |
|---|---|
| Alpine 3.21 cloud image | root is a whole disk with no partition, so `grub-install --target=i386-pc` answers `will not proceed with blocklists` |
| Alpine 3.21 cloud image, before `apk add util-linux` | `findmnt` and `lsblk` are absent, so the layout reads back empty |
| Fedora 41 cloud image | a replaced directory that is its own mount used to be refused; it is now replaced by its contents instead |

### Not verified

UEFI conversion, a btrfs subvolume root end to end, and the `vm-convert`
cluster fixture.

## Mode 3: boot into RAM, then install or write an image

Implemented and wired into `cli.py`; **no machine has been rebooted into the
environment it arms.** `--ram` fetches the Gentoo CJK ISO, `--lowram` the
Alpine netboot archive; both place a kernel where the machine's own bootloader
reads one, deliver the configuration and the installer's own tree in a cpio
appended to the initramfs, and arm a single boot. `--bypass` replaces the
default entry instead of arming one boot.

`dd` writes a prepared image over a whole disk from inside that environment,
streamed rather than staged, and is offered only on a live medium or in a
memory environment because writing over the running root is writing over
yourself. It takes nothing over afterwards: the disk carries the image's own
layout and bootloader. No machine has been written this way either.

| Revision | What ran | Result |
|---|---|---|
| — | — | nothing end to end yet |

Four mechanisms underneath it were measured one guest at a time rather than
assumed, and each contradicted a reading of the source that preceded it:

| What was asked | How it was answered |
|---|---|
| Does an appended newc cpio reach the live system? | A `cmdline` hook inside the appended segment printed to `/dev/kmsg` 3.5s into the boot. `lsinitrd` reads only the first segment and shows nothing, so the static tool answers the opposite way |
| Does `iso-scan/filename` find an ISO stored as a file? | The console printed `Copying live image to RAM...` and reached `livecd login:` with the ISO on a plain ext4 filesystem |
| Is the disk holding the ISO free afterwards? | `/proc/mounts` held 17 mounts and none named `vda`, so `--ram` needs to unmount nothing before erasing. The design text said the opposite, derived from reading `dmsquash-live-root` |
| Where can the first screen be hung? | Not `/etc/local.d`: that medium's `/etc/init.d/local` discards the output and runs with no controlling terminal, so the question was invisible and the `read` beside it answered itself. `/root/.bash_profile` is where the medium's auto-login and its ssh login both arrive |

Rows land in the table above for: a machine that boots into the RAM environment
and installs Gentoo normally; a machine that writes an image with the `dd`
mode; and, for each, a deliberately failed run proving the machine returns to
its original system because the boot entry is one-shot.

## Not covered by any record

greetd desktop sessions, ibus outside GNOME, binary-host failure fallback, and
focused configuration tests for ext2 and ext3.

CJK text-console rendering is not covered either, and the `vm-cjk-kernel` row
above does not cover it: that run establishes that the patched kernel merges,
that `system.console_cjk` reaches the target and that the machine boots with no
failed unit. Whether the console draws a CJK glyph is a different question, and
one build answered it wrongly with only `CONFIG_FONT_CJK_16x16` while every
check passed.
