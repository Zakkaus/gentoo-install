"""Install media the harness can boot, and how to get a shell on each."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

CACHE = Path.home() / "code/gentoo-install/lab/vm"


class MediaError(Exception):
    """The install medium is missing or does not contain what the profile claims."""


@dataclass(frozen=True)
class Medium:
    """An ISO plus everything needed to boot it headless and reach a root shell.

    The kernel and initramfs are extracted and passed to QEMU directly instead of
    booting the ISO's own bootloader, because the shipped menu entries carry no
    `console=ttyS0` and cannot be edited unattended.
    """

    name: str
    iso: Path
    volume_label: str
    kernel_in_iso: str
    initrd_in_iso: str
    root_prompt: str
    login_user: str | None = None
    login_password: str | None = None
    extra_cmdline: tuple[str, ...] = ()
    #: What to run before the installer, for a medium that ships without a
    #: command preflight needs. `bootstrap.sh` prints the package manager line
    #: and stops rather than running it, which is what an operator wants and
    #: what left every Debian run at `missing commands: mkfs.vfat sgdisk`.
    prepare: tuple[str, ...] = ()
    #: Set when the medium does not boot the dracut way. Alpine's initramfs
    #: takes `modules=` and finds its own media; it has no `root=live:`.
    boot_cmdline: tuple[str, ...] = ()
    #: Run after login on a medium whose live user is not root. Debian's live
    #: session logs in as `user`, and the installer needs root.
    become_root: str = ""

    def cmdline(self) -> str:
        base = self.boot_cmdline or (
            f"root=live:CDLABEL={self.volume_label}",
            "rd.live.image",
        )
        return " ".join((*base, "console=ttyS0,115200", *self.extra_cmdline))

    def boot_files(self) -> tuple[Path, Path]:
        """The kernel and initrd of this medium, extracted once and cached.

        Keyed by content, not by filename: a rolling release keeps its name, so
        replacing `opensuse-tumbleweed-rescue.iso` left the previous kernel and
        initramfs in the cache and the campaign reported a new matrix while
        booting the old medium.
        """
        if not self.iso.is_file():
            raise MediaError(f"{self.iso} does not exist")
        target = CACHE / self.name / self.iso.stem
        kernel = target / "kernel"
        initrd = target / "initrd"
        stamp = target / "source"
        wanted = self.source_stamp()
        if not (kernel.is_file() and initrd.is_file()) or _read(stamp) != wanted:
            target.mkdir(parents=True, exist_ok=True)
            stamp.unlink(missing_ok=True)
            _extract(self.iso, {self.kernel_in_iso: kernel, self.initrd_in_iso: initrd})
            stamp.write_text(wanted)
        return kernel, initrd

    def source_stamp(self) -> str:
        """What identifies the ISO's content, for the cache and for the record
        a campaign result carries. The digest is what actually decides; size and
        mtime only keep a gigabyte from being hashed on every run."""
        state = self.iso.stat()
        quick = CACHE / self.name / f"{self.iso.stem}.quick"
        seen = _read(quick).split()
        if len(seen) == 3 and seen[0] == str(state.st_size) and seen[1] == str(state.st_mtime_ns):
            return seen[2]
        digest = _sha256(self.iso)
        quick.parent.mkdir(parents=True, exist_ok=True)
        quick.write_text(f"{state.st_size} {state.st_mtime_ns} {digest}")
        return digest


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _sha256(path: Path) -> str:
    reader = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            reader.update(block)
    return reader.hexdigest()


def _extract(iso: Path, files: dict[str, Path]) -> None:
    argv = ["xorriso", "-osirrox", "on", "-indev", str(iso)]
    for source, target in files.items():
        target.unlink(missing_ok=True)
        argv += ["-extract", source, str(target)]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError(f"xorriso failed on {iso}: {result.stderr.strip()}")
    for target in files.values():
        if not target.is_file():
            raise MediaError(f"{iso} does not contain {target.name}")


#: Every image lives here, not beside the operator's own downloads: AGENTS.md
#: puts build artefacts under lab/, and three of these pointed at ~/Downloads
#: until a run failed on a path nobody had moved.
ISO_CACHE = CACHE / "iso"

GIGOS = Medium(
    name="gigos",
    iso=ISO_CACHE / "gig-os-20260807.iso",
    volume_label="Gig-OS",
    kernel_in_iso="/boot/kernel",
    initrd_in_iso="/boot/initrd",
    root_prompt=r"# ",
    login_user="root",
    login_password="live",
    extra_cmdline=(
        "systemd.unit=multi-user.target",
        "systemd.wants=sshd.service",
        # Without this the live session defaults to zh_CN and the login prompt is
        # localized, which no English pattern can match.
        "gigos.lang=en_US",
        "rd.driver.blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm",
    ),
)

OFFICIAL_MINIMAL = Medium(
    name="official-minimal",
    iso=ISO_CACHE / "install-amd64-minimal-20260712T170110Z.iso",
    volume_label="Gentoo-amd64-20260712",
    kernel_in_iso="/boot/gentoo",
    initrd_in_iso="/boot/gentoo.igz",
    # The official medium logs root in automatically and needs no credentials.
    root_prompt=r"livecd ~ #",
    extra_cmdline=("rd.live.dir=/", "rd.live.squashimg=image.squashfs", "cdroot", "nodhcp"),
)

GENTOO_CJK = Medium(
    name="gentoo-cjk",
    iso=ISO_CACHE / "gentoo-cjk-minimal-7.1.7-gentoo-cjk-dist-bin.iso",
    # Built from the official installcd spec, so it keeps that spec's volume
    # label; the kernel carries the CJK console fonts and zfs.
    volume_label="Gentoo-amd64-20260712",
    kernel_in_iso="/boot/gentoo",
    initrd_in_iso="/boot/gentoo.igz",
    root_prompt=r"livecd ~ #",
    extra_cmdline=("rd.live.dir=/", "rd.live.squashimg=image.squashfs", "cdroot", "nodhcp"),
)

ALPINE = Medium(
    name="alpine",
    iso=ISO_CACHE / "alpine-standard-3.24.1-x86_64.iso",
    volume_label="alpine-std 3.24.1 x86_64",
    kernel_in_iso="/boot/vmlinuz-lts",
    initrd_in_iso="/boot/initramfs-lts",
    # Alpine logs root in with no password and its shell prompt has no host.
    root_prompt=r"localhost:~#",
    login_user="root",
    login_password=None,
    boot_cmdline=("modules=loop,squashfs,sd-mod,usb-storage",),
)

DEBIAN = Medium(
    name="debian",
    iso=ISO_CACHE / "debian-live-13.6.0-amd64-standard.iso",
    volume_label="d-live 13.6.0 st amd64",
    kernel_in_iso="/live/vmlinuz",
    initrd_in_iso="/live/initrd.img",
    # The live session logs in as `user`; the installer needs root.
    root_prompt=r"user@debian:",
    login_user="user",
    login_password="live",
    become_root="sudo -i",
    boot_cmdline=("boot=live", "components", "username=user"),
    # `mkfs.vfat` and `sgdisk` are what preflight found missing on 13.6.0.
    prepare=("apt-get update -qq", "apt-get install -y dosfstools gdisk"),
)

ARCH = Medium(
    name="arch",
    iso=ISO_CACHE / "archlinux-2026.08.01-x86_64.iso",
    volume_label="ARCH_202608",
    kernel_in_iso="/arch/boot/x86_64/vmlinuz-linux",
    initrd_in_iso="/arch/boot/x86_64/initramfs-linux.img",
    root_prompt=r"root@archiso ~ #",
    # Auto-login is configured for tty1 only; the serial getty asks.
    login_user="root",
    login_password=None,
    boot_cmdline=("archisobasedir=arch", "archisolabel=ARCH_202608"),
)

FEDORA = Medium(
    name="fedora",
    iso=ISO_CACHE / "fedora-workstation-live-43.iso",
    volume_label="Fedora-WS-Live-43",
    kernel_in_iso="/boot/x86_64/loader/linux",
    initrd_in_iso="/boot/x86_64/loader/initrd",
    root_prompt=r"liveuser@",
    login_user="liveuser",
    login_password=None,
    become_root="sudo -i",
    # Without this the live image starts GDM and the serial console never
    # gets a shell.
    extra_cmdline=("systemd.unit=multi-user.target",),
    # 43 ships without sgdisk; preflight needs it for every GPT layout.
    prepare=("dnf install -y gdisk",),
)

OPENSUSE = Medium(
    name="opensuse",
    iso=ISO_CACHE / "opensuse-tumbleweed-rescue.iso",
    volume_label="openSUSE_Tumbleweed_Rescue_CD",
    kernel_in_iso="/boot/x86_64/loader/linux",
    initrd_in_iso="/boot/x86_64/loader/initrd",
    # The rescue image logs root in with no password at all.
    root_prompt=r"localhost:~ #",
    login_user="root",
    login_password=None,
    extra_cmdline=("rd.live.overlay.persistent", "rd.live.overlay.cowfs=ext4"),
)

MEDIA = {
    medium.name: medium
    for medium in (
        GIGOS,
        OFFICIAL_MINIMAL,
        GENTOO_CJK,
        ALPINE,
        DEBIAN,
        ARCH,
        FEDORA,
        OPENSUSE,
    )
}
