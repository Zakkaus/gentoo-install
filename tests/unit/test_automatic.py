"""What the panel says the installer adds, against what it actually writes.

`plan/automatic.py` exists so an operator can see the parameters and USE flags
they did not type. That is only worth showing if it agrees with the operations,
so these tests read both and compare, rather than asserting a list by hand.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.model import atoms
from gentoo_install.model.config import (
    Bootloader,
    BootloaderConfig,
    InstallConfig,
    KernelConfig,
    RemoteUnlock,
    SystemConfig,
)
from gentoo_install.plan import automatic, bootloader as plan_bootloader, kernel as plan_kernel
from gentoo_install.tui import screens

from .layouts import config, encrypted_root, ext4_on_gpt, zfs_root
from .recorder import Recorder


def command_line(installation: InstallConfig) -> str:
    """The one line this bootloader's entries are built from."""
    recorder = Recorder()
    for operation in (*plan_bootloader.build(installation), *plan_kernel.build(installation)):
        operation.apply(recorder)
    for path, content in recorder.files.items():
        if path.name == "cmdline":
            return content.strip()
    grub = recorder.files.get(PurePosixPath("/etc/default/grub"), "")
    # Both variables: `GRUB_CMDLINE_LINUX` reaches every entry and `_DEFAULT`
    # only the default one, and an entry boots with the two concatenated.
    both = [
        line.partition("=")[2].strip('"')
        for line in grub.splitlines()
        if line.startswith(("GRUB_CMDLINE_LINUX=", "GRUB_CMDLINE_LINUX_DEFAULT="))
    ]
    if both:
        return " ".join(both)
    raise AssertionError("no command line was written")


@pytest.mark.parametrize(
    "kind", [Bootloader.GRUB, Bootloader.SYSTEMD_BOOT], ids=["grub", "systemd-boot"]
)
def test_the_panel_and_the_boot_entry_hold_the_same_parameters(kind: Bootloader) -> None:
    """Exact equality, both directions. A parameter written but not shown is a
    surprise in the installed system; one shown but not written is a promise
    the panel cannot keep.

    `written_here` is what carries the difference between the two bootloaders:
    `10_linux` composes `root=` and `ro` from the filesystem it probes, so GRUB
    takes neither from `/etc/default/grub`.
    """
    installation = replace(
        config(encrypted_root()),
        bootloader=BootloaderConfig(kind=kind),
        system=replace(SystemConfig(), keymap="de", keymap_initramfs="de"),
    )
    ours = {
        one.value.split("=")[0]
        for one in automatic.kernel_parameters(installation)
        if one.written_here
    }
    written = {word.split("=")[0] for word in command_line(installation).split()}
    assert ours == written


def test_grub_is_not_handed_a_root_it_writes_for_itself() -> None:
    """Two `root=` on one line is the second one winning by accident. The panel
    still shows it, because the entry carries it either way."""
    installation = replace(config(encrypted_root()), bootloader=BootloaderConfig(kind=Bootloader.GRUB))
    assert "root=" not in command_line(installation)
    shown = automatic.kernel_parameters(installation)
    assert [one for one in shown if one.value.startswith("root=") and not one.written_here]


def test_a_dataset_root_is_named_exactly_because_it_can_be() -> None:
    """`root=ZFS=` carries a name the configuration already knows, unlike a
    UUID, so the row shows the real value rather than an ellipsis."""
    installation = replace(
        config(zfs_root()), bootloader=BootloaderConfig(kind=Bootloader.ZFSBOOTMENU)
    )
    shown = [one.value for one in automatic.kernel_parameters(installation)]
    assert any(one.startswith("root=ZFS=") and "…" not in one for one in shown), shown


def test_zfsbootmenu_is_not_shown_a_keymap_it_never_carries() -> None:
    """ZFSBootMenu prompts for the passphrase itself, so its entry has no
    `rd.vconsole.keymap`. Showing one would send the operator looking for a
    parameter that is not there."""
    installation = replace(
        config(zfs_root()),
        bootloader=BootloaderConfig(kind=Bootloader.ZFSBOOTMENU),
        system=replace(SystemConfig(), keymap="de", keymap_initramfs="de"),
    )
    shown = [one.value for one in automatic.kernel_parameters(installation)]
    assert not [one for one in shown if one.startswith("rd.vconsole.keymap")], shown


def test_remote_unlock_parameters_are_attributed_to_remote_unlock() -> None:
    installation = replace(
        config(encrypted_root()),
        kernel=replace(
            KernelConfig(),
            remote_unlock=RemoteUnlock(enabled=True, interface="eth0", address="192.0.2.10/24"),
        ),
    )
    added = automatic.kernel_parameters(installation)
    reasons = {one.because for one in added if one.value.startswith(("rd.neednet", "ip="))}
    assert reasons == {automatic.UNLOCK}, added


def test_a_flag_the_operator_typed_is_not_reported_as_automatic() -> None:
    """The row counts what was added on top of what was typed. Counting a
    typed flag twice reads as the installer overriding the answer."""
    catalog = load_catalog()
    installation = replace(config(ext4_on_gpt()), packages=replace(config().packages, desktop="plasma"))
    every = {one.value for one in automatic.use_flags(installation, catalog)}
    assert every, "plasma asks for at least one flag"
    one_of_them = sorted(every)[0]
    typed = replace(
        installation, portage=replace(installation.portage, use=(one_of_them,))
    )
    after = {one.value for one in automatic.use_flags(typed, catalog)}
    assert one_of_them not in after


def test_the_automatic_use_flags_are_the_ones_make_conf_gets() -> None:
    """`required_use` is what reaches `make.conf`. A flag shown here and not
    there, or there and not here, is the panel and the plan disagreeing."""
    from gentoo_install.plan.packages import required_use

    catalog = load_catalog()
    installation = replace(
        config(ext4_on_gpt()),
        packages=replace(config().packages, desktop="plasma", graphics=("nvidia",)),
    )
    shown = {one.value for one in automatic.use_flags(installation, catalog)}
    assert shown == set(required_use(installation, catalog))


def test_every_reason_used_is_in_the_table_the_catalog_reads() -> None:
    """`REASONS` is what `test_i18n` translates. A reason built somewhere else
    would draw as English in the middle of a translated screen."""
    catalog = load_catalog()
    installation = replace(
        config(encrypted_root()),
        packages=replace(config().packages, desktop="plasma", graphics=("nvidia",)),
        system=replace(SystemConfig(), keymap="de", keymap_initramfs="de"),
        kernel=replace(
            KernelConfig(), remote_unlock=RemoteUnlock(enabled=True, interface="eth0")
        ),
    )
    used = {
        one.because
        for one in (
            *automatic.kernel_parameters(installation),
            *automatic.use_flags(installation, catalog),
            *automatic.video_cards(installation, catalog),
        )
    }
    assert used <= set(automatic.REASONS), sorted(used - set(automatic.REASONS))
    assert used, "the configuration was meant to exercise several reasons"


@pytest.mark.parametrize(
    "parameter",
    ['quiet"', "a$(b)", "back\\slash", "it's", "`hostname`"],
    ids=["quote", "substitution", "backslash", "apostrophe", "backtick"],
)
def test_a_parameter_that_would_escape_the_grub_file_is_refused(parameter: str) -> None:
    """`/etc/default/grub` is a shell script sourced by `grub-mkconfig`, and
    the parameters go inside a double-quoted assignment. Any of these ends the
    value early or reaches the shell."""
    good, bad = atoms.split_kernel_parameters(parameter)
    assert good == () and bad == (parameter,)


@pytest.mark.parametrize(
    "parameter", ["quiet", "loglevel=3", "video=1920x1080", "i915.enable_psr=0", "-flag"]
)
def test_an_ordinary_parameter_is_accepted(parameter: str) -> None:
    good, bad = atoms.split_kernel_parameters(parameter)
    assert good == (parameter,) and bad == ()


@pytest.mark.parametrize(
    "flag", ["wayland", "-X", "dist-kernel", "-*", "python_targets_python3_13", "l10n@zh"]
)
def test_a_use_flag_portage_accepts_is_accepted(flag: str) -> None:
    good, bad = atoms.split_use_flags(flag)
    assert good == (flag,) and bad == ()


@pytest.mark.parametrize("flag", ["-", "+wayland", "*", "x11-libs/gtk+", "a b"])
def test_a_word_that_is_not_a_use_flag_is_refused(flag: str) -> None:
    """`+` in front is not accepted: Portage takes it in `IUSE`, not in `USE`,
    and a flag beginning with one silently does nothing."""
    good, _ = atoms.split_use_flags(flag)
    assert flag not in good


def test_confirming_a_driver_pins_what_it_adds_into_the_configuration() -> None:
    """Once pinned, the value is the operator's own: it survives a later
    change to the desktop, and `use_flags` stops reporting a flag that is
    already in `portage.use`, so the panel never lists it twice."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context, down
    from gentoo_install.tui import screens

    at = context()
    before = config(ext4_on_gpt())
    after = replace(before, packages=replace(before.packages, graphics=("nvidia",)))
    answer = screens.settle(FakeScreen(keys=[*down(1), "\n"], lines=30), at, before, after)
    pinned = answer.unwrap()
    assert pinned.portage.video_cards == ("nvidia",)
    assert automatic.video_cards(pinned, at.groups) == ()


def test_declining_the_side_effects_cancels_the_choice() -> None:
    """The flags are not extras that come with the driver; they are what makes
    it work. Keeping the choice and dropping them would install nvidia-drivers
    with no `nvidia` in VIDEO_CARDS."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    at = context()
    before = config(ext4_on_gpt())
    after = replace(before, packages=replace(before.packages, graphics=("nvidia",)))
    answer = screens.settle(FakeScreen(keys=["\n"], lines=30), at, before, after)
    assert answer.unwrap() == before


def test_a_choice_that_changes_nothing_asks_nothing() -> None:
    """`nouveau` names a VIDEO_CARDS value and installs nothing, but a screen
    that asked anyway would train the operator to press yes."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    at = context()
    before = config(ext4_on_gpt())
    # No keys at all: FakeScreen raises if the widget asks for one.
    answer = screens.settle(FakeScreen(keys=[], lines=30), at, before, before)
    assert answer.unwrap() == before


def test_a_driver_row_says_what_it_will_add_before_it_is_chosen() -> None:
    """Three places name the same values: the row, the confirmation, and the
    USE row afterwards. This is the earliest, and the only one an operator
    comparing two drivers reads without picking one."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    drawn = FakeScreen(keys=["q"], lines=30, columns=110)
    screens.graphics_screen(drawn, config(ext4_on_gpt()), context())
    assert "nvidia  proprietary" in drawn.last
    assert "(+nvidia)" in drawn.last
    assert "(+amdgpu radeonsi)" in drawn.last
    # No `none` row: nothing ticked is what leaves the kernel to pick, so a
    # row saying so would be a second way to say the same thing.
    assert "none" not in drawn.last


def test_a_desktop_row_says_it_brings_wayland() -> None:
    """`wayland` is a global flag every package reads, including
    nvidia-drivers, whose IUSE carries it. An operator choosing Plasma is
    choosing that too."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    drawn = FakeScreen(keys=["q"], lines=30, columns=120)
    screens.desktop_screen(drawn, config(ext4_on_gpt()), context())
    assert "plasma  the session only (+wayland qt6 networkmanager)" in drawn.last
    assert "gnome  the session only (+wayland gnome networkmanager gtk)" in drawn.last


def test_a_hybrid_machine_can_name_more_than_one_card() -> None:
    """An AMD laptop with an NVIDIA card carries `amdgpu radeonsi nvidia`, and
    the driver row offers one group. What is typed here is added to what the
    group contributes, not replaced by it."""
    from gentoo_install.plan.packages import required_video_cards

    catalog = load_catalog()
    installation = replace(
        config(ext4_on_gpt()),
        packages=replace(config().packages, graphics=("amdgpu",)),
        portage=replace(config().portage, video_cards=("nvidia",)),
    )
    assert required_video_cards(installation, catalog) == ("nvidia", "amdgpu", "radeonsi")


def test_input_devices_is_never_left_empty_by_default() -> None:
    """make.conf replaces the profile's INPUT_DEVICES rather than adding to it,
    so a machine installed with the row untouched would have no pointer."""
    from gentoo_install.plan.portage import make_conf

    written = dict(make_conf(config(ext4_on_gpt()), (), (), ()))
    assert written["INPUT_DEVICES"] == "libinput"


def test_the_confirmation_can_open_the_row_the_values_landed_on() -> None:
    """The USE row is elsewhere in the menu. An operator who wants to look at
    what was just added would otherwise leave, find it, and try to remember
    what they were checking."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context, down

    at = context()
    before = config(ext4_on_gpt())
    after = replace(before, packages=replace(before.packages, desktop="plasma"))
    # Down twice to "Yes, and open", enter; then q out of the USE row it opens.
    answer = screens.settle(
        FakeScreen(keys=[*down(2), "\n", "q"], lines=30, columns=110), at, before, after
    )
    pinned = answer.unwrap()
    assert "wayland" in pinned.portage.use
    # Cancelling the row it opened keeps the pinned values rather than undoing
    # the choice that produced them.
    assert pinned.packages.desktop == "plasma"


def test_every_screen_that_picks_a_group_carrying_use_confirms_it() -> None:
    """Four screens choose groups and three of them carried USE. `sddm`,
    `pipewire` and `bluetooth` were each added silently, so the operator met
    them in `make.conf` afterwards.

    Read from the catalog rather than listed here: a group given a USE flag
    later has to fail this rather than slip through.
    """
    import inspect

    catalog = load_catalog()
    carries = {name for name, group in catalog.items() if group.use or group.video_cards}
    assert carries, "the catalog is meant to have groups that set make.conf"
    picks = {
        "desktop_screen": set(catalog),
        "graphics_screen": {name for name, _ in screens.GRAPHICS if name},
        "display_manager_screen": {name for name, _ in screens.DISPLAY_MANAGERS if name},
        "packages_screen": set(catalog),
    }
    for name, chooses in picks.items():
        if not (chooses & carries):
            continue
        source = inspect.getsource(getattr(screens, name))
        assert "settle(" in source, f"{name} picks a group that sets make.conf and never asks"


#: The Gentoo tree, when this machine has one. The group files are edited here,
#: so the check runs where the mistake is made.
_USE_DESC = Path("/var/db/repos/gentoo/profiles/use.desc")
_USE_LOCAL_DESC = Path("/var/db/repos/gentoo/profiles/use.local.desc")


@pytest.mark.skipif(not _USE_DESC.is_file(), reason="no Gentoo tree on this machine")
def test_a_group_declares_only_flags_more_than_one_package_can_use() -> None:
    """`use` reaches `make.conf`, which applies to every package. A flag that
    exactly one package declares belongs in `package_use` beside it.

    Not "absent from use.desc": `pipewire` is local too, and 31 packages
    declare it, which is why the desktop profile sets it globally. `kcm` is
    declared by one, `app-i18n/fcitx-configtool`. It sat in the plasma group,
    where it matched nothing, and was missing whenever fcitx5 was chosen with
    GNOME or Xfce.
    """
    global_flags = {
        line.split(" - ")[0]
        for line in _USE_DESC.read_text().splitlines()
        if line and not line.startswith("#")
    }
    owners: dict[str, int] = {}
    for line in _USE_LOCAL_DESC.read_text().splitlines():
        if not line or line.startswith("#") or ":" not in line.split(" - ")[0]:
            continue
        owners[line.split(" - ")[0].split(":")[1]] = (
            owners.get(line.split(" - ")[0].split(":")[1], 0) + 1
        )
    for name, group in load_catalog().items():
        wrong = [
            flag
            for flag in group.use
            if flag.lstrip("-") not in global_flags and owners.get(flag.lstrip("-"), 0) == 1
        ]
        assert not wrong, f"{name} puts {wrong} in USE; one package declares them"
