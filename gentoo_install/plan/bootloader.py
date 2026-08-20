# SPDX-License-Identifier: GPL-2.0-or-later
"""GRUB, systemd-boot and ZFSBootMenu.

A ZFS root gets no GRUB artefacts at all. The Live ISO's Calamares run installs
GRUB and then deletes what it wrote, because its module order cannot be changed;
this installer decides before it writes, so nothing has to be undone.
"""

from __future__ import annotations

import ipaddress

import re

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from ..errors import CommandFailed, ConfigError, InvalidLayout, NothingToBoot
from ..model import compat
from ..model.config import Bootloader, DiskMode, Firmware, InitSystem, InstallConfig, RemoteUnlock
from ..model.device import (
    Existing,
    DeviceId,
    Filesystem,
    MdRaid,
    Mountpoint,
    Partition,
    PartitionRole,
    PartitionTable,
    ZfsDataset,
    ZfsPool,
    Subvolume,
)
from .operations import CommandOutput, Context, Operation, Stage, answered
from .portage import Emerge, InstallMode, PortageConfigKind, WritePortageConfig


@dataclass(frozen=True, kw_only=True)
class VerifyPackageUse(Operation):
    """Verify requested flags against the selected ebuild metadata."""

    stage: Stage = Stage.PORTAGE
    atom: str
    flags: tuple[str, ...]

    def describe(self) -> str:
        return f"verify {self.atom} USE {' '.join(self.flags)} from ebuild metadata"

    def apply(self, context: Context) -> None:
        visible = context.run_in_target(["portageq", "best_visible", "/", self.atom], check=False)
        if not isinstance(visible, CommandOutput) or visible.returncode != 0:
            raise ConfigError(f"cannot resolve {self.atom} for USE verification")
        cpv = visible.strip()
        iuse = context.run_in_target(
            ["portageq", "metadata", "/", "ebuild", cpv, "IUSE"], check=False
        )
        required = context.run_in_target(
            ["portageq", "metadata", "/", "ebuild", cpv, "REQUIRED_USE"], check=False
        )
        if not isinstance(iuse, CommandOutput) or iuse.returncode != 0:
            raise ConfigError(f"cannot read metadata for {cpv}")
        if not isinstance(required, CommandOutput) or required.returncode != 0:
            raise ConfigError(f"cannot read REQUIRED_USE for {cpv}")
        available = {flag.lstrip("+-") for flag in iuse.split()}
        missing = tuple(
            flag.lstrip("-") for flag in self.flags if flag.lstrip("-") not in available
        )
        if missing:
            raise ConfigError(f"{cpv} does not declare USE flags: {', '.join(missing)}")

BOOTLOADER_PACKAGES: Final[dict[Bootloader, tuple[str, ...]]] = {
    Bootloader.GRUB: ("sys-boot/grub",),
    Bootloader.SYSTEMD_BOOT: (),
    Bootloader.ZFSBOOTMENU: ("sys-boot/zfsbootmenu",),
}

#: `bootctl` comes from systemd on a systemd system and from systemd-utils on
#: an openrc one, and the two block each other, so the init decides which.
BOOTCTL_PACKAGE: Final[dict[InitSystem, str]] = {
    InitSystem.SYSTEMD: "sys-apps/systemd",
    InitSystem.OPENRC: "sys-apps/systemd-utils",
}

#: How long the boot menu waits. The same five seconds `GRUB_TIMEOUT` uses, so
#: the two bootloaders behave alike from the operator's side.
MENU_SECONDS: Final[int] = 5

#: Writing an NVRAM entry needs this, and only UEFI has NVRAM to write to.
EFI_PACKAGE: Final[str] = "sys-boot/efibootmgr"

#: Where generate-zbm writes, and the path firmware boots with no NVRAM entry.
#: It names the image after the kernel, so the name is looked up, not assumed.
ZBM_DIRECTORY: Final[str] = "EFI/zbm"
FALLBACK_IMAGE: Final[str] = "EFI/BOOT/BOOTX64.EFI"


@dataclass(frozen=True, kw_only=True)
class BootFacts:
    """Boot inputs derived once from the device graph and configuration."""

    root: DeviceId | None
    dataset: str
    root_parameters: tuple[str, ...]
    containers: tuple[DeviceId, ...]
    arrays: tuple[DeviceId, ...]
    keymap: str
    unlock_parameters: tuple[str, ...]
    pool: str
    pool_dataset: str

#: ZFSBootMenu's dracut tree is separate from the installed system's tree.
ZBM_REMOTE_CONFIG: Final[PurePosixPath] = PurePosixPath(
    "/etc/zfsbootmenu/dracut.conf.d/dropbear.conf"
)
ZBM_NETWORK_CONFIG: Final[PurePosixPath] = PurePosixPath(
    "/etc/cmdline.d/dracut-network.conf"
)
ZBM_CONFIG: Final[PurePosixPath] = PurePosixPath("/etc/zfsbootmenu/config.yaml")
ZBM_KEY_DIRECTORY: Final[PurePosixPath] = PurePosixPath("/etc/dropbear")
ZBM_AUTHORIZED_KEYS: Final[PurePosixPath] = ZBM_KEY_DIRECTORY / "root_key"
#: `ssh-keygen -m PEM` refuses ed25519 — "Saving key failed: invalid format" —
#: and ZFSBootMenu's own documentation names only these two.
ZBM_HOST_KEY_TYPES: Final[tuple[str, ...]] = ("rsa", "ecdsa")


#: The firmware boot entry, which is an accelerator and not the boot path.
#: `EFI/BOOT/BOOTX64.EFI` is what the firmware falls back to with no entry at
#: all, and both bootloaders write it before asking for one.
NVRAM_ENTRY: Final[str] = "efi boot entry"

#: What proves the built image can answer an unlock: the acl the daemon reads,
#: or the hook that starts it. Not the word `dropbear`, which the host-key
#: paths under `etc/dropbear` match on an image that authenticates nobody.
ZBM_UNLOCK_MARKERS: Final[tuple[str, ...]] = ("authorized_keys", "dropbear-start")
REMOTE_UNLOCK_IMAGE: Final[str] = "zfsbootmenu remote unlock"


def _try_the_nvram_entry(context: Context, argv: list[str]) -> None:
    """Ask the firmware for a boot entry, and carry on when it refuses.

    An NVRAM that is full or read-only is the machine's, not the install's,
    and a machine whose fallback image is already in place boots without one.
    Stopping there loses a complete install to a variable nobody needs.
    """
    try:
        context.run_in_target(argv)
    except CommandFailed as error:
        context.degrade(NVRAM_ENTRY, f"the firmware refused a boot entry: {error}")


@dataclass(frozen=True, kw_only=True)
class InstallGrub(Operation):
    stage: Stage = Stage.BOOTLOADER
    firmware: Firmware
    esp: PurePosixPath | None
    #: Devices whose containing disks receive GRUB on a BIOS machine.
    boot_devices: tuple[DeviceId, ...]
    force: bool = False
    write_nvram: bool = True

    def describe(self) -> str:
        if self.esp is not None:
            return f"install GRUB for {self.firmware.value} on the esp at {self.esp}"
        # Every disk, not "the boot disk": the BIOS branch writes a boot sector
        # to the containing disk of each boot device, and a mirrored root has
        # two. The devices rather than the disks, because a disk is resolved
        # from the machine and `describe` has none.
        under = ", ".join(str(one) for one in self.boot_devices)
        disks = "disk" if len(self.boot_devices) == 1 else "disks"
        return f"install GRUB for {self.firmware.value} on the {disks} under {under}"

    def apply(self, context: Context) -> None:
        force = ["--force"] if self.force else []
        if self.firmware is Firmware.UEFI and self.esp is not None:
            efi = ["grub-install", *force, "--target=x86_64-efi", f"--efi-directory={self.esp}"]
            # The removable-media path first, because it is the one that boots
            # without an NVRAM entry: firmware that loses its entry, and every
            # firmware that never had one, boots only that.
            context.run_in_target([*efi, "--removable"])
            if self.write_nvram:
                _try_the_nvram_entry(context, [*efi, "--bootloader-id=Gentoo"])
        else:
            installed: set[str] = set()
            for device in self.boot_devices:
                disk = context.containing_disk(device)
                if disk in installed:
                    continue
                installed.add(disk)
                context.run_in_target(
                    ["grub-install", *force, "--target=i386-pc", disk]
                )
        context.run_in_target(["grub-mkconfig", "--output", "/boot/grub/grub.cfg"])
        # grub-mkconfig exits 0 having found no kernel, and the machine then
        # drops back to the firmware menu with nothing to boot.
        # grep exits 1 when nothing matched, which is a count of zero; any
        # other code is a probe that did not run, and its message must not be
        # read as a file holding no entry.
        entries = answered(
            context.run_in_target(
                ["grep", "--count", "^menuentry", "/boot/grub/grub.cfg"], check=False
            ),
            "grub.cfg could not be counted",
            allowed=(0, 1),
        )
        if not entries.isdigit() or int(entries) == 0:
            raise NothingToBoot("grub.cfg has no menu entry; /boot holds no kernel")
        # grub-mkconfig writes `grub.cfg.new` and copies it over `grub.cfg`
        # last (`util/grub-mkconfig.in`), so the new file surviving means the
        # config in place is the one that was already there. A converted Debian
        # machine kept its own single `menuentry` that way, counted it as
        # success, and booted to `Failed to boot both default and fallback
        # entries` against a kernel the conversion had replaced.
        gone = context.run_in_target(
            ["test", "!", "-e", "/boot/grub/grub.cfg.new"], check=False
        )
        if not isinstance(gone, CommandOutput) or gone.returncode != 0:
            raise NothingToBoot(
                "grub-mkconfig left /boot/grub/grub.cfg.new behind, so the "
                "configuration in place is the one it was meant to replace"
            )


@dataclass(frozen=True, kw_only=True)
class WriteGrubDefaults(Operation):
    stage: Stage = Stage.BOOTLOADER
    kernel_params: tuple[str, ...]
    #: `/boot` inside a LUKS container. `grub-install` refuses outright without
    #: this, saying so in as many words.
    cryptodisk: bool
    serial: tuple[str, int] | None
    #: Gentoo's dracut sets `hostonly_cmdline="no"` and detects the chroot's own
    #: root, so only `rd.luks.uuid` tells the initramfs what to open.
    luks: tuple[DeviceId, ...] = ()
    #: Arrays the initramfs assembles. /etc/mdadm.conf alone is not enough:
    #: dracut assembles nothing without `rd.md.uuid` on the command line.
    arrays: tuple[DeviceId, ...] = ()
    #: The initramfs keymap. An encrypted root asks for its passphrase there,
    #: and dracut is built with hostonly_cmdline="no", so the command line is
    #: the only thing that says which keyboard is attached.
    keymap: str = ""

    def describe(self) -> str:
        extra = []
        if self.cryptodisk:
            extra.append("cryptodisk enabled")
        if self.serial is not None:
            extra.append(f"its menu on {self.serial[0]}")
        listed = f", {' and '.join(extra)}" if extra else ""
        return f"write /etc/default/grub with cmdline {' '.join(self.kernel_params) or 'empty'}{listed}"

    def apply(self, context: Context) -> None:
        # `GRUB_CMDLINE_LINUX` reaches every entry and `_DEFAULT` only the
        # default one, so what the initramfs needs to find the root at all goes
        # in the first: a recovery entry with no `rd.luks.uuid` waits for a
        # device that never appears. Recovery is disabled below, which made the
        # split invisible rather than unnecessary.
        needed = (
            *luks_parameters(context, self.luks),
            *array_parameters(context, self.arrays),
            *keymap_parameters(self.keymap),
        )
        lines = [
            f'GRUB_CMDLINE_LINUX="{" ".join(needed)}"',
            f'GRUB_CMDLINE_LINUX_DEFAULT="{" ".join(self.kernel_params)}"',
            # The constant, not a second five: the comment on `MENU_SECONDS`
            # promises the two bootloaders wait the same time, and a literal
            # here made that promise something nothing could keep.
            f"GRUB_TIMEOUT={MENU_SECONDS}",
            "GRUB_DISABLE_RECOVERY=true",
        ]
        if self.cryptodisk:
            lines.append("GRUB_ENABLE_CRYPTODISK=y")
        if self.serial is not None:
            port, baud = self.serial
            unit = port.removeprefix("ttyS")
            # Both: serial alone leaves a machine with a monitor dark until
            # the kernel starts.
            lines += [
                'GRUB_TERMINAL_INPUT="console serial"',
                'GRUB_TERMINAL_OUTPUT="console serial"',
                f'GRUB_SERIAL_COMMAND="serial --unit={unit} --speed={baud}"',
            ]
        context.write(PurePosixPath("/etc/default/grub"), "\n".join(lines) + "\n")


@dataclass(frozen=True, kw_only=True)
class RequestBootctl(Operation):
    """The `boot` flag is what provides `bootctl` and the EFI stub, and both
    packages that can provide them keep it behind that flag.

    Written in the portage phase, not the bootloader phase:
    `installkernel[systemd-boot]` depends on this same flag and is merged with
    the kernel, several phases earlier.
    """

    stage: Stage = Stage.PORTAGE
    package: str

    #: `sys-apps/systemd-utils` refuses `boot` without it: its REQUIRED_USE is
    #: `boot? ( kernel-install )`, and an openrc install with systemd-boot
    #: stopped at `has unmet requirements` before anything was built. Harmless
    #: on `sys-apps/systemd`, which has the flag and enables it with `boot`.
    FLAGS: Final[tuple[str, ...]] = ("boot", "kernel-install")

    def describe(self) -> str:
        return f"ask for {self.package}[{','.join(self.FLAGS)}], which is what provides bootctl"

    def apply(self, context: Context) -> None:
        VerifyPackageUse(atom=self.package, flags=self.FLAGS).apply(context)
        WritePortageConfig(
            kind=PortageConfigKind.USE,
            name="systemd-boot",
            lines=(f"{self.package} {' '.join(self.FLAGS)}",),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class InstallSystemdBoot(Operation):
    stage: Stage = Stage.BOOTLOADER
    esp: PurePosixPath

    def describe(self) -> str:
        return f"install systemd-boot on the esp at {self.esp}"

    def apply(self, context: Context) -> None:
        context.run_in_target(["bootctl", f"--esp-path={self.esp}", "install"])


@dataclass(frozen=True, kw_only=True)
class ShowTheBootMenu(Operation):
    """`bootctl install` writes no `loader.conf`, and systemd-boot's own
    default for `timeout` is 0: no menu at all, straight into the default
    entry. An encrypted machine went from firmware to the passphrase prompt
    with no way to pick an older kernel.
    """

    stage: Stage = Stage.BOOTLOADER
    esp: PurePosixPath
    seconds: int = MENU_SECONDS

    def describe(self) -> str:
        return f"show the boot menu for {self.seconds}s in {self.esp}/loader/loader.conf"

    def apply(self, context: Context) -> None:
        context.write(
            self.esp / "loader" / "loader.conf",
            f"timeout {self.seconds}\nconsole-mode keep\n",
        )


@dataclass(frozen=True, kw_only=True)
class GenerateHostId(Operation):
    """The pool records the hostid it was created under, which is the installing
    system's. `zgenhostid -f` alone writes a fresh random one, so the value is
    read from the installing system and written into the target."""

    #: The kernel stage, not the bootloader one: dracut's zfs module copies
    #: `/etc/hostid` into the initramfs, so writing it after `RebuildInitramfs`
    #: left the image without it and the pool imported under a hostid the
    #: target does not have.
    stage: Stage = Stage.KERNEL

    def describe(self) -> str:
        return "copy the installing system's hostid into the target so the pool imports"

    def apply(self, context: Context) -> None:
        hostid = context.run(["hostid"]).strip()
        context.run_in_target(["zgenhostid", "-f", hostid])


@dataclass(frozen=True, kw_only=True)
class ConfigureZfsBootMenuRemoteAccess(Operation):
    """Files and dedicated keys embedded by ZFSBootMenu's dracut build."""

    stage: Stage = Stage.BOOTLOADER
    unlock: RemoteUnlock
    authorized_keys: tuple[str, ...]

    def describe(self) -> str:
        return (
            f"write {ZBM_REMOTE_CONFIG} and {ZBM_NETWORK_CONFIG}, authorise keys at "
            f"{ZBM_AUTHORIZED_KEYS}, and generate dedicated host keys under {ZBM_KEY_DIRECTORY}"
        )

    def apply(self, context: Context) -> None:
        context.write(
            ZBM_AUTHORIZED_KEYS,
            "".join(f"{key}\n" for key in self.authorized_keys),
            mode=0o600,
        )
        for keytype in ZBM_HOST_KEY_TYPES:
            key = ZBM_KEY_DIRECTORY / f"ssh_host_{keytype}_key"
            if context.read(key):
                continue
            # PEM lets dracut-crypt-ssh convert dedicated keys without copying
            # the installed system's private host identity into the EFI image.
            context.run_in_target(
                [
                    "ssh-keygen",
                    "-q",
                    "-g",
                    "-N",
                    "",
                    "-m",
                    "PEM",
                    "-t",
                    keytype,
                    "-f",
                    str(key),
                ]
            )
        context.write(
            ZBM_NETWORK_CONFIG,
            f"ip={_ip_parameter(self.unlock)} rd.neednet=1\n",
        )
        context.write(
            ZBM_REMOTE_CONFIG,
            "".join(
                f"{line}\n"
                for line in (
                    # `network-legacy` by name: dracut's `40network` picks the
                    # first implementation already included, and the others all
                    # reach `systemd`, which this image does not carry.
                    'add_dracutmodules+=" crypt-ssh network-legacy "',
                    # Their checks pass on the installed system, but
                    # ZFSBootMenu deliberately omits the systemd module.
                    'omit_dracutmodules+=" systemd-networkd systemd-battery-check "',
                    f'install_optional_items+=" {ZBM_NETWORK_CONFIG} "',
                    # The module's own default is `rsa ecdsa ed25519`, and it
                    # generates each one with `ssh-keygen -m PEM`, which refuses
                    # ed25519. Its `install()` returns at that failure, after
                    # the two host keys and before the acl and the start hook,
                    # which is exactly what the built image held.
                    f'dropbear_keytypes="{" ".join(ZBM_HOST_KEY_TYPES)}"',
                    f'dropbear_port="{self.unlock.port}"',
                    f"dropbear_rsa_key={ZBM_KEY_DIRECTORY}/ssh_host_rsa_key",
                    f"dropbear_ecdsa_key={ZBM_KEY_DIRECTORY}/ssh_host_ecdsa_key",
                    f"dropbear_acl={ZBM_AUTHORIZED_KEYS}",
                )
            ),
        )


@dataclass(frozen=True, kw_only=True)
class InstallZfsBootMenu(Operation):
    """No `zpool.cache`: the boot path is hostid plus an import scan, which
    survives the pool being seen under a different device path."""

    stage: Stage = Stage.BOOTLOADER
    pool: str
    dataset: str
    esp: PurePosixPath
    #: The esp partition itself, so the boot entry names the right one rather
    #: than defaulting to partition 1.
    esp_device: DeviceId
    kernel_params: tuple[str, ...]
    #: Whether the image was configured to answer an unlock over ssh.
    unlocks_remotely: bool = False
    write_nvram: bool = True
    #: `org.zfsbootmenu:commandline` is the command line of the system ZBM
    #: boots, not of ZBM itself, which is what prompts for a passphrase.
    serial: tuple[str, int] | None

    def describe(self) -> str:
        return (
            f"write {ZBM_CONFIG}, build ZFSBootMenu into {self.esp}/{ZBM_DIRECTORY}, "
            f"and boot {self.dataset} from it"
        )

    def _config(self) -> str:
        kernel = ""
        if self.serial is not None:
            port, baud = self.serial
            # Both consoles, and the serial one last: /dev/console follows the
            # last `console=`, and a machine with a monitor still shows a menu.
            kernel = (
                "Kernel:\n"
                f'  CommandLine: "ro loglevel=4 console=tty1 console={port},{baud}"\n'
            )
        return (
            "Global:\n"
            "  ManageImages: true\n"
            f"  BootMountPoint: {self.esp}\n"
            "  InitCPIO: false\n"
            "Components:\n"
            "  Enabled: false\n"
            "EFI:\n"
            f"  ImageDir: {self.esp}/EFI/zbm\n"
            "  Versions: false\n"
            "  Enabled: true\n"
            "  Stub: /usr/lib/systemd/boot/efi/linuxx64.efi.stub\n"
            f"{kernel}"
        )

    def _say_if_the_image_cannot_unlock(self, context: Context, image: str) -> None:
        """Read the built image rather than trusting that dracut obeyed.

        `generate-zbm` prints two lines and swallows dracut's own output, so a
        module listed in the image whose files are not in it leaves a machine
        that boots and can never be unlocked over ssh. Said rather than raised:
        the passphrase prompt on the console still works.
        """
        listed = context.run_in_target(["lsinitrd", image], check=False)
        if isinstance(listed, CommandOutput) and listed.returncode != 0:
            context.degrade(
                REMOTE_UNLOCK_IMAGE, f"{image} could not be read: {str(listed).strip()[:200]}"
            )
            return
        if not any(marker in str(listed) for marker in ZBM_UNLOCK_MARKERS):
            context.degrade(
                REMOTE_UNLOCK_IMAGE,
                f"{image} carries none of {', '.join(ZBM_UNLOCK_MARKERS)}, "
                "so only the console can unlock this machine",
            )

    def apply(self, context: Context) -> None:
        context.run_in_target(["zpool", "set", f"bootfs={self.dataset}", self.pool])
        context.run_in_target(
            [
                "zfs", "set",
                f"org.zfsbootmenu:commandline={' '.join(self.kernel_params)}",
                self.dataset,
            ]
        )
        context.write(ZBM_CONFIG, self._config())
        context.run_in_target(["generate-zbm"])
        image = self._image(context)
        if self.unlocks_remotely:
            self._say_if_the_image_cannot_unlock(context, image)
        context.run_in_target(["install", "-D", "-m0644", image, f"{self.esp}/{FALLBACK_IMAGE}"])
        if self.write_nvram:
            _try_the_nvram_entry(
                context,
                [
                    "efibootmgr",
                    "--create",
                    "--disk", context.containing_disk(self.esp_device),
                    "--part", str(context.partition_index(self.esp_device)),
                    "--label", "ZFSBootMenu",
                    "--loader", _windows_path(image, self.esp),
                ],
            )

    def _image(self, context: Context) -> str:
        """Whatever generate-zbm wrote. It names the image after the kernel
        it built from, so `vmlinuz.EFI` is only one of the names it can have."""
        # findutils 4.11.0 exits 1 for an entry it could not read while still
        # printing the matches it reached, so the listing decides, not the code.
        listing = str(
            context.run_in_target(
                ["find", f"{self.esp}/{ZBM_DIRECTORY}", "-name", "*.EFI"], check=False
            )
        ).strip()
        found = sorted(
            line.strip() for line in listing.splitlines() if line.strip().endswith(".EFI")
        )
        if not found:
            said = f": {listing[:200]}" if listing else ""
            raise NothingToBoot(
                f"generate-zbm wrote no EFI image under {self.esp}/{ZBM_DIRECTORY}{said}"
            )
        return found[0]


def build(config: InstallConfig) -> list[Operation]:
    kind = config.bootloader.kind
    facts = boot_facts(config)
    mount = compat.esp_mount(config.disk.graph)
    esp = mount.path if mount is not None else None
    esp_device = _esp_partition(config)
    packages = BOOTLOADER_PACKAGES[kind]
    write_nvram = config.disk.mode is not DiskMode.IMAGE
    if (
        config.bootloader.firmware is Firmware.UEFI
        and kind is not Bootloader.SYSTEMD_BOOT
        and write_nvram
    ):
        # GRUB only: `bootctl install` writes the boot entry through efivarfs
        # itself, so systemd-boot needs no efibootmgr. Nor does an image
        # install, whose NVRAM is not the one that will boot it.
        packages = (*packages, EFI_PACKAGE)
    operations: list[Operation] = []
    if packages:
        operations.append(
            Emerge(stage=Stage.BOOTLOADER, packages=packages, summary="install the bootloader")
        )
    if kind is Bootloader.GRUB:
        operations += [
            WriteGrubDefaults(
                kernel_params=(*facts.unlock_parameters, *config.bootloader.kernel_params),
                cryptodisk=compat.boot_is_encrypted(config.disk.graph),
                serial=serial_console(config),
                luks=facts.containers,
                arrays=facts.arrays,
                keymap=facts.keymap,
            ),
            InstallGrub(
                firmware=config.bootloader.firmware,
                esp=esp,
                boot_devices=_bios_boot_devices(config),
                force=config.disk.mode is DiskMode.IMAGE,
                write_nvram=write_nvram,
            ),
        ]
    elif kind is Bootloader.SYSTEMD_BOOT and esp is not None:
        provider = BOOTCTL_PACKAGE[config.system.init]
        operations += [
            RequestBootctl(package=provider),
            # `--noreplace`: `sys-kernel/installkernel[systemd-boot]` RDEPENDs
            # `sys-apps/systemd[boot(-)]`, so the kernel stage has already
            # pulled it in and a plain atom here is `[ebuild R]`. One run spent
            # 132 seconds rebuilding it with the flags it already had. The
            # operation stays, because nothing else guarantees `bootctl` when
            # the provider is `systemd-utils`.
            Emerge(
                stage=Stage.BOOTLOADER,
                packages=(provider,),
                summary="install bootctl",
                mode=InstallMode.NOREPLACE,
            ),
            InstallSystemdBoot(esp=esp),
            ShowTheBootMenu(esp=esp),
        ]
    elif kind is Bootloader.ZFSBOOTMENU and esp is not None and esp_device is not None:
        operations += [
            # Without the stub systemd ships behind its `boot` flag,
            # generate-zbm writes loose components and no bootable image.
            RequestBootctl(package=BOOTCTL_PACKAGE[config.system.init]),
            Emerge(
                stage=Stage.BOOTLOADER,
                packages=(BOOTCTL_PACKAGE[config.system.init],),
                summary="install the EFI stub generate-zbm builds around",
            ),
        ]
        if config.kernel.remote_unlock.enabled:
            operations.append(
                ConfigureZfsBootMenuRemoteAccess(
                    unlock=config.kernel.remote_unlock,
                    authorized_keys=config.system.authorized_keys,
                )
            )
        operations += [
            InstallZfsBootMenu(
                pool=facts.pool,
                dataset=facts.pool_dataset,
                esp=esp,
                esp_device=esp_device,
                kernel_params=config.bootloader.kernel_params,
                serial=serial_console(config),
                unlocks_remotely=config.kernel.remote_unlock.enabled,
                write_nvram=write_nvram,
            ),
        ]
    return operations


def boot_facts(config: InstallConfig) -> BootFacts:
    """Derive every root and initramfs input shared by boot planners."""
    graph = config.disk.graph
    root = graph[config.disk.root]
    source = graph[root.source] if isinstance(root, Mountpoint) else root
    if isinstance(source, ZfsDataset):
        pool = graph[source.pool]
        if not isinstance(pool, ZfsPool):
            raise InvalidLayout(f"{source.id} does not refer to a ZFS pool")
        root_device: DeviceId | None = None
        dataset = f"{pool.name}/{source.name}"
        root_parameters: tuple[str, ...] = ()
        pool_name = pool.name
        pool_dataset = dataset
    elif isinstance(source, Subvolume):
        filesystem = graph[source.filesystem]
        if not isinstance(filesystem, Filesystem):
            raise InvalidLayout(f"{source.id} does not refer to a filesystem")
        root_device = filesystem.device
        dataset = ""
        root_parameters = (f"rootflags=subvol={source.name}",)
        pool_name = ""
        pool_dataset = ""
    elif isinstance(source, Filesystem):
        root_device = source.device
        dataset = ""
        root_parameters = ()
        pool_name = ""
        pool_dataset = ""
    else:
        raise InvalidLayout(f"{config.disk.root} is not something a boot entry can mount")
    containers = tuple(node.backing for node in compat.early_containers(graph))
    arrays = tuple(node.id for node in graph.of_type(MdRaid))
    return BootFacts(
        root=root_device,
        dataset=dataset,
        root_parameters=root_parameters,
        containers=containers,
        arrays=arrays,
        keymap=initramfs_keymap(config),
        unlock_parameters=unlock_parameters(config),
        pool=pool_name,
        pool_dataset=pool_dataset,
    )


def _bios_boot_devices(config: InstallConfig) -> tuple[DeviceId, ...]:
    """Return physical graph leaves that can contain a BIOS boot sector."""
    graph = config.disk.graph
    candidates = {
        node.id
        for node in (
            graph[config.disk.root],
            *(graph[ancestor] for ancestor in graph.ancestors_of(config.disk.root)),
        )
        if isinstance(node, Existing)
    }
    disks = {
        table.disk
        for table in graph.of_type(PartitionTable)
        if table.disk in candidates
    }
    selected = disks or candidates
    return tuple(sorted(selected))


def luks_parameters(context: Context, devices: tuple[DeviceId, ...]) -> tuple[str, ...]:
    """`rd.luks.uuid` for each container the initramfs has to open."""
    return tuple(f"rd.luks.uuid={context.device_uuid(device)}" for device in devices)


def keymap_parameters(keymap: str) -> tuple[str, ...]:
    """`rd.vconsole.keymap`, so the passphrase prompt uses the right keyboard."""
    return (f"rd.vconsole.keymap={keymap}",) if keymap else ()


def unlock_parameters(config: InstallConfig) -> tuple[str, ...]:
    """What the initramfs needs to have an address before the root is unlocked.

    `rd.neednet=1` brings the link up without a network root, and `ip=` is what
    configures it; dracut's network module does nothing without both.
    """
    unlock = config.kernel.remote_unlock
    if not unlock.enabled:
        return ()
    return ("rd.neednet=1", f"ip={_ip_parameter(unlock)}")


def _ip_parameter(unlock: RemoteUnlock) -> str:
    """dracut's `ip=`, built from the three fields the operator answered.

    Seven colon-separated fields: client, peer, gateway, netmask, hostname,
    interface, autoconf. The netmask is dotted there, so a CIDR prefix is
    converted; an address alone reaches nothing off its own subnet.
    """
    if not unlock.address:
        return f"{unlock.interface}:dhcp" if unlock.interface else "dhcp"
    address, _, prefix = unlock.address.partition("/")
    netmask = _netmask(address, prefix)
    client, gateway = _bracketed(address), _bracketed(unlock.gateway)
    return f"{client}::{gateway}:{netmask}::{unlock.interface}:none"


def _bracketed(address: str) -> str:
    """An IPv6 literal in square brackets. The fields are colon-separated and
    so is the address, so an unbracketed one makes the whole parameter
    unreadable."""
    return f"[{address}]" if ":" in address else address


def _netmask(address: str, prefix: str) -> str:
    """Dotted for IPv4, the prefix itself for IPv6, empty when unparsable.

    Left empty rather than guessed: dracut takes an empty field as `unset`,
    and a wrong netmask silently puts the machine on the wrong subnet.
    """
    if not prefix:
        return ""
    try:
        parsed = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
    except ValueError:
        return ""
    if isinstance(parsed, ipaddress.IPv6Network):
        return str(parsed.prefixlen)
    return str(parsed.netmask)


def initramfs_devices(config: InstallConfig) -> tuple[tuple[DeviceId, ...], tuple[DeviceId, ...]]:
    """The containers and arrays the initramfs has to be told about.

    One function for both command line writers: deriving it twice is how
    systemd-boot came to omit the arrays that GRUB was given.
    """
    facts = boot_facts(config)
    return facts.containers, facts.arrays


def array_parameters(context: Context, devices: tuple[DeviceId, ...]) -> tuple[str, ...]:
    """`rd.md.uuid` for each array the initramfs has to assemble."""
    return tuple(f"rd.md.uuid={context.array_uuid(device)}" for device in devices)


def initramfs_keymap(config: InstallConfig) -> str:
    """Only when an encrypted device asks for a passphrase before the console
    keymap is loaded, and only when it differs from the default."""
    wanted = config.system.keymap_initramfs or config.system.keymap
    graph = config.disk.graph
    native_encrypted_root = any(
        pool.encrypted
        for pool in graph.of_type(ZfsPool)
        if pool.id in graph.ancestors_of(config.disk.root)
    )
    if wanted == "us" or (
        not compat.early_containers(graph) and not native_encrypted_root
    ):
        return ""
    return wanted


def serial_console(config: InstallConfig) -> tuple[str, int] | None:
    """The serial port and speed the kernel command line asks for, if any."""
    for parameter in config.bootloader.kernel_params:
        if not parameter.startswith("console=ttyS"):
            continue
        port, _, rest = parameter.split("=", 1)[1].partition(",")
        # The leading run only: `115200n8` names the speed and then the frame
        # format, and taking every digit made that 1152008 baud.
        speed = re.match(r"\d+", rest)
        return port, int(speed.group()) if speed else 115200
    return None


def _windows_path(image: str, esp: PurePosixPath) -> str:
    """`efibootmgr --loader` wants the path within the esp, backslashed."""
    return "\\" + str(PurePosixPath(image).relative_to(esp)).replace("/", "\\")


def _esp_partition(config: InstallConfig) -> DeviceId | None:
    """The esp partition itself, found through the mount `compat` already
    identifies, so the two do not disagree about which mount is the esp."""
    graph = config.disk.graph
    mount = compat.esp_mount(graph)
    if mount is None:
        return None
    # Sorted, because `ancestors_of` returns a frozenset and a mirrored esp
    # would otherwise give a different plan on every run.
    for parent in sorted(graph.ancestors_of(mount.id)):
        node = graph[parent]
        if isinstance(node, Partition) and node.role is PartitionRole.ESP:
            return node.id
    # A reused esp has no `Partition` node: it is the existing device the
    # filesystem sits on, which is what efibootmgr and grub-install want.
    for parent in sorted(graph.ancestors_of(mount.id)):
        if isinstance(graph[parent], Existing):
            return graph[parent].id
    return None
