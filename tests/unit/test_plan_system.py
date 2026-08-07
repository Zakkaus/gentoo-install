from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

import pytest

from gentoo_install.errors import LocaleMissing
from gentoo_install.model.config import (
    ConsoleFontSize,
    InitSystem,
    InstallConfig,
    SystemConfig,
    User,
)
from gentoo_install.model.device import Filesystem, FilesystemType, Luks, Mountpoint, Node
from gentoo_install.plan import system

from .layouts import config, ext4_on_gpt, i
from .recorder import Recorder

FSTAB = PurePosixPath("/etc/fstab")
CRYPTTAB = PurePosixPath("/etc/crypttab")


def apply_all(installation: InstallConfig, *, generated: str | None = None) -> Recorder:
    recorder = Recorder()
    if generated is not None:
        recorder.replies["locale"] = generated
    for operation in system.build(installation):
        operation.apply(recorder)
    return recorder


def with_system(**fields: Any) -> InstallConfig:
    return replace(config(), system=replace(SystemConfig(), **fields))


def generated(installation: InstallConfig) -> str:
    return " ".join(locale.lower().replace("-", "") for locale in installation.system.locales)


def test_a_locale_that_locale_gen_skipped_is_generated_again_and_then_checked() -> None:
    installation = with_system(locales=("en_US.UTF-8", "zh_TW.UTF-8"))
    recorder = apply_all(installation, generated=generated(installation))
    assert recorder.files[PurePosixPath("/etc/locale.gen")] == (
        "en_US.UTF-8 UTF-8\nzh_TW.UTF-8 UTF-8\n"
    )
    assert not recorder.argv_starting("localedef")


def test_locale_gen_exiting_zero_is_not_taken_as_proof_the_locale_exists() -> None:
    installation = with_system(locales=("en_US.UTF-8", "zh_TW.UTF-8"))
    recorder = Recorder()
    recorder.replies["locale"] = "en_us.utf8"
    operation = system.GenerateLocales(locales=installation.system.locales)
    with pytest.raises(LocaleMissing, match="zh_TW.UTF-8"):
        operation.apply(recorder)
    assert recorder.argv_starting("localedef")


def test_the_console_font_is_the_one_kbd_ships_for_that_size() -> None:
    installation = with_system(console_font=ConsoleFontSize.SIZE_16X32)
    written = apply_all(installation, generated=generated(installation)).files
    assert "FONT=latarcyrheb-sun32" in written[PurePosixPath("/etc/vconsole.conf")]


def test_openrc_writes_the_conf_d_files_instead_of_vconsole() -> None:
    installation = with_system(init=InitSystem.OPENRC)
    written = apply_all(installation, generated=generated(installation)).files
    assert PurePosixPath("/etc/vconsole.conf") not in written
    assert written[PurePosixPath("/etc/conf.d/keymaps")] == 'keymap="us"\n'


def test_fstab_names_devices_by_uuid_and_puts_the_root_check_first() -> None:
    installation = config()
    written = apply_all(installation, generated=generated(installation)).files[FSTAB]
    lines = [line for line in written.splitlines() if not line.startswith("#")]
    assert lines[0].startswith("UUID=uuid-of-rootpart\t/\text4\t")
    assert lines[0].endswith("\t0\t1")
    assert any("umask=0077" in line for line in lines)


def test_an_option_the_layout_sets_replaces_the_default_rather_than_joining_it() -> None:
    """`umask=0077,umask=0077` is what mount reads when both are written, and
    the installed system's fstab had exactly that."""
    installation = config()
    written = apply_all(installation, generated=generated(installation)).files[FSTAB]
    esp = next(line for line in written.splitlines() if "/efi" in line)
    assert esp.count("umask=") == 1


def test_a_swap_entry_is_written_even_though_the_installer_never_enables_it() -> None:
    from gentoo_install.model.device import Swap

    nodes: list[Node] = ext4_on_gpt()
    nodes.append(Swap(id=i("swap"), device=i("rootpart")))
    installation = config(nodes)
    written = apply_all(installation, generated=generated(installation)).files[FSTAB]
    assert "\tswap\tsw\t0\t0" in written


def test_an_encrypted_root_gets_the_option_that_stops_systemd_waiting_forever() -> None:
    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
    nodes += [
        Luks(id=i("crypt"), backing=i("rootpart"), name="root"),
        Filesystem(id=i("rootfs"), device=i("crypt"), kind=FilesystemType.EXT4),
    ]
    installation = config(nodes)
    written = apply_all(installation, generated=generated(installation)).files[CRYPTTAB]
    assert written == "root\tUUID=uuid-of-rootpart\tnone\tluks,x-initrd.attach\n"


def test_an_encrypted_data_disk_does_not_get_that_option() -> None:
    nodes: list[Node] = ext4_on_gpt()
    nodes += [
        Luks(id=i("crypt"), backing=i("esp"), name="data"),
        Filesystem(id=i("datafs"), device=i("crypt"), kind=FilesystemType.EXT4),
        Mountpoint(id=i("mnt-data"), source=i("datafs"), path=PurePosixPath("/srv")),
    ]
    installation = config(nodes)
    written = apply_all(installation, generated=generated(installation)).files[CRYPTTAB]
    assert written.strip().endswith("luks")


def groups_of(recorder_argv: tuple[str, ...]) -> list[str]:
    return recorder_argv[recorder_argv.index("--groups") + 1].split(",")


def test_a_sudo_user_lands_in_wheel_and_a_plain_one_does_not() -> None:
    sudoer = with_system(users=(User(name="zakk", sudo=True),))
    plain = with_system(users=(User(name="guest", sudo=False),))
    assert "wheel" in groups_of(apply_all(sudoer, generated=generated(sudoer)).only("useradd"))
    assert "wheel" not in groups_of(apply_all(plain, generated=generated(plain)).only("useradd"))


def test_sudo_needs_a_password_because_a_desktop_button_once_removed_that() -> None:
    installation = with_system(users=(User(name="zakk", sudo=True),))
    written = apply_all(installation, generated=generated(installation)).files
    assert written[PurePosixPath("/etc/sudoers.d/10-wheel")] == "%wheel ALL=(ALL:ALL) ALL\n"


def test_an_account_with_no_hash_is_locked_rather_than_left_open() -> None:
    installation = with_system(users=(User(name="zakk", password_hash=""),))
    recorder = apply_all(installation, generated=generated(installation))
    assert ("passwd", "--lock", "zakk") in recorder.in_target
    assert ("passwd", "--lock", "root") in recorder.in_target


def test_a_hash_is_set_without_ever_holding_the_password() -> None:
    installation = with_system(
        users=(User(name="zakk", password_hash="$6$salt$hash"),), root_password_hash="$6$salt$root"
    )
    recorder = apply_all(installation, generated=generated(installation))
    assert ("usermod", "--password", "$6$salt$hash", "zakk") in recorder.in_target
    assert ("usermod", "--password", "$6$salt$root", "root") in recorder.in_target


def test_systemd_networkd_gets_a_network_file_and_not_only_an_enabled_unit() -> None:
    installation = config()
    recorder = apply_all(installation, generated=generated(installation))
    assert "DHCP=yes" in recorder.files[PurePosixPath("/etc/systemd/network/20-wired.network")]
    assert ("systemctl", "enable", "systemd-networkd.service") in recorder.in_target


def test_openrc_gets_a_serial_login_when_the_cmdline_asks_for_one() -> None:
    """systemd starts one by itself; openrc's inittab ships the serial lines
    commented out, so a machine installed for remote use has no way in."""
    from gentoo_install.model.config import BootloaderConfig

    remote = replace(
        with_system(init=InitSystem.OPENRC),
        bootloader=BootloaderConfig(kernel_params=("console=ttyS0,115200",)),
    )
    written = apply_all(remote, generated=generated(remote)).files
    assert "agetty -L 115200 ttyS0" in written[PurePosixPath("/etc/inittab")]

    local = with_system(init=InitSystem.OPENRC)
    assert PurePosixPath("/etc/inittab") not in apply_all(
        local, generated=generated(local)
    ).files


def test_systemd_needs_no_inittab_entry_for_the_serial_console() -> None:
    from gentoo_install.model.config import BootloaderConfig

    remote = replace(
        config(), bootloader=BootloaderConfig(kernel_params=("console=ttyS0,115200",))
    )
    written = apply_all(remote, generated=generated(remote)).files
    assert PurePosixPath("/etc/inittab") not in written


def test_openrc_gets_netifrc_which_a_stage3_does_not_carry() -> None:
    installation = with_system(init=InitSystem.OPENRC)
    merged = " ".join(
        operation.describe() for operation in system.build(installation) if "emerge" in operation.describe()
    )
    assert "net-misc/netifrc" in merged
