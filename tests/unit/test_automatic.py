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
from gentoo_install.tui.widgets import Answer, Outcome

from .layouts import config, encrypted_root, ext4_on_gpt, zfs_root
from .recorder import Recorder


def command_line(installation: InstallConfig) -> str:
    """The one line this bootloader's entries are built from."""
    recorder = Recorder(replies={"find": "/boot/kernel-6.18.41-gentoo-dist-bin\n"})
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


def test_the_two_nvidia_drivers_cannot_both_be_ticked() -> None:
    """nvidia-drivers writes `blacklist nouveau` into its own modprobe.d file,
    so a machine with both ticked installs one driver that cannot load."""
    from gentoo_install.errors import ValidationFailed
    from gentoo_install.plan import packages as plan_packages

    catalog = load_catalog()
    both = replace(
        config(ext4_on_gpt()),
        packages=replace(config().packages, graphics=("nouveau", "nvidia")),
    )
    assert plan_packages.driver_conflict(both, catalog)
    with pytest.raises(ValidationFailed):
        plan_packages.build(both, catalog)
    # Two AMD generations are not the same card, so those two stay allowed.
    amd = replace(
        config(ext4_on_gpt()),
        packages=replace(config().packages, graphics=("amdgpu", "radeon")),
    )
    assert plan_packages.driver_conflict(amd, catalog) == ""


def test_every_exclusive_pair_names_two_groups_that_exist() -> None:
    """A pair naming a group the catalog does not have can never fire, which
    reads as coverage and is not."""
    from gentoo_install.plan.packages import EXCLUSIVE_DRIVERS

    catalog = load_catalog()
    assert EXCLUSIVE_DRIVERS
    for one, other, reason in EXCLUSIVE_DRIVERS:
        assert one in catalog and other in catalog, (one, other)
        assert reason.strip()


def test_the_blocked_row_lists_as_many_names_as_the_terminal_fits() -> None:
    """Naming one and counting the rest said `+3` on a 200-column console as
    readily as on an 80-column one."""
    from gentoo_install.tui import app
    from tests.unit.test_tui_app import context

    at = context()
    blank = replace(config(ext4_on_gpt()), system=replace(config().system, root_password_hash=""))
    at.columns = 200
    wide = app._blocked(blank, at)
    at.columns = 60
    narrow = app._blocked(blank, at)
    assert "+" not in wide, wide
    assert "+" in narrow, narrow
    assert len(wide) > len(narrow)


def _account(
    keys: list[str], at: screens.Context, start: InstallConfig | None = None
) -> Answer[InstallConfig]:
    from tests.unit.fake_screen import FakeScreen

    return screens.user_screen(
        FakeScreen(keys=keys, lines=24, columns=100),
        config(ext4_on_gpt()) if start is None else start,
        at,
    )


def test_the_account_is_one_form_and_a_wrong_answer_keeps_the_others() -> None:
    """Three screens in a row meant the operator confirmed a password before
    seeing whether the account gets sudo, and a mismatch threw away the name."""
    from tests.unit.test_tui_app import context

    at = context()
    down = "KEY_DOWN"
    keys = [
        # Name, two passwords that differ, tick sudo, groups, Done.
        *"zakk", down, *"one", down, *"two", down, " ", down, *"plugdev kvm", down, "\n",
        # Redrawn with the message: the name, sudo and the groups are still
        # there, so only the two passwords are retyped. Enter moves to the next
        # field and submits only from the Done row, so the last one is reached
        # first.
        down, *"same", down, *"same", down, down, down, "\n",
    ]
    user = _account(keys, at).unwrap().system.users[0]
    assert user.name == "zakk"
    assert user.sudo is True
    assert user.groups == ("plugdev", "kvm")
    assert user.password_hash == "$6$test$4"


def test_a_name_useradd_would_refuse_is_refused_on_the_form() -> None:
    """`useradd` rejects it after the disk has been partitioned, which is an
    hour too late to ask again."""
    from tests.unit.test_tui_app import context

    at = context()
    down = "KEY_DOWN"
    keys = [
        *"Zakk 1", down, *"same", down, *"same", down, down, down, "\n",
        # The name is still in the field, so it is corrected rather than
        # retyped from an empty form.
        *["\x7f"] * 6, *"zakk",
        down, *"same", down, *"same", down, down, down, "\n",
    ]
    assert _account(keys, at).unwrap().system.users[0].name == "zakk"


def test_an_account_with_no_password_is_refused() -> None:
    """It cannot log in, and the operator who left the field empty meant to
    skip the account, which the empty name does."""
    from tests.unit.test_tui_app import context

    at = context()
    down = "KEY_DOWN"
    # Rejected, so the form is redrawn with the message and the operator
    # leaves it rather than the screen exiting on their behalf. Escape, not
    # `q`: in a form `q` is a character a group name can contain.
    keys = [*"zakk", down, down, down, down, down, "\n", "\x1b"]
    assert not _account(keys, at).chosen


def test_no_account_at_all_is_still_one_keypress() -> None:
    """A server install leaves the system with root only."""
    from tests.unit.test_tui_app import context

    at = context()
    keys = ["KEY_DOWN"] * 5 + ["\n"]
    assert _account(keys, context()).unwrap().system.users == ()


def test_the_erase_row_is_not_reported_as_a_field_to_fill_in() -> None:
    """There is nothing to type: the operator is being asked to agree."""
    from gentoo_install.tui import app, settings
    from tests.unit.test_tui_app import context

    at = context()
    at.columns = 200
    at.visited.update(one.key for group in settings.SETTINGS for one in (group, *group.rows))
    ready = replace(
        config(ext4_on_gpt()),
        system=replace(config().system, root_password_hash="$6$t$x"),
        portage=replace(
            config().portage, mirrors=replace(config().portage.mirrors, site="tuna")
        ),
    )
    said = app._blocked(ready, at)
    assert "Confirm erasing the drive: not confirmed" == said, said


def test_the_legend_names_marks_the_rows_actually_carry() -> None:
    """The footer said `* required` over a menu that drew no `*`, so it
    described an interface that did not exist. The mark is the signal and the
    colour repeats it: a serial console with no colour shows the same thing."""
    from gentoo_install.tui import app
    from gentoo_install.tui.widgets import MARKS, Style
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    at = context()
    blank = replace(config(ext4_on_gpt()), system=replace(config().system, root_password_hash=""))
    # `q` opens the leaving menu, and its first row is Back to the menu.
    drawn = FakeScreen(keys=["q", "KEY_DOWN", "\n"], lines=40, columns=120)
    app.run(drawn, blank, at)
    page = "\n".join(drawn.frames[0])
    for style, mark in MARKS.items():
        assert f"\n{mark} " in page, f"the legend names {mark} and no row carries it"
    assert MARKS[Style.REQUIRED] in page.split("\n")[-1]


def test_pipewire_puts_the_account_in_the_group_its_postinst_asks_for() -> None:
    """`>=pipewire-0.3.66 uses the 'pipewire' group to manage permissions and
    limits needed to function smoothly`, from the ebuild's own postinst. It is
    the only package in the catalog that asks for a group the installer does
    not already hand every account."""
    from gentoo_install.model.config import User
    from gentoo_install.plan import packages as plan_packages

    catalog = load_catalog()
    installation = replace(
        config(ext4_on_gpt()),
        packages=replace(config().packages, applications=("pipewire",)),
        system=replace(config().system, users=(User(name="zakk"),)),
    )
    assert plan_packages.required_user_groups(installation, catalog) == ("pipewire",)
    added = [
        one
        for one in plan_packages.build(installation, catalog)
        if isinstance(one, plan_packages.AddUserToGroups)
    ]
    assert len(added) == 1
    assert added[0].user == "zakk" and added[0].groups == ("pipewire",)
    # After the merge that creates the group. `build` sorts stably by stage and
    # these share one, so emission order is what reaches the machine, and
    # `usermod` on a group no package has installed yet stops the run with the
    # disks already written.
    built = plan_packages.build(installation, catalog)
    assert built.index(added[0]) == len(built) - 1, [type(one).__name__ for one in built]
    recorder = Recorder()
    added[0].apply(recorder)
    # `-a`, or usermod replaces every supplementary group and takes the account
    # out of wheel.
    assert recorder.in_target == [("usermod", "-aG", "pipewire", "zakk")]


def test_a_group_every_account_already_gets_is_not_asked_for_again() -> None:
    """nvidia's postinst says to be in `video`, and `plan/system.py` puts every
    account there. Naming it again reads as something the installer does not
    already do."""
    from gentoo_install.plan import packages as plan_packages
    from gentoo_install.plan.system import USER_GROUPS

    catalog = load_catalog()
    for name, group in catalog.items():
        overlap = set(group.user_groups) & set(USER_GROUPS)
        assert not overlap, f"{name} names {overlap}, which every account already has"


def test_the_form_names_the_group_a_package_adds() -> None:
    """The account is put in it whatever is typed, so the row says so rather
    than an editable box holding a value it would discard."""
    from tests.unit.test_tui_app import context

    from tests.unit.fake_screen import FakeScreen

    at = context()
    with_audio = replace(
        config(ext4_on_gpt()), packages=replace(config().packages, applications=("pipewire",))
    )
    drawn = FakeScreen(keys=["\x1b"], lines=24, columns=110)
    screens.user_screen(drawn, with_audio, at)
    assert "Extra groups (+pipewire)" in drawn.last
    # And nothing to say when no chosen package asks for one.
    plain = FakeScreen(keys=["\x1b"], lines=24, columns=110)
    screens.user_screen(plain, config(ext4_on_gpt()), at)
    assert "Extra groups (+" not in plain.last


def test_a_configuration_beside_the_installer_is_offered_and_not_loaded() -> None:
    """A file called `my-install.toml` next to the installer is very likely the
    operator's own answers from before a reboot, and just as likely someone
    else's example. Offered, so neither is assumed."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    at = context()
    at.configs_here = ("my-install.toml",)
    theirs = replace(config(ext4_on_gpt()), system=replace(config().system, hostname="loaded"))
    at.load_config = lambda name: theirs
    # Down to the file and enter.
    picked = screens.saved_config_screen(
        FakeScreen(keys=["KEY_DOWN", "\n"], lines=24, columns=100), config(ext4_on_gpt()), at
    )
    assert picked.unwrap().system.hostname == "loaded"
    # The first row starts from scratch, so the answers behind it are kept.
    kept = screens.saved_config_screen(
        FakeScreen(keys=["\n"], lines=24, columns=100), config(ext4_on_gpt()), at
    )
    assert kept.unwrap().system.hostname == config().system.hostname


def test_no_file_beside_the_installer_asks_nothing() -> None:
    """The usual run has an empty directory, and a screen that appeared anyway
    would be one keypress before every install."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    at = context()
    # No keys at all: FakeScreen raises if the widget asks for one.
    answer = screens.saved_config_screen(
        FakeScreen(keys=[], lines=24, columns=100), config(ext4_on_gpt()), at
    )
    assert answer.unwrap() == config(ext4_on_gpt())


def test_a_file_that_will_not_parse_goes_back_to_the_list() -> None:
    """One unreadable file says nothing about the other beside it, and leaving
    the installer over it loses the language the operator just chose."""
    from gentoo_install.errors import ConfigError
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    def refuse(name: str) -> InstallConfig:
        raise ConfigError("line 3: not a size literal")

    at = context()
    at.configs_here = ("broken.toml",)
    at.load_config = refuse
    # Pick it, acknowledge the message, then start from scratch.
    answer = screens.saved_config_screen(
        FakeScreen(keys=["KEY_DOWN", "\n", "\n", "\n"], lines=24, columns=100),
        config(ext4_on_gpt()),
        at,
    )
    assert answer.unwrap() == config(ext4_on_gpt())


def test_the_overview_names_the_values_nobody_typed() -> None:
    """The last screen before the disk is written. `VIDEO_CARDS`, the USE the
    groups asked for, the group the account joins and the command line are all
    derived, so they appear nowhere the operator typed something."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    at = context()
    installation = replace(
        config(ext4_on_gpt()),
        packages=replace(config().packages, applications=("pipewire",), graphics=("nvidia",)),
        bootloader=replace(config().bootloader, kernel_params=("quiet",)),
    )
    drawn = FakeScreen(keys=["q"], lines=120, columns=130)
    screens.overview_screen(drawn, installation, at)
    page = drawn.last
    assert "added for you" in page
    for value in ("nvidia", "pipewire", "screencast", "root=UUID="):
        assert value in page, value


def test_the_overview_exports_without_taking_the_key_that_installs() -> None:
    """Enter on the overview means go ahead. A row that took that keypress for
    itself would publish instead of installing."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    sent: list[InstallConfig] = []

    def publish(one: InstallConfig) -> str:
        sent.append(one)
        return "https://paste.example/abc"

    at = context()
    at.publish_config = publish
    installation = config(ext4_on_gpt())
    # Enter straight away, then No to the install confirmation.
    accepted = screens.overview_screen(
        FakeScreen(keys=["\n", "\n"], lines=60, columns=130), installation, at
    )
    assert sent == [], "enter on the overview published instead of proceeding"
    assert accepted.outcome is Outcome.BACK

    # Up to the export row, enter, a key to leave the address, then cancel.
    screens.overview_screen(
        FakeScreen(keys=["KEY_UP", "\n", "\n", "q"], lines=60, columns=130), installation, at
    )
    assert len(sent) == 1


def test_a_pastebin_that_refuses_leaves_the_overview_standing() -> None:
    """The address is a convenience. Losing every answer because the network
    is down is not a trade the operator agreed to."""
    from gentoo_install.errors import GentooInstallError
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    def refuse(one: InstallConfig) -> str:
        raise GentooInstallError("paste.gentoozh.org did not answer")

    at = context()
    at.publish_config = refuse
    # Export, acknowledge the failure, then enter and No.
    answer = screens.overview_screen(
        FakeScreen(keys=["KEY_UP", "\n", "\n", "\n", "\n"], lines=60, columns=130),
        config(ext4_on_gpt()),
        at,
    )
    assert answer.outcome is Outcome.BACK


def test_opening_the_mirror_screen_and_changing_nothing_answers_it() -> None:
    """The row is required so nobody installs from a mirror they never looked
    at, and opening the screen is looking at it. Leaving the site unset made
    the row say `required` after it had been answered."""
    from gentoo_install.tui import settings
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    at = context()
    before = config(ext4_on_gpt())
    assert settings._mirror(before, at) == settings.UNSET
    # Down to Done and enter, touching nothing.
    after = screens.mirror_screen(
        FakeScreen(keys=["KEY_DOWN"] * 12 + ["\n"], lines=30, columns=110), before, at
    ).unwrap()
    at.visited.add("mirror")
    row = next(one for one in settings.SETTINGS if one.key == "mirror")
    assert settings._mirror(after, at) != settings.UNSET
    assert settings.settled(row, after, at)


def test_the_mirror_screen_shows_the_site_it_would_adopt() -> None:
    """Saying `not set` and then taking that site on the way out was the screen
    keeping the value it was about to use to itself."""
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    drawn = FakeScreen(keys=["q"], lines=30, columns=110)
    screens.mirror_screen(drawn, config(ext4_on_gpt()), context())
    line = next(one for one in drawn.last.splitlines() if "Gentoo mirror" in one)
    assert "not set" not in line
    assert "(default)" in line


def test_password_login_does_not_let_root_in_by_itself() -> None:
    """The row said `root included` and the installer wrote
    `PermitRootLogin no`: root is a second row, and it starts off."""
    from gentoo_install.plan.system import WriteSshdConfig
    from tests.unit.fake_screen import FakeScreen
    from tests.unit.test_tui_app import context

    at = context()
    # Down twice to `password login`, enter.
    chosen = screens.sshd_screen(
        FakeScreen(keys=["KEY_DOWN", "KEY_DOWN", "\n"], lines=24), config(ext4_on_gpt()), at
    ).unwrap()
    assert chosen.system.sshd_password_login is True
    assert chosen.system.sshd_root_login is False
    recorder = Recorder()
    WriteSshdConfig(password_login=True, root_login=False).apply(recorder)
    written = recorder.files[PurePosixPath("/etc/ssh/sshd_config.d/50-gentoo-install.conf")]
    assert "PermitRootLogin no" in written
    assert "PasswordAuthentication yes" in written


def test_the_kcm_flag_follows_plasma_and_not_the_input_method() -> None:
    """`app-i18n/fcitx-configtool[kcm]` pulls nine kde-frameworks packages and
    libplasma. Declared by the fcitx5 group it installed Plasma beside GNOME;
    declared by plasma it reaches the package only where those already are."""
    from gentoo_install.plan.packages import build as build_packages

    catalog = load_catalog()
    for desktop, wanted in (("gnome", False), ("plasma", True)):
        installation = replace(
            config(ext4_on_gpt()),
            packages=replace(
                config().packages, desktop=desktop, applications=("fcitx5", "rime")
            ),
        )
        written = [
            line
            for one in build_packages(installation, catalog)
            for line in getattr(one, "lines", ())
        ]
        assert any("fcitx-configtool kcm" in one for one in written) is wanted, desktop


def test_kwin_is_not_pointed_at_a_launcher_ibus_never_installs() -> None:
    """The launcher is fcitx's own desktop entry, so Plasma with ibus told KWin
    to exec a file no package puts on disk."""
    from gentoo_install.plan.packages import ConfigureKwinInputMethod
    from gentoo_install.plan.packages import build as build_packages

    catalog = load_catalog()
    for engine, wanted in (("fcitx5", True), ("ibus", False)):
        installation = replace(
            config(ext4_on_gpt()),
            packages=replace(
                config().packages, desktop="plasma", applications=(engine, "rime")
                if engine == "fcitx5"
                else (engine,),
            ),
        )
        built = build_packages(installation, catalog)
        told = any(isinstance(one, ConfigureKwinInputMethod) for one in built)
        assert told is wanted, engine


def test_a_greeter_with_no_seat_flag_is_not_handed_one() -> None:
    """`gui-libs/greetd` has `IUSE="selinux"`, so the line was one Portage warns
    about and drops."""
    from gentoo_install.plan.packages import build as build_packages

    catalog = load_catalog()
    for manager, wanted in (("sddm", True), ("greetd", False)):
        installation = replace(
            config(ext4_on_gpt()),
            packages=replace(config().packages, desktop="plasma", display_manager=manager),
        )
        written = [
            line
            for one in build_packages(installation, catalog)
            for line in getattr(one, "lines", ())
        ]
        assert any("elogind" in one or "systemd" in one for one in written) is wanted, manager


def test_every_rime_schema_installs_the_engine_that_reads_it() -> None:
    """`app-i18n/rime-data` installs data files and nothing else. A schema
    chosen on its own wrote `Name=rime` into the fcitx profile for an addon
    that was not on disk, and fcitx fell back to `keyboard-us`."""
    catalog = load_catalog()
    schemas = [name for name in catalog if name.startswith("rime")]
    assert len(schemas) >= 5, schemas
    for name in schemas:
        assert "app-i18n/fcitx-rime" in catalog[name].packages, name


def test_greetd_is_pointed_at_the_greeter_it_installs() -> None:
    """The ebuild's own `/etc/greetd/config.toml` runs `agreety`, so
    `gui-apps/tuigreet` was installed and never reached."""
    catalog = load_catalog()
    greetd = catalog["greetd"]
    assert "gui-apps/tuigreet" in greetd.packages
    written = {str(one.path): one.content for one in greetd.files}
    assert "/etc/greetd/config.toml" in written
    assert "tuigreet" in written["/etc/greetd/config.toml"]
    # The user and the vt the ebuild's patch sets, or greetd starts as root on
    # a console something else already owns.
    assert 'user = "greetd"' in written["/etc/greetd/config.toml"]
    assert "vt = 7" in written["/etc/greetd/config.toml"]


#: Where a group's packages may come from. GURU is unreviewed and its ebuilds
#: come and go, so anything `::gentoo` lacks is packaged in gentoo-zh instead.
ALLOWED_REPOSITORIES = frozenset({"gentoo-zh", "gig"})

_TREES = {
    "gentoo": Path("/var/db/repos/gentoo"),
    "gentoo-zh": Path("/var/db/repos/gentoo-zh"),
}


def test_no_group_takes_a_package_from_an_overlay_we_do_not_ship() -> None:
    """An installer that stops an hour in because a GURU ebuild moved is worse
    than not offering the package."""
    for name, group in load_catalog().items():
        wrong = set(group.repositories) - ALLOWED_REPOSITORIES
        assert not wrong, f"{name} takes packages from {sorted(wrong)}"


@pytest.mark.skipif(
    not all(one.is_dir() for one in _TREES.values()), reason="no Gentoo trees on this machine"
)
def test_every_package_a_group_names_exists_where_it_says() -> None:
    """A group that names an atom no repository carries fails at emerge time,
    an hour into an install that has already partitioned the disks. A group
    whose packages are all in `::gentoo` should declare no overlay, and one
    that needs gentoo-zh has to say so.
    """
    for name, group in load_catalog().items():
        for atom in group.packages:
            where = atom.split(":")[0]
            in_gentoo = (_TREES["gentoo"] / where).is_dir()
            in_overlay = (_TREES["gentoo-zh"] / where).is_dir()
            assert in_gentoo or in_overlay, f"{name} names {atom}, which neither tree has"
            if not in_gentoo:
                assert "gentoo-zh" in group.repositories, (
                    f"{name} names {atom}, which only gentoo-zh has, "
                    "and declares no repository"
                )


@pytest.mark.skipif(
    not all(one.is_dir() for one in _TREES.values()), reason="no Gentoo trees on this machine"
)
def test_a_group_whose_package_is_testing_only_says_so() -> None:
    """`media-video/obs-studio` carries `~amd64` in every version the tree has,
    so the default stable channel masks it and Portage says so in the package
    stage, an hour after the disks were written. A group naming such an atom
    has to declare the acceptance itself."""
    import re

    keywords = re.compile(r'^\s*KEYWORDS="([^"]*)"', re.MULTILINE)
    for name, group in load_catalog().items():
        accepted = {line.split()[0] for line in group.accept_keywords}
        for atom in group.packages:
            where = atom.split(":")[0]
            # `::gentoo` only: selecting an overlay writes `*/*::<repo> ~amd64`,
            # so everything in it is already accepted.
            directory = _TREES["gentoo"] / where
            if not directory.is_dir():
                continue
            said = [
                match.group(1)
                for ebuild in directory.glob("*.ebuild")
                if not ebuild.name.endswith("-9999.ebuild")
                for match in keywords.finditer(ebuild.read_text(errors="replace"))
            ]
            if not said:
                continue
            if any("amd64" in one.split() for one in said):
                continue
            assert where in accepted, (
                f"{name} names {atom}, which the tree carries only under testing "
                "keywords, and declares no accept_keywords line"
            )


def test_greetd_starts_the_desktop_rather_than_a_shell() -> None:
    """Its command was `tuigreet --cmd /bin/bash`, so a successful login opened
    a shell beside the desktop that had just been installed. tuigreet reads the
    session directories instead, and each desktop's own .desktop names its
    command."""
    written = load_catalog()["greetd"].files
    config = next(one for one in written if one.path.name == "config.toml")
    assert "/bin/bash" not in config.content
    assert "--sessions /usr/share/wayland-sessions" in config.content
    assert "--xsessions /usr/share/xsessions" in config.content


def test_the_pam_stack_gets_the_seat_flag_the_manager_needs() -> None:
    """`gnome-base/gdm` RDEPENDs `sys-auth/pambase[elogind?,systemd?]` and
    refuses the merge without it; sddm and lightdm merge and start a session
    that registers with no seat. The desktop profiles hide this with `elogind`
    in global USE, and the `console` group is built against the base profile,
    which has none."""
    from gentoo_install.model.config import InitSystem
    from gentoo_install.plan.packages import build as build_packages

    catalog = load_catalog()
    for init, flag in ((InitSystem.SYSTEMD, "systemd"), (InitSystem.OPENRC, "elogind")):
        installation = replace(
            config(ext4_on_gpt()),
            system=replace(config().system, init=init),
            packages=replace(config().packages, desktop="console", display_manager="gdm"),
        )
        written = [
            line
            for one in build_packages(installation, catalog)
            for line in getattr(one, "lines", ())
        ]
        assert f"sys-auth/pambase {flag}" in written, (init, written)
        assert f"gnome-base/gdm {flag}" in written, (init, written)


def test_pipewire_is_started_on_systemd_and_left_to_openrc() -> None:
    """The ebuild says it outright: "the out-of-the-box experience is automatic
    on OpenRC, while it needs manual intervention on systemd". Without the
    enable the packages merge and the desktop has no sound server.

    `--global`, not `--user`: no user instance is running during an install.
    """
    from gentoo_install.model.config import InitSystem
    from gentoo_install.plan.packages import EnableUserUnits
    from gentoo_install.plan.packages import build as build_packages

    catalog = load_catalog()
    for init, wanted in ((InitSystem.SYSTEMD, True), (InitSystem.OPENRC, False)):
        installation = replace(
            config(ext4_on_gpt()),
            system=replace(config().system, init=init),
            packages=replace(config().packages, applications=("pipewire",)),
        )
        told = [
            one
            for one in build_packages(installation, catalog)
            if isinstance(one, EnableUserUnits)
        ]
        assert bool(told) is wanted, init
        if not told:
            continue
        recorder = Recorder()
        told[0].apply(recorder)
        assert recorder.in_target == [
            (
                "systemctl",
                "--global",
                "--force",
                "enable",
                "pipewire.socket",
                "pipewire-pulse.socket",
                "wireplumber.service",
            )
        ]


def test_a_group_whose_two_inits_name_a_service_differently_says_both() -> None:
    """The nftables ebuild installs `nftables-load.service` and an OpenRC init
    named `nftables`, and no `nftables.service` exists: enabling that name
    under systemd failed after the package was already merged."""
    from dataclasses import replace

    from gentoo_install.model.config import InitSystem
    from gentoo_install.plan.packages import build as build_packages

    catalog = load_catalog()
    wanted = {InitSystem.OPENRC: "enable nftables ", InitSystem.SYSTEMD: "enable nftables-load "}
    for init, said in wanted.items():
        installation = replace(
            config(ext4_on_gpt()),
            system=replace(config().system, init=init),
            packages=replace(config().packages, applications=("nftables",)),
        )
        described = [one.describe() for one in build_packages(installation, catalog)]
        assert any(one.startswith(said) for one in described), (init, described)


def test_pipewire_asks_for_its_sound_server_without_a_desktop() -> None:
    """`sound-server` is in IUSE without a `+`, and only
    `targets/desktop/package.use` turns it on. Selected without a desktop, the
    ebuild comments out the pipewire-pulse launcher line while the group still
    enables `pipewire-pulse.socket`."""
    from dataclasses import replace

    from gentoo_install.plan.packages import build as build_packages

    installation = replace(
        config(ext4_on_gpt()),
        packages=replace(config().packages, desktop="", applications=("pipewire",)),
    )
    written = [
        line for one in build_packages(installation, load_catalog()) for line in getattr(one, "lines", ())
    ]
    assert "media-video/pipewire sound-server" in written, written


def test_an_engine_selected_alone_still_gets_its_framework() -> None:
    """`rime` on its own merged `app-i18n/fcitx-rime` and nothing else: no
    `app-i18n/fcitx`, no `fcitx-gtk` and no `fcitx-qt`, so no Gtk or Qt
    application could reach the engine that had just been installed."""
    from dataclasses import replace

    from gentoo_install.plan.packages import FRAMEWORK_GROUPS, groups

    catalog = load_catalog()
    engines = [
        name
        for name, group in catalog.items()
        if group.input_method and group.input_framework
    ]
    assert engines, "the catalog offers no input method"
    for name in engines:
        installation = replace(
            config(ext4_on_gpt()),
            packages=replace(config().packages, applications=(name,)),
        )
        chosen = {one.name for one in groups(installation, catalog)}
        wanted = FRAMEWORK_GROUPS[catalog[name].input_framework]
        assert wanted in chosen, (name, sorted(chosen))
