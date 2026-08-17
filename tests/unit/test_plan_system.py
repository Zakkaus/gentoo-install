# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

import pytest

from gentoo_install.errors import InvalidLayout, LocaleMissing
from gentoo_install.model.config import (
    Networking,
    ConsoleFontSize,
    InitSystem,
    InstallConfig,
    Logger,
    ProxyConfig,
    ProxyKind,
    SystemConfig,
    User,
)
from gentoo_install.model.size import Size
from gentoo_install.model.device import (
    DeviceGraph,
    Existing,
    Filesystem,
    FilesystemType,
    Luks,
    Mountpoint,
    Node,
    Partition,
    PartitionRole,
    Subvolume,
    ZfsDataset,
    ZfsPool,
)
from gentoo_install.plan import bootloader, disk, mounts, system
from gentoo_install.plan.portage import Emerge

from .layouts import config, ext4_on_gpt, i
from .recorder import Recorder

#: What `locale -a` answers for the default set, so a test that is not
#: about locales does not trip over the check that they were generated.
DEFAULT_LOCALES = " ".join(
    locale.lower().replace("-", "") for locale in SystemConfig().locales
)

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


def test_a_converted_operation_keeps_its_description_byte_for_byte() -> None:
    operation = system.SetTimezone(timezone="Asia/Tokyo")

    assert operation.describe_parts() == ("set the timezone to {}", ("Asia/Tokyo",))
    assert operation.describe() == "set the timezone to Asia/Tokyo"

def test_a_locale_that_locale_gen_skipped_is_generated_again_and_then_checked() -> None:
    installation = with_system(locales=("en_US.UTF-8", "zh_TW.UTF-8"))
    recorder = apply_all(installation, generated=generated(installation))
    assert recorder.files[PurePosixPath("/etc/locale.gen")] == (
        "en_US.UTF-8 UTF-8\nzh_TW.UTF-8 UTF-8\n"
    )
    assert not recorder.argv_starting("localedef")


def test_installed_system_keeps_proxy_endpoint_and_bypass_without_credentials() -> None:
    installation = replace(
        config(),
        proxy=ProxyConfig(
            kind=ProxyKind.SOCKS5,
            host="proxy.example",
            port=1080,
            username="operator",
            password="secret",
            bypass=("localhost", "corp.example"),
        ),
    )
    written = apply_all(installation, generated=generated(installation)).files
    environment = written[PurePosixPath("/etc/environment")]
    profile = written[PurePosixPath("/etc/profile.d/gentoo-install-proxy.sh")]
    assert "socks5h://proxy.example:1080" in environment
    assert "localhost,corp.example" in environment
    assert "secret" not in environment
    assert "secret" not in profile


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


def test_openrc_takes_lang_from_env_d_because_locale_conf_is_systemds() -> None:
    """`gentoo-install-zh/scripts/main.sh:80` branches the same way: openrc runs
    `eselect locale set` and systemd writes the file. Writing only the systemd
    file left an openrc install booting under C, with no CJK anywhere."""
    openrc = with_system(init=InitSystem.OPENRC, locale="zh_TW.UTF-8")
    written = apply_all(openrc, generated=generated(openrc))
    assert PurePosixPath("/etc/locale.conf") not in written.files
    assert written.files[PurePosixPath("/etc/env.d/02locale")] == 'LANG="zh_TW.UTF-8"\n'
    assert ("env-update",) in written.in_target

    systemd = with_system(init=InitSystem.SYSTEMD, locale="zh_TW.UTF-8")
    both = apply_all(systemd, generated=generated(systemd))
    assert both.files[PurePosixPath("/etc/locale.conf")] == "LANG=zh_TW.UTF-8\n"
    assert PurePosixPath("/etc/env.d/02locale") not in both.files


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


def test_fstab_escapes_a_space_in_a_mountpoint() -> None:
    nodes = ext4_on_gpt()
    nodes.append(
        Mountpoint(
            id=i("mnt-shared"),
            source=i("rootfs"),
            path=PurePosixPath("/srv/shared files"),
        )
    )
    installation = config(nodes)
    written = apply_all(installation, generated=generated(installation)).files[FSTAB]
    assert "\t/srv/shared\\040files\text4\t" in written


def test_an_option_the_layout_sets_replaces_the_default_rather_than_joining_it() -> None:
    """`umask=0077,umask=0077` is what mount reads when both are written, and
    the installed system's fstab had exactly that."""
    installation = config()
    written = apply_all(installation, generated=generated(installation)).files[FSTAB]
    esp = next(line for line in written.splitlines() if "/efi" in line)
    assert esp.count("umask=") == 1


def test_runtime_mounts_and_fstab_share_resolved_graph_meaning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [
        node for node in ext4_on_gpt() if node.id not in {i("rootfs"), i("mnt-root")}
    ]
    nodes += [
        Filesystem(id=i("rootfs"), device=i("rootpart"), kind=FilesystemType.BTRFS),
        Subvolume(id=i("sub-root"), filesystem=i("rootfs"), name="@"),
        Mountpoint(
            id=i("mnt-root"),
            source=i("sub-root"),
            path=PurePosixPath("/"),
            options=("compress=zstd:2",),
        ),
        Existing(id=i("pool-disk"), selector="/dev/disk/by-id/pool"),
        ZfsPool(id=i("pool"), vdevs=(i("pool-disk"),), name="tank"),
        ZfsDataset(id=i("ds-home"), pool=i("pool"), name="home"),
        Mountpoint(id=i("mnt-home"), source=i("ds-home"), path=PurePosixPath("/home")),
    ]
    installation = config(nodes)
    resolved = mounts.resolve_mounts(installation.disk.graph)
    root = next(mount for mount in resolved if mount.path == PurePosixPath("/"))
    assert root.device == i("rootpart")
    assert root.filesystem_kind is FilesystemType.BTRFS
    assert root.subvolume == "@"
    assert root.options == ("compress=zstd:2", "subvol=@")
    assert any(
        mount.path == PurePosixPath("/efi")
        and mount.device == i("esp")
        and mount.filesystem_kind is FilesystemType.VFAT
        for mount in resolved
    )
    assert any(
        mount.path == PurePosixPath("/home") and mount.dataset == "tank/home"
        for mount in resolved
    )

    shared = tuple(
        replace(mount, device=i("resolved-root"), options=(*mount.options, "shared"))
        if mount.path == PurePosixPath("/")
        else replace(mount, device=i("resolved-esp"))
        if mount.path == PurePosixPath("/efi")
        else replace(mount, dataset="resolved/home")
        for mount in resolved
    )
    calls = {"disk": 0, "system": 0}

    def for_disk(graph: DeviceGraph) -> tuple[mounts.ResolvedMount, ...]:
        calls["disk"] += 1
        assert graph is installation.disk.graph
        return shared

    def for_system(graph: DeviceGraph) -> tuple[mounts.ResolvedMount, ...]:
        calls["system"] += 1
        assert graph is installation.disk.graph
        return shared

    monkeypatch.setattr(disk, "resolve_mounts", for_disk)
    monkeypatch.setattr(system, "resolve_mounts", for_system)

    runtime = [
        operation
        for operation in disk.build(installation)
        if isinstance(operation, (disk.Mount, disk.MountZfsDataset))
    ]
    entries = system.fstab_entries(installation)

    assert calls == {"disk": 1, "system": 1}
    assert [operation.path for operation in runtime] == [
        PurePosixPath("/"),
        PurePosixPath("/efi"),
        PurePosixPath("/home"),
    ]
    root_mount = next(operation for operation in runtime if operation.path == PurePosixPath("/"))
    assert isinstance(root_mount, disk.Mount)
    assert root_mount.source == i("resolved-root")
    assert root_mount.options == ("compress=zstd:2", "subvol=@", "shared")
    assert any(
        isinstance(operation, disk.Mount)
        and operation.path == PurePosixPath("/efi")
        and operation.source == i("resolved-esp")
        for operation in runtime
    )
    assert any(
        isinstance(operation, disk.MountZfsDataset)
        and operation.path == PurePosixPath("/home")
        and operation.name == "resolved/home"
        for operation in runtime
    )

    root_entry = next(entry for entry in entries if entry.path == PurePosixPath("/"))
    assert root_entry.device == i("resolved-root")
    assert root_entry.kind == "btrfs"
    assert root_entry.options.count("subvol=@") == 1
    assert "compress=zstd:2" in root_entry.options
    assert "shared" in root_entry.options
    assert any(
        entry.path == PurePosixPath("/efi") and entry.device == i("resolved-esp")
        for entry in entries
    )
    assert not any(entry.path == PurePosixPath("/home") for entry in entries)


def test_unsupported_mount_sources_are_rejected_by_both_consumers() -> None:
    nodes = [node for node in ext4_on_gpt() if node.id != i("mnt-root")]
    nodes.append(
        Mountpoint(id=i("mnt-root"), source=i("rootpart"), path=PurePosixPath("/"))
    )
    installation = config(nodes)
    for consumer in (disk.build, system.fstab_entries):
        with pytest.raises(InvalidLayout, match="not a mountable source"):
            consumer(installation)


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


def test_declining_sudo_in_the_menu_keeps_the_account_out_of_wheel() -> None:
    """`_groups_of` strips `wheel` for a plain account and then re-adds every
    name in `user.groups`, so a second copy of the list in the menu handed the
    account the group `/etc/sudoers.d/10-wheel` grants ALL to."""
    from gentoo_install.tui import screens

    from .fake_screen import FakeScreen

    from gentoo_install.data import load_catalog
    from gentoo_install.i18n import Catalog

    at = screens.Context(
        translate=Catalog("en"),
        disks=[("/dev/disk/by-id/virtio-target0", "20 GiB")],
        groups=load_catalog(),
        hash_password=lambda password: "$6$t$x",
    )
    # One form: name, the password twice, sudo left unticked, then Done.
    keys = [
        *"zakk", "KEY_DOWN", *"secret", "KEY_DOWN", *"secret",
        "KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n",
    ]
    answer = screens.user_screen(FakeScreen(keys=keys, lines=24, columns=100), config(), at)
    plain = answer.unwrap().system.users[0]
    assert plain.sudo is False
    assert "wheel" not in system._groups_of(plain)


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
    line = written[PurePosixPath("/etc/inittab")].strip()
    assert line.startswith("s0:")
    assert "agetty -L 115200 ttyS0" in line
    # The id field takes four characters at most; a longer one is dropped and
    # the console stays silent, which is what a real boot showed.
    assert len(line.split(":", 1)[0]) <= 4

    local = with_system(init=InitSystem.OPENRC)
    assert PurePosixPath("/etc/inittab") not in apply_all(
        local, generated=generated(local)
    ).files


def test_serial_frame_format_does_not_change_the_getty_baud() -> None:
    from gentoo_install.model.config import BootloaderConfig

    remote = replace(
        with_system(init=InitSystem.OPENRC),
        bootloader=BootloaderConfig(kernel_params=("console=ttyS0,115200n8",)),
    )
    getty = next(
        operation
        for operation in system.build(remote)
        if isinstance(operation, system.EnableSerialGetty)
    )
    grub = next(
        operation
        for operation in bootloader.build(remote)
        if isinstance(operation, bootloader.WriteGrubDefaults)
    )
    assert grub.serial is not None
    assert getty.baud == grub.serial[1] == 115200


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


def test_every_networking_backend_has_one_requirements_entry() -> None:
    assert set(system.NETWORK_BACKENDS) == set(Networking)


def test_zram_is_configured_for_the_init_that_will_read_it() -> None:
    """systemd has a generator that reads one file; openrc has an init script
    that reads conf.d and has to be added to a runlevel."""
    wanted = replace(config(), system=replace(config().system, zram=Size.parse("4GiB")))
    recorder = Recorder()
    for operation in system.build(wanted):
        if isinstance(operation, system.ConfigureZram):
            operation.apply(recorder)
    assert "zram-size = 4096" in recorder.files[PurePosixPath("/etc/systemd/zram-generator.conf")]

    openrc = replace(
        wanted, system=replace(wanted.system, init=InitSystem.OPENRC, zram=Size.parse("2GiB"))
    )
    plain = Recorder()
    enabled: list[str] = []
    for operation in system.build(openrc):
        if isinstance(operation, system.ConfigureZram):
            operation.apply(plain)
        if isinstance(operation, system.EnableService) and operation.service == "zram-init":
            enabled.append(operation.runlevel)
    assert "size0=2048" in plain.files[PurePosixPath("/etc/conf.d/zram-init")]
    assert enabled == ["boot"]

    assert not any(
        isinstance(operation, system.ConfigureZram) for operation in system.build(config())
    )


def test_the_hardware_clock_is_written_where_each_init_reads_it() -> None:
    """systemd reads the third line of /etc/adjtime and nothing else; openrc
    reads conf.d. Wrong here and every boot is off by the timezone offset."""
    recorder = Recorder()
    for operation in system.build(config()):
        if isinstance(operation, system.SetHardwareClock):
            operation.apply(recorder)
    assert recorder.files[PurePosixPath("/etc/adjtime")].splitlines()[2] == "UTC"

    local = replace(
        config(),
        system=replace(config().system, init=InitSystem.OPENRC, hardware_clock_utc=False),
    )
    plain = Recorder()
    for operation in system.build(local):
        if isinstance(operation, system.SetHardwareClock):
            operation.apply(plain)
    assert plain.files[PurePosixPath("/etc/conf.d/hwclock")] == 'clock="local"\n'


def test_only_planned_arrays_are_recorded_where_the_initramfs_reads_them() -> None:
    """Without /etc/mdadm.conf the array comes up under whatever name the
    kernel picks, the root UUID never appears and boot stops in the emergency
    shell."""
    from gentoo_install.model.device import DeviceId, Existing, MdRaid, RaidLevel

    nodes: list[Node] = [node for node in ext4_on_gpt() if node.id != i("rootfs")]
    nodes += [
        Existing(id=i("root-member"), selector="/dev/root-member"),
        Existing(id=i("root-member-2"), selector="/dev/root-member-2"),
        Existing(id=i("data-member"), selector="/dev/data-member"),
        Existing(id=i("data-member-2"), selector="/dev/data-member-2"),
        MdRaid(
            id=i("array-root"),
            members=(i("root-member"), i("root-member-2")),
            level=RaidLevel.RAID1,
            name="root",
        ),
        MdRaid(
            id=i("array-data"),
            members=(i("data-member"), i("data-member-2")),
            level=RaidLevel.RAID1,
            name="data",
        ),
        Filesystem(id=i("rootfs"), device=i("array-root"), kind=FilesystemType.EXT4),
    ]

    queried: list[DeviceId] = []

    class ArrayRecorder(Recorder):
        def array_uuid(self, device: DeviceId) -> str:
            queried.append(device)
            return {
                i("array-root"): "root-uuid",
                i("array-data"): "data-uuid",
            }[device]

    recorder = ArrayRecorder(
        replies={"mdadm": "ARRAY /dev/md/live metadata=1.2 UUID=live-uuid\n"}
    )
    written = [
        operation for operation in system.build(config(nodes))
        if isinstance(operation, system.WriteMdadmConf)
    ]
    assert len(written) == 1
    assert "root" in written[0].describe()
    assert "data" in written[0].describe()
    written[0].apply(recorder)
    conf = recorder.files[PurePosixPath("/etc/mdadm.conf")]
    # Without an address `mdadm --monitor` exits with an error, and a healthy
    # array then has a failed unit on every boot.
    assert conf == (
        "MAILADDR root\n"
        "ARRAY /dev/md/root metadata=1.2 UUID=root-uuid\n"
        "ARRAY /dev/md/data metadata=1.2 UUID=data-uuid\n"
    )
    assert queried == [i("array-root"), i("array-data")]
    assert not recorder.argv_starting("mdadm", "--detail", "--scan")

    assert not any(
        isinstance(operation, system.WriteMdadmConf) for operation in system.build(config())
    )


def static(
    *,
    init: InitSystem | None = None,
    interface: str | None = None,
    networking: Networking | None = None,
    addresses: tuple[str, ...] | None = None,
    gateways: tuple[str, ...] | None = None,
    dns: tuple[str, ...] | None = None,
    logger: Logger | None = None,
    cron: bool | None = None,
) -> InstallConfig:
    """A configuration with one system field replaced.

    Named parameters rather than `**fields: object`: the splat needed a
    suppression, and a misspelled field name in it was a silent default.
    """
    system = config().system
    if init is not None:
        system = replace(system, init=init)
    if interface is not None:
        system = replace(system, interface=interface)
    if networking is not None:
        system = replace(system, networking=networking)
    if addresses is not None:
        system = replace(system, addresses=addresses)
    if gateways is not None:
        system = replace(system, gateways=gateways)
    if dns is not None:
        system = replace(system, dns=dns)
    if logger is not None:
        system = replace(system, logger=logger)
    if cron is not None:
        system = replace(system, cron=cron)
    return replace(config(), system=system)


def networked(installation: InstallConfig) -> Recorder:
    recorder = Recorder()
    for operation in system.build(installation):
        if isinstance(operation, system.WriteNetworkConfig):
            operation.apply(recorder)
    return recorder


NETWORKD = PurePosixPath("/etc/systemd/network/20-wired.network")
CONFD_NET = PurePosixPath("/etc/conf.d/net")


def test_a_static_address_carries_its_gateway_and_its_resolvers() -> None:
    """An address with no resolver boots able to reach a number and unable to
    look up a name, which reads as no network at all."""
    written = networked(
        static(
            addresses=("192.0.2.10/24",), gateways=("192.0.2.1",), dns=("1.1.1.1", "9.9.9.9")
        )
    ).files[NETWORKD]
    assert "Address=192.0.2.10/24" in written
    assert "Gateway=192.0.2.1" in written
    assert "DNS=1.1.1.1" in written and "DNS=9.9.9.9" in written
    assert "DHCP=yes" not in written


def test_both_families_are_configured_together() -> None:
    """A v6-only or dual-stack network is not a special case any more."""
    written = networked(
        static(
            addresses=("192.0.2.10/24", "2001:db8::2/64"),
            gateways=("192.0.2.1", "fe80::1"),
        )
    ).files[NETWORKD]
    assert "Address=2001:db8::2/64" in written
    assert "Gateway=fe80::1" in written


def test_dhcp_accepts_router_advertisements_as_well() -> None:
    """Stateless autoconfiguration is how most v6 networks hand out a prefix,
    and DHCP=yes alone does not ask for it."""
    written = networked(static()).files[NETWORKD]
    assert "DHCP=yes" in written and "IPv6AcceptRA=yes" in written


def test_the_interface_is_matched_by_name_when_one_is_given() -> None:
    assert "Name=enp1s0" in networked(static(interface="enp1s0")).files[NETWORKD]
    both = networked(static()).files[NETWORKD]
    assert "Name=en*" in both and "Name=eth*" in both


def test_netifrc_names_the_interface_the_operator_gave() -> None:
    """netifrc has no wildcard: `config_eth0` on a machine whose card is
    `enp1s0` configures an interface that does not exist."""
    openrc = static(init=InitSystem.OPENRC, interface="enp1s0", addresses=("192.0.2.10/24",))
    written = networked(openrc).files[CONFD_NET]
    assert 'config_enp1s0="192.0.2.10/24"' in written


def test_netifrc_has_no_eth0_fallback_for_an_unresolved_static_interface() -> None:
    openrc = static(init=InitSystem.OPENRC, addresses=("192.0.2.10/24",))
    recorder = Recorder()
    for operation in system.build(openrc):
        if isinstance(operation, (system.WriteNetworkConfig, system.LinkNetifrcService)):
            operation.apply(recorder)

    assert CONFD_NET not in recorder.files
    assert not any("net.eth0" in argument for argv in recorder.in_target for argument in argv)


def test_an_openrc_static_address_is_applied_by_netifrc_and_not_by_dhcpcd() -> None:
    """dhcpcd manages every interface itself and would DHCP over the static
    address, and nothing reads /etc/conf.d/net unless net.<iface> is enabled."""
    openrc = static(init=InitSystem.OPENRC, interface="enp1s0", addresses=("192.0.2.10/24",))
    operations = system.build(openrc)
    linked = [one for one in operations if isinstance(one, system.LinkNetifrcService)]
    assert [one.interface for one in linked] == ["enp1s0"]
    enabled = [
        one.service for one in operations if isinstance(one, system.EnableService)
    ]
    assert "dhcpcd" not in enabled

    plain = static(init=InitSystem.OPENRC)
    services = [
        one.service for one in system.build(plain) if isinstance(one, system.EnableService)
    ]
    assert "dhcpcd" in services


def test_networkmanager_is_left_to_manage_the_interfaces_itself() -> None:
    managed = static(networking=Networking.NETWORKMANAGER_IWD)
    assert not networked(managed).files
    services = [
        one.service for one in system.build(managed) if isinstance(one, system.EnableService)
    ]
    assert "NetworkManager" in services


def test_root_can_be_given_a_key_before_the_first_boot() -> None:
    """A headless install with no key and no console is reachable only by
    taking the disk out."""
    keyed = replace(
        config(),
        system=replace(config().system, authorized_keys=("ssh-ed25519 AAAA test",)),
    )
    recorder = Recorder()
    for operation in system.build(keyed):
        if isinstance(operation, system.WriteAuthorizedKeys):
            operation.apply(recorder)
    assert recorder.files[PurePosixPath("/root/.ssh/authorized_keys")] == "ssh-ed25519 AAAA test\n"
    assert not any(
        isinstance(operation, system.WriteAuthorizedKeys) for operation in system.build(config())
    )


def test_the_sshd_drop_in_sorts_before_the_one_the_ebuild_installs() -> None:
    """`9999999gentoo-pam.conf` sets `PasswordAuthentication no`, and sshd takes
    the first value it reads, so a later file cannot turn password login on."""
    recorder = apply_all(
        with_system(sshd=True, sshd_password_login=True, sshd_root_login=True),
        generated=DEFAULT_LOCALES,
    )
    where = PurePosixPath("/etc/ssh/sshd_config.d/50-gentoo-install.conf")
    written = recorder.files[where]
    assert where.name < "9999999gentoo-pam.conf"
    assert "PasswordAuthentication yes" in written
    # PAM answers the prompt through keyboard-interactive, so the one setting
    # on its own still leaves a password refused.
    assert "KbdInteractiveAuthentication yes" in written
    assert "PermitRootLogin yes" in written


def test_keys_only_leaves_root_reachable_with_a_key_and_not_a_password() -> None:
    """Only when root is allowed in at all, which is not the default."""
    recorder = apply_all(
        with_system(sshd=True, sshd_password_login=False, sshd_root_login=True),
        generated=DEFAULT_LOCALES,
    )
    written = recorder.files[PurePosixPath("/etc/ssh/sshd_config.d/50-gentoo-install.conf")]
    assert "PasswordAuthentication no" in written
    assert "PermitRootLogin prohibit-password" in written


def test_root_over_ssh_is_refused_unless_the_operator_asks_for_it() -> None:
    """A machine reachable as root by default is one bad password away from
    being someone else's. The keys still reach root when no sudo user exists,
    so a headless install loses nothing."""
    from gentoo_install.model.config import SystemConfig

    assert SystemConfig().sshd_root_login is False
    plain = apply_all(with_system(sshd=True), generated=DEFAULT_LOCALES)
    written = plain.files[PurePosixPath("/etc/ssh/sshd_config.d/50-gentoo-install.conf")]
    assert "PermitRootLogin no" in written

    alone = with_system(sshd=True, authorized_keys=("ssh-ed25519 AAAA test",))
    assert system.key_accounts(alone.system)[0][0] == "root"


def test_refusing_root_refuses_it_with_a_key_too() -> None:
    recorder = apply_all(with_system(sshd=True, sshd_root_login=False), generated=DEFAULT_LOCALES)
    written = recorder.files[PurePosixPath("/etc/ssh/sshd_config.d/50-gentoo-install.conf")]
    assert "PermitRootLogin no" in written


def test_a_key_is_written_for_root_and_for_every_sudo_user() -> None:
    """The operator authorises a person, and that person uses both accounts.
    Root is on the list only when sshd lets root in, which is not the default."""
    installation = with_system(
        authorized_keys=("ssh-ed25519 AAAA test",),
        users=(User(name="zakk", sudo=True), User(name="guest")),
        sshd_root_login=True,
    )
    recorder = apply_all(installation, generated=generated(installation))
    assert PurePosixPath("/root/.ssh/authorized_keys") in recorder.files
    assert PurePosixPath("/home/zakk/.ssh/authorized_keys") in recorder.files
    assert PurePosixPath("/home/guest/.ssh/authorized_keys") not in recorder.files
    assert recorder.argv_starting("chown")


def test_a_key_still_reaches_root_when_there_is_no_sudo_user() -> None:
    """Refusing root with no other account authorised leaves a headless machine
    with no way in at all."""
    installation = with_system(
        authorized_keys=("ssh-ed25519 AAAA test",), sshd_root_login=False
    )
    assert system.key_accounts(installation.system) == (("root", "/root"),)


def test_refusing_root_keeps_the_key_off_root_when_a_sudo_user_has_it() -> None:
    installation = with_system(
        authorized_keys=("ssh-ed25519 AAAA test",),
        sshd_root_login=False,
        users=(User(name="zakk", sudo=True),),
    )
    assert system.key_accounts(installation.system) == (("zakk", "/home/zakk"),)


def test_choosing_no_networking_configures_none() -> None:
    """`NONE` produced exactly the plan `BUILTIN` did: a DHCP file and a
    service, which is the opposite of what the option says."""
    quiet = static(networking=Networking.NONE)
    assert not networked(quiet).files
    services = [
        one.service for one in system.build(quiet) if isinstance(one, system.EnableService)
    ]
    assert "systemd-networkd" not in services and "dhcpcd" not in services


def test_openrc_reads_its_containers_from_conf_d_and_not_from_crypttab() -> None:
    """`sys-fs/cryptsetup` ships a `dmcrypt` service that reads
    /etc/conf.d/dmcrypt; crypttab there is a file nobody opens."""
    entries = (
        system.CrypttabEntry(name="root", backing=i("crypt"), initrd_attach=True),
        system.CrypttabEntry(name="data", backing=i("crypt2"), initrd_attach=False),
    )
    recorder = Recorder()
    system.WriteCrypttab(entries=entries, init=InitSystem.OPENRC).apply(recorder)
    written = recorder.files[PurePosixPath("/etc/conf.d/dmcrypt")]
    assert "target=data" in written
    # The root is already open by then: the initramfs did it.
    assert "target=root" not in written
    assert PurePosixPath("/etc/crypttab") not in recorder.files

    systemd = Recorder()
    system.WriteCrypttab(entries=entries, init=InitSystem.SYSTEMD).apply(systemd)
    assert "x-initrd.attach" in systemd.files[PurePosixPath("/etc/crypttab")]


def test_the_crypttab_describe_names_only_what_the_file_will_hold() -> None:
    """An encrypted openrc root is the ordinary case and it is the one where
    the two disagreed: every entry is opened by the initramfs, so the file is
    written empty while `--dry-run` said it would name `root`."""
    only_root = (system.CrypttabEntry(name="root", backing=i("crypt"), initrd_attach=True),)
    operation = system.WriteCrypttab(entries=only_root, init=InitSystem.OPENRC)
    recorder = Recorder()
    operation.apply(recorder)

    assert "root" not in recorder.files[PurePosixPath("/etc/conf.d/dmcrypt")]
    assert "root" not in operation.describe()
    assert "empty" in operation.describe(), operation.describe()

    # Not vacuous: on systemd the same entry is written and named.
    on_systemd = system.WriteCrypttab(entries=only_root, init=InitSystem.SYSTEMD)
    written = Recorder()
    on_systemd.apply(written)
    assert "root" in written.files[PurePosixPath("/etc/crypttab")]
    assert "root" in on_systemd.describe()

    # The rule rather than the case: every name the describe gives is a name
    # the file holds, whichever init and whichever mix of entries.
    for init in (InitSystem.OPENRC, InitSystem.SYSTEMD):
        mixed = system.WriteCrypttab(entries=entries_for_both(), init=init)
        seen = Recorder()
        mixed.apply(seen)
        holds = "".join(seen.files.values())
        for entry in entries_for_both():
            assert (entry.name in mixed.describe()) == (
                f"={entry.name}" in holds or f"{entry.name}\t" in holds
            ), (init, entry.name, mixed.describe())


def entries_for_both() -> tuple[system.CrypttabEntry, ...]:
    return (
        system.CrypttabEntry(name="root", backing=i("crypt"), initrd_attach=True),
        system.CrypttabEntry(name="data", backing=i("crypt2"), initrd_attach=False),
    )


def test_an_openrc_storage_service_is_enabled_after_its_package_is_merged() -> None:
    """`rc-update add lvm boot` exits 1 with `service does not exist` until
    sys-fs/lvm2 is installed, and that happens with the kernel stack."""
    from pathlib import Path

    from gentoo_install.data import load_catalog
    from gentoo_install.exec.config import load
    from gentoo_install.plan.build import build

    for fixture, package, service in (
        ("vm-lvm", "sys-fs/lvm2", "lvm"),
        ("vm-mdraid", "sys-fs/mdadm", "mdraid"),
    ):
        loaded = load(Path(f"tests/fixtures/{fixture}.toml"))
        openrc = replace(
            loaded,
            system=replace(loaded.system, init=InitSystem.OPENRC),
            portage=replace(loaded.portage, profile="default/linux/amd64/23.0"),
        )
        described = [one.describe() for one in build(openrc, load_catalog())]
        merged = next(n for n, line in enumerate(described) if package in line)
        enabled = next(n for n, line in enumerate(described) if line.startswith(f"enable {service} "))
        assert merged < enabled, fixture


def test_dmcrypt_waits_for_cryptsetup_the_same_way() -> None:
    """The third openrc storage service, and the only one a fixture cannot
    reach: every fixture's container holds the root, which the initramfs opens."""
    nodes: list[Node] = [
        *ext4_on_gpt(),
        Partition(
            id=i("datapart"), table=i("table"), index=3, role=PartitionRole.DATA, size=None
        ),
        Luks(id=i("crypt"), backing=i("datapart"), name="data"),
        Filesystem(id=i("datafs"), device=i("crypt"), kind=FilesystemType.EXT4),
        Mountpoint(id=i("mnt-data"), source=i("datafs"), path=PurePosixPath("/data")),
    ]
    services = [
        one
        for one in system.build(
            replace(config(nodes), system=SystemConfig(init=InitSystem.OPENRC))
        )
        if isinstance(one, system.EnableService) and one.service == "dmcrypt"
    ]
    assert [one.stage for one in services] == [system.STORAGE_SERVICE_STAGE]


def test_openrc_gets_a_logger_and_cron_because_a_stage3_has_neither() -> None:
    """An openrc install without these keeps no record of its own boot. The
    handbook names three loggers; systemd carries journald and needs none."""
    from gentoo_install.model.config import Logger

    openrc = [one.describe() for one in system.build(static(init=InitSystem.OPENRC))]
    assert any("app-admin/sysklogd" in one for one in openrc)
    assert "enable sysklogd in the default runlevel" in openrc
    assert any("sys-process/cronie" in one for one in openrc)

    systemd = [one.describe() for one in system.build(static())]
    assert not any("app-admin/" in one for one in systemd)
    assert any("sys-process/cronie" in one for one in systemd)

    quiet = static(init=InitSystem.OPENRC, logger=Logger.NONE, cron=False)
    described = [one.describe() for one in system.build(quiet)]
    assert not any("app-admin/" in one or "cronie" in one for one in described)


def test_openrc_brings_up_the_storage_stack_it_has_a_service_for() -> None:
    """systemd has generators; openrc has one service per kind, and without
    them a volume group that does not carry the root never activates."""
    from pathlib import Path

    from gentoo_install.exec.config import load

    # The fixture is already openrc; the systemd half has to be built from it.
    lvm = load(Path("tests/fixtures/vm-lvm.toml"))
    assert lvm.system.init is InitSystem.OPENRC
    services = [
        one.service for one in system.build(lvm) if isinstance(one, system.EnableService)
    ]
    assert "lvm" in services

    systemd = replace(
        lvm,
        system=replace(lvm.system, init=InitSystem.SYSTEMD),
        portage=replace(lvm.portage, profile="default/linux/amd64/23.0/systemd"),
    )
    assert "lvm" not in [
        one.service for one in system.build(systemd) if isinstance(one, system.EnableService)
    ]


def test_a_pool_gets_the_services_that_import_it_and_mount_its_datasets() -> None:
    """The initramfs brings up the root dataset and nothing else, so `/home` on
    its own dataset came up empty on a system with no ZFS service enabled."""
    from .layouts import zfs_root

    on_zfs = replace(config(zfs_root()), system=SystemConfig(init=InitSystem.SYSTEMD))
    units = [
        one.service for one in system.build(on_zfs) if isinstance(one, system.EnableService)
    ]
    assert {"zfs-import-scan.service", "zfs-mount.service", "zfs.target"} <= set(units)

    # `zfs.target.service` is not a unit, so a name with a suffix passes through.
    recorder = Recorder()
    system.EnableService(service="zfs.target", init=InitSystem.SYSTEMD).apply(recorder)
    assert ("systemctl", "enable", "zfs.target") in recorder.in_target

    openrc = replace(
        config(zfs_root()),
        system=SystemConfig(init=InitSystem.OPENRC),
        portage=replace(config().portage, profile="default/linux/amd64/23.0"),
    )
    boot = [
        one.service
        for one in system.build(openrc)
        if isinstance(one, system.EnableService) and one.runlevel == "boot"
    ]
    assert {"zfs-import", "zfs-mount"} <= set(boot)
    # No pool, no services: a rule that fires on every layout is not a rule.
    plain = [one.service for one in system.build(config()) if isinstance(one, system.EnableService)]
    assert not any(name.startswith("zfs") for name in plain)


def test_an_encrypted_pool_loads_its_key_between_import_and_mount() -> None:
    """openrc's `zfs-mount` skips a dataset whose key is not loaded, and the
    initramfs unlocked only the root one."""
    from dataclasses import replace as _replace

    from gentoo_install.model.device import ZfsPool

    from .layouts import zfs_root

    nodes = [
        _replace(node, passphrase_file="/run/keys/pool") if isinstance(node, ZfsPool) else node
        for node in zfs_root()
    ]
    encrypted = replace(
        config(nodes),
        system=SystemConfig(init=InitSystem.OPENRC),
        portage=replace(config().portage, profile="default/linux/amd64/23.0"),
    )
    order = [
        one.service for one in system.build(encrypted) if isinstance(one, system.EnableService)
    ]
    assert order.index("zfs-import") < order.index("zfs-load-key") < order.index("zfs-mount")


def test_every_logger_has_a_package_a_service_and_a_row() -> None:
    """It was two tables of the same name: `plan/system.py` held the package
    and the service, `tui/screens.py` held the menu row. A logger added to one
    and forgotten in the other was offered and then silently not installed."""
    assert set(system.LOGGERS) == set(Logger)
    for chosen, entry in system.LOGGERS.items():
        assert entry.reason, chosen
        # NONE is the only member with nothing to merge.
        assert bool(entry.package) is bool(entry.service)
        assert bool(entry.package) is (chosen is not Logger.NONE), chosen

    picked = with_system(init=InitSystem.OPENRC, logger=Logger.METALOG)
    merged = [
        one for one in system.build(picked) if isinstance(one, Emerge) and "logger" in one.summary
    ]
    assert merged and merged[0].packages == ("app-admin/metalog",)


def test_the_installed_system_resolves_with_its_own_nameservers() -> None:
    """`SeedResolver` copies the install medium's `/etc/resolv.conf` in and
    nothing takes it out, and `systemd-networkd` publishes what it learns only
    through `systemd-resolved`, so the DNS the operator typed reached nothing."""
    static = with_system(
        init=InitSystem.SYSTEMD,
        networking=Networking.BUILTIN,
        addresses=("192.0.2.10/24",),
        gateways=("192.0.2.1",),
        dns=("223.5.5.5",),
    )
    recorder = apply_all(static, generated=generated(static))
    written = recorder.files[PurePosixPath("/etc/systemd/network/20-wired.network")]
    assert "DNS=223.5.5.5" in written
    assert ("systemctl", "enable", "systemd-resolved.service") in recorder.in_target
    # `ln` also makes /etc/localtime, so the resolver link is picked out by name.
    linked = next(
        argv for argv in recorder.argv_starting("ln") if argv[-1] == "/etc/resolv.conf"
    )
    assert linked[-2] == "../run/systemd/resolve/stub-resolv.conf"

    # netifrc writes resolv.conf itself, so openrc needs none of it.
    openrc = with_system(
        init=InitSystem.OPENRC,
        interface="enp1s0",
        addresses=("192.0.2.10/24",),
        dns=("223.5.5.5",),
    )
    plain = apply_all(openrc, generated=generated(openrc))
    assert "dns_servers_" in plain.files[PurePosixPath("/etc/conf.d/net")]
    assert not any("resolved" in " ".join(argv) for argv in plain.in_target)


def test_a_static_address_reaches_every_manager_that_can_take_one() -> None:
    """It was written for the init's own manager and dropped for
    NetworkManager, so an operator who typed an address under NetworkManager
    got a machine on DHCP and no word about it."""
    static = dict(
        interface="eth0",
        addresses=("192.0.2.10/24", "2001:db8::10/64"),
        gateways=("192.0.2.1", "2001:db8::1"),
        dns=("223.5.5.5", "2400:3200::1"),
    )
    for init in InitSystem:
        for manager in (Networking.NETWORKMANAGER_WPA, Networking.NETWORKMANAGER_IWD):
            chosen = with_system(init=init, networking=manager, **static)
            written = apply_all(chosen, generated=DEFAULT_LOCALES).files
            profile = written[system.NM_PROFILE]
            # One family per section, and the gateway rides on the first
            # address of its own family.
            assert "[ipv4]\nmethod=manual\naddress1=192.0.2.10/24,192.0.2.1" in profile
            assert "[ipv6]\nmethod=manual\naddress1=2001:db8::10/64,2001:db8::1" in profile
            assert "dns=223.5.5.5;" in profile and "dns=2400:3200::1;" in profile
            assert "interface-name=eth0" in profile


def test_networkmanager_on_dhcp_still_needs_no_file() -> None:
    """It manages every unconfigured interface, so a file saying so is a file
    that changes nothing."""
    for manager in (Networking.NETWORKMANAGER_WPA, Networking.NETWORKMANAGER_IWD):
        chosen = with_system(networking=manager)
        assert system.NM_PROFILE not in apply_all(chosen, generated=DEFAULT_LOCALES).files


def test_one_family_alone_leaves_the_other_on_automatic() -> None:
    """`disabled` would switch the other family off, and a v4-only answer is
    not a decision about v6."""
    only_v4 = with_system(
        networking=Networking.NETWORKMANAGER_WPA, addresses=("192.0.2.10/24",)
    )
    profile = apply_all(only_v4, generated=DEFAULT_LOCALES).files[system.NM_PROFILE]
    assert "[ipv4]\nmethod=manual" in profile
    assert "[ipv6]\nmethod=auto" in profile


def test_the_profile_is_written_with_the_mode_networkmanager_demands() -> None:
    """It refuses a world-readable keyfile and says so only in its own log,
    while the machine sits there with no address."""
    chosen = with_system(networking=Networking.NETWORKMANAGER_WPA, addresses=("192.0.2.10/24",))
    written = next(
        one for one in system.build(chosen) if isinstance(one, system.WriteNetworkConfig)
    )
    recorder = Recorder()
    written.apply(recorder)
    assert recorder.modes[system.NM_PROFILE] == 0o600


def test_the_resolver_link_is_the_last_thing_written() -> None:
    """It points at a socket systemd-resolved only creates once the installed
    system boots. Written before the emerges, it takes the copied resolver away
    from every one of them and the install dies on name resolution."""
    from pathlib import Path as FilePath

    from gentoo_install.data import load_catalog as catalog_of
    from gentoo_install.exec.config import load as load_config
    from gentoo_install.plan.build import build as whole_plan
    from gentoo_install.plan.portage import Emerge
    from gentoo_install.plan.system import LinkResolvConf

    operations = whole_plan(load_config(FilePath("tests/fixtures/vm-binpkg.toml")), catalog_of())
    linked = next(
        at for at, one in enumerate(operations) if isinstance(one, LinkResolvConf)
    )
    last_merge = max(at for at, one in enumerate(operations) if isinstance(one, Emerge))
    unmounted = next(
        at for at, one in enumerate(operations) if one.describe().startswith("unmount")
    )
    assert last_merge < linked < unmounted


def test_building_in_ram_writes_a_tmpfs_portage_can_write_into() -> None:
    """portage runs as 250:250 and has to create directories under it, so an
    entry with default options gives root-only 1777 and the first build fails
    on EACCES. `size=` is the whole point of the row: without it a tmpfs takes
    half of memory and a Chromium build takes the machine down."""
    installation = replace(
        config(ext4_on_gpt()),
        portage=replace(config().portage, build_in_ram=Size(16 * 1024**3)),
    )
    written = apply_all(installation, generated=generated(installation)).files[
        PurePosixPath("/etc/fstab")
    ]
    line = next(one for one in written.splitlines() if "tmpfs" in one)
    fields = line.split("\t")
    assert fields[0] == "tmpfs" and fields[1] == "/var/tmp/portage"
    options = fields[3].split(",")
    assert "size=16G" in options
    assert "uid=250" in options and "gid=250" in options and "mode=0775" in options
    assert "nodev" in options and "nosuid" in options


def test_nothing_is_mounted_on_var_tmp_when_the_row_is_off() -> None:
    """Off is the default: a build that outgrows the tmpfs dies on ENOSPC an
    hour in, which is worse than building on disk."""
    plain = config(ext4_on_gpt())
    written = apply_all(plain, generated=generated(plain)).files[PurePosixPath("/etc/fstab")]
    assert "tmpfs" not in written


def test_the_first_boot_script_is_fetched_while_the_installer_still_can() -> None:
    """A download that fails at first boot leaves a machine half-configured
    with nobody watching, and the operator cannot read beforehand what is
    about to run as root."""
    from gentoo_install.model.config import FirstBoot
    from gentoo_install.plan.system import FIRST_BOOT_SCRIPT, FIRST_BOOT_UNIT

    wanted = with_system(
        first_boot=FirstBoot(commands=("emerge --sync",), url="https://example.com/setup.sh")
    )
    recorder = Recorder()
    recorder.replies["locale"] = generated(wanted)
    recorder.pages["https://example.com/setup.sh"] = "echo from the script\n"
    for operation in system.build(wanted):
        operation.apply(recorder)
    written = recorder.files[FIRST_BOOT_SCRIPT]
    assert ("fetch-text", "https://example.com/setup.sh") in recorder.commands
    assert "echo from the script" in written
    assert "emerge --sync" in written
    # `set -e`, or a step that failed is indistinguishable from one that never
    # ran; the removal is last, or a failure leaves nothing to look at.
    assert written.splitlines()[1] == "set -e"
    assert written.rstrip().endswith(f"rm -f {FIRST_BOOT_SCRIPT}")
    assert recorder.modes[FIRST_BOOT_SCRIPT] == 0o700
    assert "WantedBy=multi-user.target" in recorder.files[FIRST_BOOT_UNIT]


def test_openrc_starts_it_through_local_d() -> None:
    """systemd gets a unit; openrc runs every `/etc/local.d/*.start` through
    the `local` service, which is enabled rather than assumed."""
    from gentoo_install.model.config import FirstBoot, InitSystem
    from gentoo_install.plan.system import FIRST_BOOT_OPENRC, EnableService

    wanted = with_system(init=InitSystem.OPENRC, first_boot=FirstBoot(commands=("true",)))
    built = system.build(wanted)
    recorder = Recorder()
    recorder.replies["locale"] = generated(wanted)
    for operation in built:
        operation.apply(recorder)
    assert FIRST_BOOT_OPENRC in recorder.files
    assert recorder.modes[FIRST_BOOT_OPENRC] == 0o755
    assert any(
        isinstance(one, EnableService) and one.service == "local" for one in built
    )


def test_nothing_is_written_when_no_first_boot_work_was_asked_for() -> None:
    """A unit that runs an empty script is one more thing to explain."""
    from gentoo_install.plan.system import FIRST_BOOT_SCRIPT

    plain = config()
    recorder = apply_all(plain, generated=generated(plain))
    assert FIRST_BOOT_SCRIPT not in recorder.files


def test_the_host_keys_exist_before_dracut_converts_them() -> None:
    """`net-misc/openssh` makes none at merge time and sshd makes them the
    first time it starts, which is after this install ends. `dracut-crypt-ssh`
    reads them at initramfs build time, so with none the remote-unlock daemon
    comes up with a key the operator's client has never seen."""
    from pathlib import Path as Where

    from gentoo_install.data import load_catalog
    from gentoo_install.exec.config import load as load_config
    from gentoo_install.plan.build import build as build_plan

    where = Where(__file__).resolve().parents[1] / "fixtures" / "vm-unlock.toml"
    described = [one.describe() for one in build_plan(load_config(where), load_catalog())]
    keys = next(n for n, one in enumerate(described) if "host keys" in one)
    dracut = next(n for n, one in enumerate(described) if "rebuild the initramfs" in one)
    assert keys < dracut, described[keys : dracut + 1]


def test_no_host_keys_are_made_for_a_system_with_no_sshd() -> None:
    """`ssh-keygen` comes with openssh, which that system does not install."""
    from gentoo_install.plan.system import GenerateHostKeys

    plain = with_system(sshd=False)
    assert not [one for one in system.build(plain) if isinstance(one, GenerateHostKeys)]


def test_remote_unlock_gets_host_keys_without_enabling_target_sshd() -> None:
    """`dropbear_*_key="SYSTEM"` converts the target's own host keys, and they
    were generated only under `system.sshd`. With sshd off the initramfs had
    no host key and the machine stayed locked."""
    from dataclasses import replace

    from gentoo_install.data import load_catalog
    from gentoo_install.model.config import KernelConfig, RemoteUnlock
    from gentoo_install.plan.build import build

    from .layouts import config, unlockable_root

    base = config(unlockable_root())
    installation = replace(
        base,
        system=replace(base.system, sshd=False, authorized_keys=("ssh-ed25519 AAAA test",)),
        kernel=replace(KernelConfig(), remote_unlock=RemoteUnlock(enabled=True, interface="eth0")),
    )
    described = [one.describe() for one in build(installation, load_catalog())]
    assert any("host keys" in one for one in described), described
    # And no sshd service: the keys are an initramfs prerequisite, not a server.
    assert not [one for one in described if "enable sshd" in one], described


def test_the_unlock_key_always_reaches_root() -> None:
    """dracut-crypt-ssh reads `/root/.ssh/authorized_keys` and nothing else, so
    a key that went only to a sudo user left the machine locked before that
    account existed."""
    from dataclasses import replace

    from gentoo_install.model.config import KernelConfig, RemoteUnlock, SystemConfig, User
    from gentoo_install.plan.system import key_accounts

    system = SystemConfig(
        sshd_root_login=False,
        users=(User(name="zakk", sudo=True, password_hash="$6$x$y"),),
        authorized_keys=("ssh-ed25519 AAAA test",),
    )
    assert "root" not in {name for name, _ in key_accounts(system)}
    assert "root" in {name for name, _ in key_accounts(system, unlocking=True)}
    assert KernelConfig().remote_unlock == RemoteUnlock()


def test_no_proxy_writes_no_proxy_environment() -> None:
    """`/etc/environment` is replaced rather than appended to, so an install
    with no proxy left ten empty variables there and discarded whatever the
    file already carried."""
    from gentoo_install.plan import system as plan_system

    written = [
        one for one in plan_system.build(config())
        if isinstance(one, plan_system.WriteProxyEnvironment)
    ]

    assert written == []


def test_a_configured_proxy_still_reaches_the_installed_system() -> None:
    from gentoo_install.model.config import ProxyConfig
    from gentoo_install.plan import system as plan_system

    installation = replace(
        config(),
        proxy=ProxyConfig(
            kind=ProxyKind.HTTP, host="proxy.example", port=8080,
            username="operator", password="secret",
        ),
    )
    written = [
        one for one in plan_system.build(installation)
        if isinstance(one, plan_system.WriteProxyEnvironment)
    ]

    assert len(written) == 1
    assert "secret" not in written[0].describe()
