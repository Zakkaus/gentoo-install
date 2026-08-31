# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import pytest

from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Sequence, cast

from gentoo_install.exec.config import load
from gentoo_install.model.architecture import DEFAULT_ARCHITECTURE
from gentoo_install.model.config import (
    Bootloader, BootloaderConfig, DiskMode, Firmware, InstallConfig, RemoteUnlock
)
from gentoo_install.model.size import Size
from gentoo_install.plan import bootloader, kernel
from gentoo_install.plan.operations import CommandOutput
from gentoo_install.plan.portage import Emerge

from .recorder import Recorder


def zfsbootmenu_remote_installation() -> InstallConfig:
    installation = load(Path("tests/fixtures/vm-zfs-encrypted.toml"))
    return replace(
        installation,
        system=replace(
            installation.system,
            authorized_keys=("ssh-ed25519 operator-key remote@example",),
        ),
        kernel=replace(
            installation.kernel,
            remote_unlock=RemoteUnlock(
                enabled=True,
                port=2201,
                address="192.0.2.10/24",
                gateway="192.0.2.1",
                interface="eth0",
            ),
        ),
    )


def test_zfsbootmenu_carries_remote_access_in_its_own_image() -> None:
    installation = zfsbootmenu_remote_installation()
    boot_operations = bootloader.build(installation)
    configured = next(
        operation
        for operation in boot_operations
        if isinstance(operation, bootloader.ConfigureZfsBootMenuRemoteAccess)
    )
    generated = next(
        index
        for index, operation in enumerate(boot_operations)
        if isinstance(operation, bootloader.InstallZfsBootMenu)
    )
    installed = boot_operations[generated]
    assert isinstance(installed, bootloader.InstallZfsBootMenu)
    assert "rd.neednet=1" not in installed.kernel_params
    assert not any(parameter.startswith("ip=") for parameter in installed.kernel_params)
    assert boot_operations.index(configured) < generated
    assert "/etc/zfsbootmenu/dracut.conf.d/dropbear.conf" in configured.describe()
    assert "/etc/dropbear" in configured.describe()

    recorder = Recorder()
    configured.apply(recorder)
    zbm = recorder.files[PurePosixPath("/etc/zfsbootmenu/dracut.conf.d/dropbear.conf")]
    assert "crypt-ssh" in zbm, zbm
    assert 'dropbear_port="2201"' in zbm
    # `generate-zbm` exited 1 with "Module 'crypt-ssh' depends on module
    # 'network', which can't be installed": dracut's `40network` resolves to
    # `systemd-networkd`, which needs the `systemd` module this image lacks.
    # Naming `network-legacy` makes `40network` pick it, per its own
    # `depends()`, which prefers an implementation already included.
    assert "network-legacy" in zbm, zbm
    assert (
        'omit_dracutmodules+=" systemd-networkd systemd-battery-check "' in zbm
    ), zbm
    assert "dropbear_rsa_key=/etc/dropbear/ssh_host_rsa_key" in zbm
    assert "dropbear_ecdsa_key=/etc/dropbear/ssh_host_ecdsa_key" in zbm
    assert "dropbear_acl=/etc/dropbear/root_key" in zbm
    assert recorder.files[PurePosixPath("/etc/dropbear/root_key")] == (
        "ssh-ed25519 operator-key remote@example\n"
    )
    assert recorder.modes[PurePosixPath("/etc/dropbear/root_key")] == 0o600
    assert recorder.files[PurePosixPath("/etc/cmdline.d/dracut-network.conf")] == (
        "ip=192.0.2.10::192.0.2.1:255.255.255.0::eth0:none rd.neednet=1\n"
    )
    # One per host key type, and the type list is what the format allows: see
    # test_every_zfsbootmenu_host_key_can_be_written_in_the_format_asked_for.
    assert len(recorder.argv_starting("ssh-keygen")) == len(bootloader.ZBM_HOST_KEY_TYPES)

    kernel_recorder = Recorder()
    for operation in kernel.build(installation):
        if isinstance(operation, kernel.ConfigureRemoteUnlock):
            operation.apply(kernel_recorder)
    assert kernel_recorder.files[kernel.REMOTE_UNLOCK_CONFIG] == (
        'omit_dracutmodules+=" crypt-ssh "\n'
    )
    assert "crypt-ssh" not in kernel.dracut_modules(installation)
    assert "network" not in kernel.dracut_modules(installation)


def test_native_zfs_unlock_uses_the_initramfs_keymap() -> None:
    encrypted = load(Path("tests/fixtures/vm-zfs-encrypted.toml"))
    encrypted = replace(
        encrypted,
        system=replace(encrypted.system, keymap_initramfs="de"),
    )
    plaintext = load(Path("tests/fixtures/vm-zfs.toml"))
    plaintext = replace(
        plaintext,
        system=replace(plaintext.system, keymap_initramfs="de"),
    )

    assert bootloader.initramfs_keymap(encrypted) == "de"
    assert bootloader.initramfs_keymap(plaintext) == ""
    assert bootloader.initramfs_keymap(
        replace(encrypted, system=replace(encrypted.system, keymap_initramfs="us"))
    ) == ""


def test_a_systemd_boot_machine_shows_its_menu() -> None:
    """`bootctl install` writes no `loader.conf`, and systemd-boot's documented
    default for `timeout` is 0: "no menu is shown and the default entry will be
    booted immediately". An encrypted openrc machine went from firmware
    straight to the passphrase prompt, with no way to pick an older kernel.
    """
    installation = load(Path("tests/fixtures/openrc-sdboot.toml"))
    operations = bootloader.build(installation)
    shown = [one for one in operations if isinstance(one, bootloader.ShowTheBootMenu)]
    assert len(shown) == 1, [one.describe() for one in operations]
    assert shown[0].seconds > 0, shown[0]

    recorder = Recorder()
    shown[0].apply(recorder)
    written = recorder.files[shown[0].esp / "loader" / "loader.conf"]
    assert f"timeout {shown[0].seconds}" in written, written


def test_a_grub_machine_writes_no_loader_conf() -> None:
    """The menu timeout for GRUB is `GRUB_TIMEOUT` in `/etc/default/grub`, and
    a `loader.conf` beside it would be read by nothing."""
    installation = load(Path("tests/fixtures/vm-btrfs.toml"))
    operations = bootloader.build(installation)
    assert not [one for one in operations if isinstance(one, bootloader.ShowTheBootMenu)]


def test_both_bootloaders_wait_the_same_time() -> None:
    """`MENU_SECONDS` says it is the same five seconds `GRUB_TIMEOUT` uses, so
    that the two behave alike from the operator's side. GRUB's was a separate
    literal, which is a promise nothing could keep.
    """
    import ast
    import inspect
    import re

    installation = load(Path("tests/fixtures/vm-btrfs.toml"))
    defaults = next(
        one
        for one in bootloader.build(installation)
        if isinstance(one, bootloader.WriteGrubDefaults)
    )
    recorder = Recorder()
    defaults.apply(recorder)
    written = recorder.files[PurePosixPath("/etc/default/grub")]
    assert f"GRUB_TIMEOUT={bootloader.MENU_SECONDS}" in written, written

    # The literal and the constant read the same today, so only the source
    # says whether there is one timeout or two.
    tree = ast.parse(inspect.getsource(bootloader.WriteGrubDefaults.apply).lstrip())
    spelled = [
        one.value
        for one in ast.walk(tree)
        if isinstance(one, ast.Constant) and isinstance(one.value, str)
    ]
    # The f-string's own `GRUB_TIMEOUT=` prefix is fine; a number after it is
    # the second spelling of the timeout.
    assert not [one for one in spelled if re.match(r"GRUB_TIMEOUT=\d", one)], spelled


def test_bios_grub_targets_every_mirrored_root_member() -> None:
    installation = replace(
        load(Path("tests/fixtures/vm-mdraid.toml")),
        bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.BIOS),
    )
    operation = next(
        one for one in bootloader.build(installation) if isinstance(one, bootloader.InstallGrub)
    )
    assert operation.boot_devices == ("disk0", "disk1")

    class MirroredRecorder(Recorder):
        def containing_disk(self, device: str) -> str:
            return f"/dev/{device}"

    recorder = MirroredRecorder()
    operation.apply(recorder)
    assert recorder.argv_starting("grub-install", "--target=i386-pc") == (
        ("grub-install", "--target=i386-pc", "/dev/disk0"),
        ("grub-install", "--target=i386-pc", "/dev/disk1"),
    )


def test_every_zfsbootmenu_host_key_can_be_written_in_the_format_asked_for() -> None:
    """`ssh-keygen -m PEM` refuses ed25519 with "Saving key failed: invalid
    format", so the install stopped at exit 4 in the middle of writing
    ZFSBootMenu's remote access. Measured with `ssh-keygen` itself; RSA and
    ECDSA both write `-----BEGIN RSA PRIVATE KEY-----` and
    `-----BEGIN EC PRIVATE KEY-----`.

    ZFSBootMenu's own documentation names `dropbear_rsa_key` and
    `dropbear_ecdsa_key` and no third one.
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from gentoo_install.plan import bootloader

    assert "ed25519" not in bootloader.ZBM_HOST_KEY_TYPES, bootloader.ZBM_HOST_KEY_TYPES
    keygen = shutil.which("ssh-keygen")
    if keygen is None:  # pragma: no cover - the medium always carries it
        pytest.skip("ssh-keygen is not on this machine")
    with tempfile.TemporaryDirectory() as where:
        for keytype in bootloader.ZBM_HOST_KEY_TYPES:
            target = Path(where) / f"ssh_host_{keytype}_key"
            done = subprocess.run(
                [keygen, "-q", "-N", "", "-m", "PEM", "-t", keytype, "-f", str(target)],
                capture_output=True,
            )
            assert target.is_file(), (keytype, done.stdout, done.stderr)
            assert target.read_bytes().startswith(b"-----BEGIN "), keytype


def test_the_bios_grub_describe_names_every_disk_that_gets_a_boot_sector() -> None:
    """The BIOS branch writes a boot sector to the containing disk of each boot
    device, and a mirrored root has two. `--dry-run` said "the boot disk",
    naming neither, so an operator could not see which disks the run rewrites.
    """
    from gentoo_install.model.device import DeviceId

    mirrored = bootloader.InstallGrub(
        firmware=Firmware.BIOS,
        esp=None,
        boot_devices=(DeviceId("first"), DeviceId("second")),
    )
    said = mirrored.describe()
    assert "first" in said and "second" in said, said
    assert "disks" in said, said

    # Not a claim about the text alone: apply writes to exactly those disks.
    class Disks(Recorder):
        def containing_disk(self, device: DeviceId) -> str:
            return f"/dev/{device}"

    recorder = Disks(replies={"grep": "1"})
    mirrored.apply(recorder)
    installed = [one[-1] for one in recorder.in_target if one[0] == "grub-install"]
    assert installed == ["/dev/first", "/dev/second"], installed
    for disk in installed:
        assert disk.removeprefix("/dev/") in said, (disk, said)

    # Negative control: the UEFI branch installs on the esp and names it, and
    # must not start listing devices it does not write a boot sector to.
    on_esp = bootloader.InstallGrub(
        firmware=Firmware.UEFI,
        esp=PurePosixPath("/efi"),
        boot_devices=(DeviceId("first"),),
    )
    assert "/efi" in on_esp.describe()
    assert "first" not in on_esp.describe(), on_esp.describe()


def test_a_firmware_that_refuses_a_boot_entry_still_leaves_a_bootable_machine() -> None:
    """`EFI/BOOT/BOOTX64.EFI` is what firmware boots with no entry at all, and
    both bootloaders write it. An NVRAM that is full or read-only belongs to
    the machine, so failing the install there loses a system that boots."""
    from gentoo_install.errors import CommandFailed
    from gentoo_install.model.device import DeviceId

    class NoRoomInNvram(Recorder):
        #: Every attempt including the refused one, which `in_target` does not
        #: hold: without it the order assertion below passes either way.
        attempts: list[tuple[str, ...]] = []

        def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> CommandOutput:
            self.attempts.append(tuple(argv))
            if "--bootloader-id=Gentoo" in argv or argv[0] == "efibootmgr":
                raise CommandFailed(
                    "efibootmgr: Could not prepare Boot variable: No space left on device"
                )
            return super().run_in_target(argv, check=check)

        def containing_disk(self, device: DeviceId) -> str:
            return "/dev/vda"

    on_esp = bootloader.InstallGrub(
        firmware=Firmware.UEFI, esp=PurePosixPath("/efi"), boot_devices=()
    )
    recorder = NoRoomInNvram(replies={"grep": "1"})
    recorder.attempts = []
    on_esp.apply(recorder)

    assert recorder.degraded(bootloader.NVRAM_ENTRY)
    # The removable image is written before the entry is asked for, or the
    # refusal would leave nothing behind at all.
    tried = [one for one in recorder.attempts if one[0] == "grub-install"]
    assert "--removable" in tried[0], tried
    assert "--bootloader-id=Gentoo" in tried[1], tried
    assert "grub-mkconfig" in {one[0] for one in recorder.in_target}, recorder.in_target

    # Negative control: a firmware that takes the entry is not degraded, and
    # both installs still run.
    working = Recorder(replies={"grep": "1"})
    on_esp.apply(working)
    assert not working.degraded(bootloader.NVRAM_ENTRY)
    assert len([one for one in working.in_target if one[0] == "grub-install"]) == 2

    # Negative control: a `grub-install` that fails for any other reason is not
    # a refused boot entry and still stops the install.
    class Broken(Recorder):
        def run_in_target(self, argv: Sequence[str], *, check: bool = True) -> CommandOutput:
            if argv[0] == "grub-install":
                raise CommandFailed("grub-install: error: cannot find EFI directory")
            return super().run_in_target(argv, check=check)

    with pytest.raises(CommandFailed):
        on_esp.apply(Broken(replies={"grep": "1"}))


def test_an_image_forces_grub_and_leaves_the_installer_nvram_alone() -> None:
    installation = load(Path("tests/fixtures/vm-btrfs.toml"))
    image = replace(
        installation,
        disk=replace(
            installation.disk,
            mode=DiskMode.IMAGE,
            image="/var/tmp/target.raw",
            size=Size.parse("20GiB"),
        ),
    )
    operations = bootloader.build(image)
    install = next(one for one in operations if isinstance(one, bootloader.InstallGrub))
    assert install.force
    assert not install.write_nvram
    assert all(
        bootloader.EFI_PACKAGE not in one.packages
        for one in operations
        if isinstance(one, Emerge)
    )

    recorder = Recorder(replies={"grep": "1"})
    install.apply(recorder)
    invoked = [one for one in recorder.in_target if one[0] == "grub-install"]
    assert len(invoked) == 1
    assert "--force" in invoked[0]
    assert "--removable" in invoked[0]
    assert "--bootloader-id=Gentoo" not in invoked[0]


def test_zfsbootmenu_and_the_unlock_on_top_of_it_are_separate_fixtures() -> None:
    """`zfs-zbm` carried both. Three cluster runs built the pool, generated the
    image and booted a machine from it, and were recorded as failures because
    an ssh daemon did not answer on 2222 — so nothing ever reported on
    ZFSBootMenu itself. A fixture answering for two features answers for
    neither."""
    from pathlib import Path

    from gentoo_install.exec.config import load
    from gentoo_install.model.config import Bootloader

    plain = load(Path("tests/fixtures/zfs-zbm.toml"))
    both = load(Path("tests/fixtures/zbm-unlock.toml"))

    for one in (plain, both):
        assert one.bootloader.kind is Bootloader.ZFSBOOTMENU

    assert not plain.kernel.remote_unlock.enabled, "zfs-zbm answers for ZFSBootMenu alone"
    assert both.kernel.remote_unlock.enabled, "and the combination keeps a fixture"

    # The operation that carries the combination reaches one plan and not the
    # other, which is what makes the split a division of work rather than two
    # copies of one fixture.
    def builds_remote_access(installation: object) -> bool:
        from gentoo_install.model.config import InstallConfig

        assert isinstance(installation, InstallConfig)
        return any(
            isinstance(one, bootloader.ConfigureZfsBootMenuRemoteAccess)
            for one in bootloader.build(installation)
        )

    assert not builds_remote_access(plain)
    assert builds_remote_access(both)

    # Two machines, so a guest that booted the wrong disk is told apart.
    assert plain.system.hostname != both.system.hostname


def test_an_image_built_without_the_unlock_daemon_is_said_rather_than_shipped() -> None:
    """`generate-zbm` prints two lines and swallows dracut's own output. Read
    from the image an install produced, `etc/dropbear/` held the two host keys
    and nothing else — no authorized key, no start hook — so the machine booted
    and nothing ever answered on the forwarded port.

    Said rather than raised: the console passphrase still unlocks it.
    """

    installation = load(Path("tests/fixtures/zbm-unlock.toml"))
    built = next(
        one
        for one in bootloader.build(installation)
        if isinstance(one, bootloader.InstallZfsBootMenu)
    )
    assert built.unlocks_remotely

    host_keys_only = "\n".join(
        (
            "etc/dropbear",
            "etc/dropbear/dropbear_ecdsa_host_key",
            "etc/dropbear/dropbear_rsa_host_key",
        )
    )

    class Listing(Recorder):
        answer = host_keys_only

        def run_in_target(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.in_target.append(tuple(argv))
            if argv[0] == "find":
                return CommandOutput("/efi/EFI/zbm/kernel.EFI\n", 0)
            if argv[0] == "lsinitrd":
                return CommandOutput(self.answer, 0)
            return CommandOutput("", 0)

    recorder = Listing()
    built.apply(recorder)
    assert recorder.degraded(bootloader.REMOTE_UNLOCK_IMAGE), recorder.in_target

    # Negative control one: an image carrying the acl is not degraded, or every
    # working unlock would be reported as broken.
    working = Listing()
    working.answer = f"{host_keys_only}\nroot/.ssh/authorized_keys"
    built.apply(working)
    assert not working.degraded(bootloader.REMOTE_UNLOCK_IMAGE)

    # Negative control two: an install that asked for no remote unlock never
    # reads the image, so a missing daemon is not reported on it.
    quiet = Listing()
    quiet.answer = host_keys_only
    replace(built, unlocks_remotely=False).apply(quiet)
    assert not quiet.degraded(bootloader.REMOTE_UNLOCK_IMAGE)
    assert not [one for one in quiet.in_target if one[0] == "lsinitrd"], quiet.in_target


def test_the_unlock_names_the_key_types_the_module_may_generate() -> None:
    """Read from `60crypt-ssh/module-setup.sh` v1.0.8, whose `install()` is:

        [[ -z "${dropbear_keytypes}" ]] && dropbear_keytypes="rsa ecdsa ed25519"
        ...
        ssh-keygen -t $keyType -f $osshKey -q -N "" -m PEM || { derror; return 1; }
        ...
        inst $dropbearKey $installKey        # per key type, inside the loop
        ...
        inst_hook pre-udev 99 dropbear-start.sh
        inst "${dropbear_acl}" /root/.ssh/authorized_keys

    `ssh-keygen -m PEM` refuses ed25519, which is why `ZBM_HOST_KEY_TYPES` has
    only two entries. Leaving the module on its default put it through the
    ed25519 iteration, so it returned after installing the two host keys and
    before the acl and the hook. The image an install produced held exactly
    `etc/dropbear/dropbear_{ecdsa,rsa}_host_key` and nothing else.
    """
    installation = load(Path("tests/fixtures/zbm-unlock.toml"))
    configured = next(
        one
        for one in bootloader.build(installation)
        if isinstance(one, bootloader.ConfigureZfsBootMenuRemoteAccess)
    )
    recorder = Recorder()
    configured.apply(recorder)
    written = recorder.files[bootloader.ZBM_REMOTE_CONFIG]

    assert 'dropbear_keytypes="rsa ecdsa"' in written, written
    # One table, not two: the types the module may generate are the types this
    # installer converts host keys for, and a second list would drift.
    assert "ed25519" not in written, written
    for keytype in bootloader.ZBM_HOST_KEY_TYPES:
        assert f'dropbear_{keytype}_key=' in written, keytype


def test_a_config_grub_mkconfig_never_put_in_place_stops_the_install() -> None:
    """`grub-mkconfig` writes `grub.cfg.new` and copies it over `grub.cfg`
    last, so the new file surviving means the configuration in place is the
    one it was meant to replace. A converted Debian machine kept its own
    single `menuentry` that way — the count passed — and booted to `Failed to
    boot both default and fallback entries` against a kernel the conversion
    had already replaced. Read off that disk: `grub.cfg` dated 2026-08-06 and
    `grub.cfg.new` dated 2026-08-18, 3231 bytes, beside a Gentoo kernel."""
    from gentoo_install.errors import NothingToBoot
    from gentoo_install.model.device import DeviceId

    operation = bootloader.InstallGrub(
        firmware=Firmware.BIOS,
        esp=None,
        boot_devices=(DeviceId("first"),),
    )

    class Disks(Recorder):
        def containing_disk(self, device: DeviceId) -> str:
            return f"/dev/{device}"

    # `replies["test"]` non-empty is how this recorder says a `test` exits
    # non-zero, which here is `test ! -e` finding the file still there.
    left_behind = Disks(replies={"grep": "1", "test": "still there"})
    with pytest.raises(NothingToBoot, match="grub.cfg.new"):
        operation.apply(left_behind)

    replaced = Disks(replies={"grep": "1"})
    operation.apply(replaced)


def zfsbootmenu_operation() -> bootloader.InstallZfsBootMenu:
    installation = load(Path("tests/fixtures/zbm-unlock.toml"))
    return next(
        one
        for one in bootloader.build(installation)
        if isinstance(one, bootloader.InstallZfsBootMenu)
    )


def test_a_probe_that_did_not_run_names_itself_rather_than_the_boot() -> None:
    """The count is read from a `check=False` command whose diagnostic arrives
    in the same string as the answer, so a missing `grep` read as a `grub.cfg`
    with no entry. That message names nothing an operator can act on."""
    from gentoo_install.errors import CommandFailed, NothingToBoot
    from gentoo_install.model.device import DeviceId

    grub = bootloader.InstallGrub(
        firmware=Firmware.UEFI, esp=PurePosixPath("/efi"), boot_devices=(DeviceId("first"),)
    )
    broken = Recorder(replies={"grep": CommandOutput("grep is not installed", 127)})
    with pytest.raises(CommandFailed, match="grub.cfg could not be counted"):
        grub.apply(broken)

    # grep exits 1 with nothing matched, which is a count of zero: that is the
    # file this operation exists to refuse, and it keeps its own message.
    empty = Recorder(replies={"grep": CommandOutput("0\n", 1)})
    with pytest.raises(NothingToBoot, match="no menu entry"):
        grub.apply(empty)

    built = zfsbootmenu_operation()
    unlisted = Recorder(replies={"find": CommandOutput("find: /efi/EFI/zbm: no such file", 1)})
    with pytest.raises(NothingToBoot, match="no EFI image under /efi/EFI/zbm"):
        built._image(unlisted)


def test_a_probe_answering_without_an_exit_status_is_read_as_a_failure() -> None:
    """`Context.run_in_target` is declared `-> str`, and a context that honours
    that carries no exit code: reading one only when it is there let every
    diagnostic through as an answer. Measured with a bare `str`, which is what
    `plan/convert.py` builds its staged machine out of."""
    from gentoo_install.errors import CommandFailed
    from gentoo_install.model.device import DeviceId

    class Wordy(Recorder):
        def run_in_target(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.in_target.append(tuple(argv))
            # str, not CommandOutput: the declared return type, and the shape
            # `answered` has to refuse rather than read as a count of entries.
            return cast(CommandOutput, "grep: command not found")

    grub = bootloader.InstallGrub(
        firmware=Firmware.UEFI, esp=PurePosixPath("/efi"), boot_devices=(DeviceId("first"),)
    )
    with pytest.raises(CommandFailed, match="grub.cfg could not be counted"):
        grub.apply(Wordy())


def test_an_image_found_beside_an_unreadable_entry_is_still_installed() -> None:
    """Measured with findutils 4.11.0: `find <dir> -name '*.EFI'` over a
    directory holding one entry with mode 000 prints the matches it reached on
    stdout and exits 1. The runner merges stderr in, so both arrive together."""
    built = zfsbootmenu_operation()

    partial = Recorder(
        replies={
            "find": CommandOutput(
                "/efi/EFI/zbm/vmlinuz-6.12.EFI\n"
                "find: '/efi/EFI/zbm/locked': Permission denied\n",
                1,
            )
        }
    )
    assert built._image(partial) == "/efi/EFI/zbm/vmlinuz-6.12.EFI"


def test_the_zfsbootmenu_describe_changes_with_the_command_line_it_will_set() -> None:
    """`apply()` sets `org.zfsbootmenu:commandline` and `describe()` said only
    the config path and the dataset, so a dry run could not show whether the
    installed system would answer on a serial port. GRUB's own describe has
    named its cmdline all along; the two disagreed about the same value.

    The parameters are also not the operator's alone: a remote unlock adds
    its own, so what a dry run hides here is larger than what was configured.
    """
    from dataclasses import replace

    operation = zfsbootmenu_operation()
    assert " ".join(operation.kernel_params) in operation.describe(), operation.describe()

    other = replace(operation, kernel_params=("console=ttyS1,9600", "nomodeset"))
    assert other.describe() != operation.describe()
    assert "console=ttyS1,9600 nomodeset" in other.describe(), other.describe()

    empty = replace(operation, kernel_params=())
    assert "empty" in empty.describe(), empty.describe()


@pytest.mark.parametrize(
    "fixture",
    ("tests/fixtures/vm-btrfs.toml", "tests/fixtures/zbm-unlock.toml"),
)
def test_serial_console_uses_kernel_default_or_rejects_garbage(fixture: str) -> None:
    from gentoo_install.errors import ConfigError

    installation = load(Path(fixture))
    no_speed = replace(
        installation,
        bootloader=replace(installation.bootloader, kernel_params=("console=ttyS0",)),
    )
    operations = bootloader.build(no_speed)
    serial = next(
        operation.serial
        for operation in operations
        if isinstance(
            operation, (bootloader.WriteGrubDefaults, bootloader.InstallZfsBootMenu)
        )
    )
    assert serial == ("ttyS0", 9600)

    malformed = replace(
        no_speed,
        bootloader=replace(no_speed.bootloader, kernel_params=("console=ttyS0,garbage",)),
    )
    with pytest.raises(ConfigError, match="decimal baud rate"):
        bootloader.build(malformed)


def test_systemd_boot_image_skips_nvram_and_firmware_failure_degrades() -> None:
    installation = load(Path("tests/fixtures/vm-sdboot.toml"))
    image = replace(
        installation,
        disk=replace(
            installation.disk,
            mode=DiskMode.IMAGE,
            image="/var/tmp/target.raw",
            size=Size.parse("20GiB"),
        ),
    )
    image_operations = bootloader.build(image)
    image_bootctl = next(
        operation
        for operation in image_operations
        if isinstance(operation, bootloader.InstallSystemdBoot)
    )
    assert not image_bootctl.write_nvram
    assert "without writing an efi boot entry" in image_bootctl.describe()

    image_recorder = Recorder()
    image_bootctl.apply(image_recorder)
    assert "--no-variables" in image_recorder.only("bootctl")

    bootctl = next(
        operation
        for operation in bootloader.build(installation)
        if isinstance(operation, bootloader.InstallSystemdBoot)
    )
    assert bootctl.write_nvram
    refused = Recorder(failures={"bootctl"})
    bootctl.apply(refused)
    assert refused.degraded(bootloader.NVRAM_ENTRY)


def test_grub_description_names_context_resolved_luks_parameters() -> None:
    installation = load(Path("tests/fixtures/vm-luks.toml"))
    without_configured_params = replace(
        installation,
        bootloader=replace(installation.bootloader, kernel_params=()),
    )
    operations = bootloader.build(without_configured_params)
    defaults = next(
        operation
        for operation in operations
        if isinstance(operation, bootloader.WriteGrubDefaults)
    )
    said = defaults.describe()
    assert "GRUB_CMDLINE_LINUX_DEFAULT empty" in said
    assert "rd.luks.uuid" in said
    assert "cryptroot" in said

    recorder = Recorder()
    defaults.apply(recorder)
    written = recorder.files[PurePosixPath("/etc/default/grub")]
    assert 'GRUB_CMDLINE_LINUX="rd.luks.uuid=uuid-of-cryptroot"' in written
    assert "uuid-of-cryptroot" not in said


def test_bios_grub_description_uses_disk_path_despite_mounted_esp() -> None:
    installation = load(Path("tests/fixtures/vm-btrfs.toml"))
    bios = replace(
        installation,
        bootloader=BootloaderConfig(kind=Bootloader.GRUB, firmware=Firmware.BIOS),
    )
    operations = bootloader.build(bios)
    grub = next(
        operation
        for operation in operations
        if isinstance(operation, bootloader.InstallGrub)
    )
    assert grub.esp == PurePosixPath("/efi")
    said = grub.describe()
    assert "/efi" not in said
    assert "bios" in said
    assert "disk" in said

    recorder = Recorder(replies={"grep": "1"})
    grub.apply(recorder)
    assert recorder.argv_starting(
        "grub-install", f"--target={DEFAULT_ARCHITECTURE.bios_target}"
    ) == (("grub-install", "--target=i386-pc", "/dev/vda"),)


def test_the_zbm_description_names_the_fallback_it_overwrites() -> None:
    """`apply` copies the generated image over the removable-media path.

    `EFI/BOOT/BOOTX64.EFI` is what firmware that dropped its NVRAM entry
    starts, and on a reused esp it belongs to whatever was installed there
    before. The description said the image was built under `EFI/zbm` and
    stopped, so a dry-run never showed the one write that replaces another
    system's loader.
    """
    operation = zfsbootmenu_operation()
    written = Recorder(replies={"find": "/efi/EFI/zbm/vmlinuz.EFI\n"})
    operation.apply(written)

    installed = [
        one
        for one in written.in_target
        if one[:1] == ("install",) and one[-1].endswith(bootloader.FALLBACK_IMAGE)
    ]
    assert installed, written.commands
    assert bootloader.FALLBACK_IMAGE in operation.describe(), operation.describe()
