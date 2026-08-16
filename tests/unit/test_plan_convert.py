# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Sequence, cast

import pytest

from gentoo_install.model.config import InstallConfig
from gentoo_install.plan import disk as plan_disk
from gentoo_install.plan import portage as plan_portage
from gentoo_install.plan.bootloader import InstallGrub
from gentoo_install.plan.build import build
from gentoo_install.plan.convert import SwapDirectories
from gentoo_install.plan.operations import Stage
from gentoo_install.model.config import DiskConfig, DiskMode
from gentoo_install.errors import ConversionFailed, ConversionUnsupported
from gentoo_install.model.device import (
    DeviceGraph,
    DeviceId,
    Existing,
    Filesystem,
    Mountpoint,
    StorageLayout,
)
from gentoo_install.plan import convert
from gentoo_install.plan.packages import Catalog, Group

from .layouts import config


CATALOG: Catalog = {"console": Group(name="console", packages=("app-editors/vim",))}


def _in_place() -> InstallConfig:
    """A conversion carries no device graph: the layout comes from the machine,
    and `validate()` refuses one beside the mode."""
    return replace(
        config(),
        disk=DiskConfig(graph=DeviceGraph.build([]), root=DeviceId(""), mode=DiskMode.IN_PLACE),
    )


def test_partition_mode_keeps_the_ordinary_list() -> None:
    ordinary = build(config(), CATALOG)
    explicit = build(replace(config(), disk=replace(config().disk)), CATALOG)
    assert tuple(type(operation) for operation in explicit) == tuple(type(operation) for operation in ordinary)


def test_conversion_operation_describes_and_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[tuple[Path, tuple[str, ...]]] = []

    def convert(staging: Any, names: tuple[str, ...]) -> None:
        called.append((staging, names))

    from gentoo_install.exec import convert as executor

    monkeypatch.setattr(executor, "convert", convert)
    operation = SwapDirectories()
    assert operation.describe()
    operation.apply(SimpleNamespace(target=PurePosixPath("/target")))
    assert called == [(Path("/gentoo-install.new"), operation.names)]


def _layout(
    *,
    root_device: str | None = "/dev/vda2",
    root_filesystem_type: str | None = "xfs",
    root_on_lvm: bool = False,
    root_on_luks: bool = False,
    root_on_mdraid: bool = False,
    esp_device: str | None = "/dev/vda1",
    esp_mountpoint: str | None = "/boot/efi",
    uefi: bool = True,
    root_free_bytes: int | None = 20 * 2**30,
    carried_fstab: tuple[str, ...] = (),
    boot_filesystem_type: str | None = "xfs",
    boot_same_filesystem: bool = True,
) -> StorageLayout:
    """A UEFI machine whose root is a plain filesystem on a partition."""
    return StorageLayout(
        root_device=root_device,
        root_filesystem_type=root_filesystem_type,
        root_uuid="8f1c0a2e-0000-4000-8000-000000000001",
        root_on_lvm=root_on_lvm,
        root_on_luks=root_on_luks,
        root_on_mdraid=root_on_mdraid,
        root_below_device="/dev/vda",
        boot_device="/dev/vda2",
        boot_filesystem_type=boot_filesystem_type,
        boot_same_filesystem=boot_same_filesystem,
        esp_device=esp_device,
        esp_mountpoint=esp_mountpoint,
        uefi=uefi,
        root_free_bytes=root_free_bytes,
        carried_fstab=carried_fstab,
    )


def test_the_running_layout_becomes_a_graph_that_formats_nothing() -> None:
    disk = convert.layout_graph(_layout())
    filesystems = [
        node for node in disk.graph.nodes.values() if isinstance(node, Filesystem)
    ]
    assert filesystems, "the graph has to describe the filesystems already there"
    assert all(not one.create for one in filesystems), filesystems
    assert disk.mode is DiskMode.IN_PLACE


def test_the_graph_names_the_devices_the_probe_read() -> None:
    disk = convert.layout_graph(_layout())
    selectors = {
        node.selector for node in disk.graph.nodes.values() if isinstance(node, Existing)
    }
    assert selectors == {"/dev/vda2", "/dev/vda1"}
    root = disk.graph[disk.root]
    assert isinstance(root, Mountpoint)
    assert root.path == PurePosixPath("/")


def test_the_esp_keeps_the_mountpoint_the_machine_already_uses() -> None:
    disk = convert.layout_graph(_layout(esp_mountpoint="/efi"))
    mounts = {
        node.path: node.options
        for node in disk.graph.nodes.values()
        if isinstance(node, Mountpoint)
    }
    assert mounts[PurePosixPath("/efi")] == ("umask=0077",)


def test_a_machine_without_uefi_gets_no_esp() -> None:
    disk = convert.layout_graph(_layout(uefi=False, esp_device=None, esp_mountpoint=None))
    assert not [
        node for node in disk.graph.nodes.values() if isinstance(node, Mountpoint)
        if node.path != PurePosixPath("/")
    ]


def test_uefi_without_an_esp_is_refused() -> None:
    with pytest.raises(ConversionUnsupported, match="no esp"):
        convert.layout_graph(_layout(esp_device=None))


def test_a_root_below_luks_is_refused_by_name() -> None:
    """One `Existing` node cannot describe a stack, and a conversion that
    guessed would rewrite a bootloader for a root it cannot unlock."""
    with pytest.raises(ConversionUnsupported, match="LUKS"):
        convert.layout_graph(_layout(root_on_luks=True))


def test_a_root_below_lvm_is_refused_by_name() -> None:
    with pytest.raises(ConversionUnsupported, match="LVM"):
        convert.layout_graph(_layout(root_on_lvm=True))


def test_a_root_below_mdraid_is_refused_by_name() -> None:
    with pytest.raises(ConversionUnsupported, match="mdraid"):
        convert.layout_graph(_layout(root_on_mdraid=True))


def test_a_filesystem_the_model_has_no_member_for_is_refused() -> None:
    with pytest.raises(ConversionUnsupported, match="reiserfs"):
        convert.layout_graph(_layout(root_filesystem_type="reiserfs"))


def test_a_root_device_that_could_not_be_read_is_refused() -> None:
    with pytest.raises(ConversionUnsupported, match="could not be read"):
        convert.layout_graph(_layout(root_device=None))


def test_the_conversion_stages_everything_then_swaps_then_writes_the_bootloader() -> None:
    """The stage order the rest of the installer sorts by puts the bootloader
    before packages. A conversion cannot: the bootloader writes to the root the
    machine will boot from, and that is the old one until the swap happens."""
    operations = build(_in_place(), CATALOG, layout=_layout())
    swapped = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, SwapDirectories)
    )
    assert all(isinstance(one, convert.Staged) for one in operations[:swapped])
    assert not any(isinstance(one, convert.Staged) for one in operations[swapped + 1 :])
    assert operations[swapped + 1 :], "the bootloader has to follow the swap"


def test_nothing_before_the_swap_writes_outside_the_staging_root() -> None:
    operations = build(_in_place(), CATALOG, layout=_layout())
    staged = [one for one in operations if isinstance(one, convert.Staged)]
    assert staged
    assert all(str(one.staging) == "/gentoo-install.new" for one in staged)
    assert all("/gentoo-install.new" in one.describe() for one in staged)


def test_a_conversion_without_a_probed_layout_is_refused() -> None:
    with pytest.raises(ConversionUnsupported, match="was not read"):
        build(_in_place(), CATALOG)


def test_the_conversion_formats_nothing_and_mounts_nothing() -> None:
    """The machine is already running on these filesystems."""
    operations = build(_in_place(), CATALOG, layout=_layout())
    stages = {
        (one.inner.stage if isinstance(one, convert.Staged) else one.stage)
        for one in operations
    }
    assert Stage.FORMAT not in stages
    assert Stage.PARTITION not in stages
    assert Stage.MOUNT not in stages


def test_the_kernel_reaches_boot_between_the_swap_and_the_bootloader() -> None:
    """`grub-mkconfig` reads `/boot`, and until this runs what is there belongs
    to the old distribution."""
    operations = build(_in_place(), CATALOG, layout=_layout())
    swapped = next(
        index for index, one in enumerate(operations) if isinstance(one, SwapDirectories)
    )
    populated = next(
        index for index, one in enumerate(operations)
        if isinstance(one, convert.PopulateBoot)
    )
    grub = next(
        index for index, one in enumerate(operations)
        if type(one).__name__ == "InstallGrub"
    )
    assert swapped < populated < grub


def test_boot_is_not_one_of_the_directories_that_are_renamed() -> None:
    """A rename refuses a mount point and a directory holding one, and `/boot`
    is one on many machines and holds the esp on many more."""
    assert "boot" not in convert.REPLACED_DIRECTORIES


class _RunningContext:
    """A context that records commands and answers `findmnt` from a list."""

    def __init__(self, mounted: tuple[str, ...] = ()) -> None:
        self.mounted = mounted
        self.ran: list[list[str]] = []

    target = PurePosixPath("/")

    answers: dict[str, str] = {}

    def run(self, argv: list[str], *, check: bool = True, input_text: str | None = None) -> str:
        self.ran.append(argv)
        if argv[0] == "findmnt":
            return "\n".join(self.mounted)
        return self.answers.get(argv[0], "")


def test_the_staging_root_is_unmounted_and_removed() -> None:
    context = _RunningContext(mounted=("/", "/boot"))
    convert.LeaveStaging().apply(cast(Any, context))
    assert ["umount", "--recursive", "--lazy", "/gentoo-install.new"] in context.ran
    assert ["rm", "--recursive", "--force", "/gentoo-install.new"] in context.ran


def test_a_staging_root_with_something_still_mounted_is_left_alone() -> None:
    """`rm` walking into a `/proc` still bound there is not a risk worth taking
    to tidy up a machine that is already converted."""
    context = _RunningContext(mounted=("/", "/gentoo-install.new/proc"))
    with pytest.raises(ConversionFailed, match="still has something mounted"):
        convert.LeaveStaging().apply(cast(Any, context))
    assert not any(argv[0] == "rm" for argv in context.ran)


def test_the_conversion_ends_by_leaving_no_staging_root() -> None:
    operations = build(_in_place(), CATALOG, layout=_layout())
    assert isinstance(operations[-1], convert.LeaveStaging)


def test_only_the_esp_and_boot_sector_writes_wait_for_the_swap() -> None:
    """Emerging the bootloader and writing `/etc/default/grub` are ordinary
    staged work, and leaving them in the irreversible window made it minutes
    long for no reason."""
    from gentoo_install.plan.bootloader import InstallGrub

    operations = build(_in_place(), CATALOG, layout=_layout())
    swapped = next(
        index for index, one in enumerate(operations) if isinstance(one, SwapDirectories)
    )
    before = operations[:swapped]
    after = operations[swapped:]
    assert any(
        isinstance(one, convert.Staged) and type(one.inner).__name__ == "Emerge"
        and "bootloader" in one.inner.describe()
        for one in before
    ), "the bootloader package is emerged before the swap"
    assert any(
        isinstance(one, convert.Staged) and type(one.inner).__name__ == "WriteGrubDefaults"
        for one in before
    )
    assert any(isinstance(one, InstallGrub) for one in after)
    assert not any(isinstance(one, InstallGrub) for one in before)


def test_a_root_with_no_room_for_both_systems_is_refused() -> None:
    """The staged system and the running one are on it at the same time, and
    running out half way leaves a staging root and nothing converted."""
    with pytest.raises(ConversionUnsupported, match="4 GiB free"):
        convert.layout_graph(_layout(root_free_bytes=4 * 2**30))


def test_a_root_with_room_is_accepted() -> None:
    convert.layout_graph(_layout(root_free_bytes=convert.CONVERSION_FREE_BYTES))


def test_an_unknown_amount_of_room_is_not_a_small_one() -> None:
    """`findmnt` not reporting `avail` is a reason to carry on: refusing there
    would stop machines that are fine."""
    convert.layout_graph(_layout(root_free_bytes=None))


def test_the_staging_root_is_created_before_anything_is_written() -> None:
    """The ordinary path gets `/mnt/gentoo` from the mount operations, and a
    conversion has none, so `tar --directory` was the first thing to find out."""
    operations = build(_in_place(), CATALOG, layout=_layout())
    first = operations[0]
    assert isinstance(first, convert.Staged)
    assert isinstance(first.inner, convert.PrepareStaging)


def test_a_staging_root_left_from_an_earlier_attempt_is_refused() -> None:
    context = _RunningContext()
    context.answers = {"find": "/gentoo-install.new/usr"}
    with pytest.raises(ConversionFailed, match="earlier attempt"):
        convert.PrepareStaging().apply(cast(Any, context))


def test_an_empty_staging_root_is_accepted() -> None:
    context = _RunningContext()
    convert.PrepareStaging().apply(cast(Any, context))
    assert ["mkdir", "--parents", "/gentoo-install.new"] in context.ran


def test_a_staged_operation_keeps_the_requirements_of_the_one_it_wraps() -> None:
    """`--missing-commands` and the preflight check read this, and a wrapper
    that answered for itself would have said a conversion needs nothing."""
    from gentoo_install.exec import preflight

    operations = build(_in_place(), CATALOG, layout=_layout())
    wanted = preflight.required_commands(_in_place(), operations)
    assert "tar" in wanted
    assert "mkdir" in wanted


def test_a_staged_operation_reports_whether_it_releases_the_machine() -> None:
    staged = [
        one for one in build(_in_place(), CATALOG, layout=_layout())
        if isinstance(one, convert.Staged)
    ]
    assert staged
    assert all(
        one.releases_the_machine == one.inner.releases_the_machine for one in staged
    )


def test_a_separate_boot_filesystem_reaches_the_new_fstab() -> None:
    """The kernel this conversion puts in `/boot` is on that filesystem, and a
    machine that does not mount it comes up with an empty `/boot`."""
    disk = convert.layout_graph(
        _layout(boot_same_filesystem=False, boot_filesystem_type="ext4")
    )
    mounts = {
        node.path
        for node in disk.graph.nodes.values()
        if isinstance(node, Mountpoint)
    }
    assert PurePosixPath("/boot") in mounts


def test_a_boot_on_the_root_filesystem_needs_no_entry_of_its_own() -> None:
    disk = convert.layout_graph(_layout(boot_same_filesystem=True))
    mounts = {
        node.path
        for node in disk.graph.nodes.values()
        if isinstance(node, Mountpoint)
    }
    assert PurePosixPath("/boot") not in mounts


def test_a_separate_boot_that_could_not_be_read_is_refused() -> None:
    with pytest.raises(ConversionUnsupported, match="separate filesystem"):
        convert.layout_graph(
            _layout(boot_same_filesystem=False, boot_filesystem_type=None)
        )


def test_the_staging_context_answers_every_member_of_the_context() -> None:
    """Naming the members by hand is what drifted: a conversion stopped at
    `make.conf` with `'_StagingContext' object has no attribute 'read'` after
    it had already downloaded and unpacked a stage3."""
    from gentoo_install.plan.operations import Context

    wanted = [name for name in dir(Context) if not name.startswith("_")]
    parent = SimpleNamespace(**{name: name for name in wanted})
    staged = convert._StagingContext(
        parent=cast(Any, parent), staging=PurePosixPath("/gentoo-install.new")
    )

    assert staged.target == PurePosixPath("/gentoo-install.new")
    missing = [name for name in wanted if not hasattr(staged, name)]
    assert missing == [], missing
    # Everything but `target` comes from the parent unchanged.
    for name in wanted:
        if name == "target":
            continue
        assert getattr(staged, name) == name, name


def test_staging_moves_the_chroot_not_only_the_target() -> None:
    """`run_in_target` chroots into the machine's own mount point rather than
    `context.target`, so a context that answered only `target` would have run
    every `emerge` in the system being replaced."""
    from dataclasses import dataclass as plain
    from pathlib import Path as RealPath

    @plain
    class Machinelike:
        mountpoint: RealPath = RealPath("/mnt/gentoo")

        @property
        def target(self) -> PurePosixPath:
            return PurePosixPath(self.mountpoint)

        def run_in_target(self, argv: list[str], *, check: bool = True) -> str:
            return f"chroot {self.mountpoint}"

    staged = convert._aimed_at(cast(Any, Machinelike()), PurePosixPath("/gentoo-install.new"))

    assert staged.target == PurePosixPath("/gentoo-install.new")
    assert staged.run_in_target(["emerge"]) == "chroot /gentoo-install.new"


def test_the_live_machine_still_keeps_its_target_in_one_field() -> None:
    """The move works by replacing that one field. A machine that grew a
    second copy of the path would keep half of itself aimed at the old root."""
    from dataclasses import fields

    from gentoo_install.exec.apply import Machine

    assert "mountpoint" in {one.name for one in fields(Machine)}


def test_a_context_without_that_field_still_reaches_the_staging_root() -> None:
    class Recorder:
        target = PurePosixPath("/mnt/gentoo")

        def read(self, path: PurePosixPath) -> str:
            return "from the parent"

    staged = convert._aimed_at(cast(Any, Recorder()), PurePosixPath("/gentoo-install.new"))
    assert staged.target == PurePosixPath("/gentoo-install.new")
    assert staged.read(PurePosixPath("/etc/portage/make.conf")) == "from the parent"


def test_the_machine_is_given_the_derived_graph() -> None:
    """Whatever resolves a `DeviceId` at apply time has to see the graph read
    from the machine: `write /etc/fstab` stopped twenty-four operations into a
    real conversion with `no node with id 'running-root-device'`."""
    from gentoo_install.plan.build import running_config

    running = running_config(_in_place(), _layout())
    assert running.disk.graph[convert.ROOT_MOUNT] is not None
    assert running.disk.root == convert.ROOT_MOUNT


def test_an_ordinary_configuration_is_handed_back_unchanged() -> None:
    from gentoo_install.plan.build import running_config

    from .layouts import config

    ordinary = config()
    assert running_config(ordinary, None) is ordinary


def test_a_conversion_without_a_layout_is_refused_there_too() -> None:
    from gentoo_install.plan.build import running_config

    with pytest.raises(ConversionUnsupported, match="was not read"):
        running_config(_in_place(), None)


def test_the_mounts_the_conversion_does_not_manage_are_carried() -> None:
    """A conversion replaces `/etc`, so a data partition or a swap the operator
    had would be gone from the new fstab: the machine boots and is wrong in a
    way nothing here can see."""
    carried = ("UUID=abc\t/srv\text4\tdefaults\t0\t2", "UUID=def\tnone\tswap\tsw\t0\t0")
    operations = build(_in_place(), CATALOG, layout=_layout(carried_fstab=carried))
    appended = [
        one.inner
        for one in operations
        if isinstance(one, convert.Staged)
        and isinstance(one.inner, convert.CarryFstabEntries)
    ]
    assert appended and appended[0].lines == carried

    written = [
        index
        for index, one in enumerate(operations)
        if isinstance(one, convert.Staged) and "fstab" in one.inner.describe()
    ]
    assert len(written) == 2, "the file is written, then the rest is appended"
    assert written[0] < written[1]


def test_a_machine_with_nothing_else_mounted_carries_nothing() -> None:
    operations = build(_in_place(), CATALOG, layout=_layout())
    appended = [
        one.inner
        for one in operations
        if isinstance(one, convert.Staged)
        and isinstance(one.inner, convert.CarryFstabEntries)
    ]
    assert appended and appended[0].lines == ()


def test_carrying_appends_rather_than_replaces() -> None:
    context = _RunningContext()
    context.answers = {}
    written: dict[str, str] = {}

    class Writing(_RunningContext):
        target = PurePosixPath("/gentoo-install.new")

        def read(self, path: object) -> str:
            return "# device\tmountpoint\ttype\toptions\tdump\tpass\nUUID=1\t/\text4\tdefaults\t0\t1\n"

        def write(self, path: object, content: str, *, mode: int = 0o644) -> None:
            written[str(path)] = content

    convert.CarryFstabEntries(lines=("UUID=abc\t/srv\text4\tdefaults\t0\t2",)).apply(
        cast(Any, Writing())
    )
    # Target-absolute: `Staged` roots it, and this test asserting the joined
    # form is what let `/gentoo-install.new/gentoo-install.new/etc/fstab`
    # through to a real machine.
    assert set(written) == {"/etc/fstab"}, written
    said = written["/etc/fstab"]
    assert "UUID=1\t/\text4" in said, "the generated entries survive"
    assert "/srv" in said
    assert said.index("UUID=1") < said.index("/srv")


def test_a_find_that_failed_is_not_read_as_an_empty_staging_root() -> None:
    """The runner merges stderr into stdout, so a `find` that fails without
    printing reads as an empty directory and the stage3 is unpacked over an
    earlier attempt; one that prints reads as a directory with an entry and the
    operator is told to remove a directory that is empty."""
    from gentoo_install.errors import ConversionFailed
    from gentoo_install.plan.operations import CommandOutput

    from .recorder import Recorder

    class FindRefuses(Recorder):
        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.commands.append(tuple(argv))
            if argv[0] == "find":
                return CommandOutput("find: '/gentoo-install.new': Permission denied\n", 1)
            return CommandOutput("", 0)

    with pytest.raises(ConversionFailed, match="could not be read"):
        convert.PrepareStaging().apply(FindRefuses())

    class FindSaysNothing(FindRefuses):
        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.commands.append(tuple(argv))
            return CommandOutput("", 1 if argv[0] == "find" else 0)

    with pytest.raises(ConversionFailed, match="could not be read"):
        convert.PrepareStaging().apply(FindSaysNothing())

    # Negative control: an empty staging root is the ordinary case and the
    # operation must go through it without complaint.
    class Empty(Recorder):
        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.commands.append(tuple(argv))
            return CommandOutput("", 0)

    convert.PrepareStaging().apply(Empty())

    # Negative control: a staging root with something in it still reports that,
    # and not the unreadable message.
    class Occupied(Recorder):
        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.commands.append(tuple(argv))
            return CommandOutput("/gentoo-install.new/etc\n" if argv[0] == "find" else "", 0)

    with pytest.raises(ConversionFailed, match="left from an earlier attempt"):
        convert.PrepareStaging().apply(Occupied())


def test_a_findmnt_that_failed_does_not_authorise_the_staging_root_to_be_removed() -> None:
    """`rm --recursive` walking into a `/proc` still bound under the staging
    root is what the refusal above it exists to prevent, and an unreadable
    `findmnt` read as "nothing is mounted" walks straight into it."""
    from gentoo_install.errors import ConversionFailed
    from gentoo_install.plan.operations import CommandOutput

    from .recorder import Recorder

    class MountsUnreadable(Recorder):
        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.commands.append(tuple(argv))
            if argv[0] == "findmnt":
                return CommandOutput("findmnt: cannot open /proc/self/mountinfo\n", 1)
            return CommandOutput("", 0)

    recorder = MountsUnreadable()
    with pytest.raises(ConversionFailed, match="could not be read"):
        convert.LeaveStaging().apply(recorder)
    assert not [one for one in recorder.commands if one[0] == "rm"], recorder.commands

    # Negative control: a readable mount table with nothing under the staging
    # root does authorise the removal.
    class NothingUnderIt(Recorder):
        def run(
            self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
        ) -> CommandOutput:
            self.commands.append(tuple(argv))
            return CommandOutput("/\n/boot\n" if argv[0] == "findmnt" else "", 0)

    clean = NothingUnderIt()
    convert.LeaveStaging().apply(clean)
    assert [one for one in clean.commands if one[0] == "rm"], clean.commands


def test_a_bios_conversion_refuses_a_root_that_is_a_whole_disk() -> None:
    """Measured on an Alpine 3.21 cloud image: its ext4 sits on `/dev/vda`
    itself, with a dos label carrying no partitions and SYSLINUX in the boot
    sector. `grub-install --target=i386-pc /dev/vda` there answers

        warning: Embedding is not possible.
        error: will not proceed with blocklists.

    The plan rendered all 49 operations against that machine, so without this
    the conversion emerges a whole system, swaps `/usr`, `/etc` and `/bin`, and
    only then finds it cannot write a bootloader — with the old system gone.
    """
    whole_disk = replace(
        _layout(uefi=False, esp_device=None, esp_mountpoint=None),
        root_device="/dev/vda",
        root_below_device=None,
    )
    with pytest.raises(ConversionUnsupported, match="nowhere to embed"):
        convert.layout_graph(whole_disk)

    # Negative control one: the same machine with root on a partition is what
    # every ordinary bios install looks like, and it has a post-MBR gap.
    partitioned = replace(whole_disk, root_device="/dev/vda2", root_below_device="/dev/vda")
    assert convert.layout_graph(partitioned).mode is DiskMode.IN_PLACE

    # Negative control two: on UEFI nothing is embedded in a boot sector, so a
    # whole-disk root is not this failure and must still be accepted.
    uefi = replace(whole_disk, uefi=True, esp_device="/dev/vda1", esp_mountpoint="/boot/efi")
    assert convert.layout_graph(uefi).mode is DiskMode.IN_PLACE


def test_a_conversion_carries_the_btrfs_subvolume_onto_the_command_line() -> None:
    """Fedora 41 mounts `/dev/vda4[/root]` and Ubuntu `[/@]`. Without a
    `Subvolume` in the graph, `_rootflags` answers nothing and the new command
    line carries no `rootflags=subvol=`, so the initramfs mounts the default
    subvolume instead of the system that was written.
    """
    from gentoo_install.model.device import Subvolume
    from gentoo_install.plan.automatic import _rootflags

    on_a_subvolume = replace(_layout(root_filesystem_type="btrfs"), root_subvolume="/root")
    disk = convert.layout_graph(on_a_subvolume)
    carried = [one for one in disk.graph.nodes.values() if isinstance(one, Subvolume)]
    assert [one.name for one in carried] == ["/root"]
    assert _rootflags(replace(_in_place(), disk=disk)) == "/root"

    # Negative control: btrfs at the top level is what Arch's cloud image runs,
    # and inventing a subvolume for it would put a `rootflags=` on a machine
    # that must not have one.
    top_level = replace(on_a_subvolume, root_subvolume=None)
    plain = convert.layout_graph(top_level)
    assert not [one for one in plain.graph.nodes.values() if isinstance(one, Subvolume)]
    assert _rootflags(replace(_in_place(), disk=plain)) == ""


def test_the_carried_fstab_opens_inside_the_staging_root(tmp_path: Path) -> None:
    """Arch's cloud image carries `/swap/swapfile none swap defaults 0 0`, and
    the conversion stopped at operation 34 of 46 with

        TargetEscape: /gentoo-install.new/etc/fstab:
        gentoo-install.new is not a directory in the target

    Every fixture and the Debian image carried nothing, so `apply` returned at
    its first line and this path had never run. The check here is the real
    `open_in_target`, against a staging root laid out like the machine's.
    """
    import os

    from gentoo_install.exec.runner import open_in_target

    staging = tmp_path / "gentoo-install.new"
    (staging / "etc").mkdir(parents=True)
    (staging / "etc" / "fstab").write_text("UUID=1\t/\tbtrfs\tdefaults\t0\t1\n")

    opened: list[PurePosixPath] = []

    class Rooted(_RunningContext):
        target = PurePosixPath("/gentoo-install.new")

        def read(self, path: object) -> str:
            handle = open_in_target(staging, cast(PurePosixPath, path), os.O_RDONLY)
            try:
                return os.read(handle, 4096).decode()
            finally:
                os.close(handle)

        def write(self, path: object, content: str, *, mode: int = 0o644) -> None:
            opened.append(cast(PurePosixPath, path))
            handle = open_in_target(
                staging, cast(PurePosixPath, path), os.O_WRONLY | os.O_TRUNC
            )
            try:
                os.write(handle, content.encode())
            finally:
                os.close(handle)

    convert.CarryFstabEntries(
        lines=("/swap/swapfile none swap defaults 0 0",)
    ).apply(cast(Any, Rooted()))

    assert opened == [PurePosixPath("/etc/fstab")]
    kept = (staging / "etc" / "fstab").read_text()
    assert "UUID=1" in kept and "/swap/swapfile" in kept


def test_a_failed_conversion_still_unmounts_the_staging_root() -> None:
    """`_release` runs only the closing operations that say they release the
    machine, and `LeaveStaging` did not. A conversion that stopped therefore
    left `/proc`, `/sys` and `/dev` bound under `/gentoo-install.new`, and
    `PrepareStaging` refuses the next attempt because that directory is not
    empty. Removing it by hand walks those binds into the running `/dev`, which
    is what happened after the Arch run stopped at operation 34 of 46.
    """
    operations = build(_in_place(), CATALOG, layout=_layout())
    closing = [one for one in operations if one.stage is Stage.FINISH]
    leaving = [one for one in closing if isinstance(one, convert.LeaveStaging)]
    assert leaving, closing
    assert leaving[0].releases_the_machine

    # Negative control: the rest of the closing stage still does not run after
    # a failure, or an empty chroot's error replaces the one that matters.
    others = [one for one in closing if not isinstance(one, convert.LeaveStaging)]
    assert others, "the closing stage has more than the unmount"
    assert not any(one.releases_the_machine for one in others), others


def test_a_conversion_refuses_a_replaced_directory_that_is_its_own_mount() -> None:
    """A Fedora 41 cloud image mounts `/var` and `/home` as their own btrfs
    subvolumes. `exec/convert.py` refuses that at the rename, which arrived at
    operation 44 of 46 with the whole system already emerged and the staging
    root left behind; the mount table says it before anything is written.
    """
    fedora = replace(_layout(), separate_mounts=("/boot", "/home", "/proc", "/var"))
    with pytest.raises(ConversionUnsupported, match=r"/var is a separate mount"):
        convert.layout_graph(fedora)

    # Negative control one: a mount that is not replaced is not a reason to
    # refuse, or every machine with a separate /home would be turned away.
    ordinary = replace(fedora, separate_mounts=("/boot", "/home", "/proc"))
    assert convert.layout_graph(ordinary).mode is DiskMode.IN_PLACE

    # Negative control two: the message names every one of them, because an
    # operator who moves /var only to be refused for /usr has learned nothing.
    both = replace(fedora, separate_mounts=("/usr", "/var"))
    with pytest.raises(ConversionUnsupported, match=r"/usr, /var are a separate mount"):
        convert.layout_graph(both)
