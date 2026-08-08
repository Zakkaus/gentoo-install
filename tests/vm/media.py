"""Install media the harness can boot, and how to get a shell on each."""

from __future__ import annotations

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
        if not self.iso.is_file():
            raise MediaError(f"{self.iso} does not exist")
        # Keyed by ISO filename so a new build never boots the previous extraction.
        target = CACHE / self.name / self.iso.stem
        kernel = target / "kernel"
        initrd = target / "initrd"
        if not (kernel.is_file() and initrd.is_file()):
            target.mkdir(parents=True, exist_ok=True)
            _extract(self.iso, {self.kernel_in_iso: kernel, self.initrd_in_iso: initrd})
        return kernel, initrd


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


GIGOS = Medium(
    name="gigos",
    iso=Path.home() / "Downloads/gig-os-20260803.iso",
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
    iso=Path.home() / "Downloads/install-amd64-minimal-20260712T170110Z.iso",
    volume_label="Gentoo-amd64-20260712",
    kernel_in_iso="/boot/gentoo",
    initrd_in_iso="/boot/gentoo.igz",
    # The official medium logs root in automatically and needs no credentials.
    root_prompt=r"livecd ~ #",
    extra_cmdline=("rd.live.dir=/", "rd.live.squashimg=image.squashfs", "cdroot", "nodhcp"),
)

ALPINE = Medium(
    name="alpine",
    iso=Path.home() / "Downloads/alpine-standard-3.24.1-x86_64.iso",
    volume_label="alpine-std 3.24.1 x86_64",
    kernel_in_iso="/boot/vmlinuz-lts",
    initrd_in_iso="/boot/initramfs-lts",
    # Alpine logs root in with no password and its shell prompt has no host.
    root_prompt=r"localhost:~#",
    login_user="root",
    login_password=None,
    boot_cmdline=("modules=loop,squashfs,sd-mod,usb-storage",),
)

#: Downloaded rather than shipped, so they live under lab/ with the other
#: build artefacts instead of beside the user's own images.
ISO_CACHE = CACHE / "iso"

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
)

MEDIA = {
    medium.name: medium
    for medium in (GIGOS, OFFICIAL_MINIMAL, ALPINE, DEBIAN, ARCH, FEDORA)
}
