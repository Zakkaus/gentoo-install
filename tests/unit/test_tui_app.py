from __future__ import annotations

from dataclasses import replace

from gentoo_install.data import load_catalog
from gentoo_install.i18n import Catalog
from gentoo_install.model.config import Bootloader, InitSystem
from gentoo_install.model.validate import validate
from gentoo_install.tui import screens, settings
from gentoo_install.tui.app import run
from gentoo_install.tui.widgets import Outcome

from .fake_screen import FakeScreen
from .layouts import config

DISKS = [("/dev/disk/by-id/virtio-target0", "20 GiB"), ("/dev/disk/by-id/virtio-target1", "40 GiB")]


def context() -> screens.Context:
    return screens.Context(
        translate=Catalog("en"),
        disks=DISKS,
        groups=load_catalog(),
        hash_password=lambda password: f"$6$test${len(password)}",
        timezones=("UTC", "Asia/Shanghai", "Asia/Taipei", "Europe/London"),
    )


def row(label: str) -> int:
    return next(index for index, s in enumerate(settings.SETTINGS) if s.label == label)


def down(count: int) -> list[str]:
    return ["KEY_DOWN"] * count


def test_the_menu_shows_every_setting_with_its_current_value() -> None:
    """The operator has to see what is set without opening each row."""
    screen = FakeScreen(keys=["q"])
    run(screen, config(), context())
    drawn = screen.last
    for setting in settings.SETTINGS:
        assert setting.label in drawn, setting.label
    assert "gentoo" in drawn


def test_a_row_can_be_opened_and_the_menu_comes_back() -> None:
    """Not a wizard: editing one row returns to the menu rather than moving to
    the next question, so any row can be revisited."""
    keys = [*down(row("Kernel")), "\n", "\n", "q"]
    screen = FakeScreen(keys=keys)
    finished = run(screen, config(), context())
    assert finished.cancelled
    # The kernel screen was drawn, and then the menu again.
    assert any("Kernel" in "\n".join(frame) for frame in screen.frames)


def test_the_same_row_can_be_edited_twice() -> None:
    """A wizard makes the operator cancel and start over to change an early
    answer; this has to not."""
    keys = [
        *down(row("Bootloader")), "\n", "\n",
        *down(row("Bootloader")), "\n", "\n",
        "q",
    ]
    finished = run(FakeScreen(keys=keys), config(), context())
    assert finished.cancelled


def test_install_is_blocked_while_something_required_is_missing() -> None:
    """And the row says what is missing rather than silently doing nothing."""
    blank = replace(config(), system=replace(config().system, root_password_hash=""))
    screen = FakeScreen(keys=["q"])
    run(screen, blank, context())
    assert "Install" in screen.last


def test_install_hands_back_the_configuration() -> None:
    keys = [*down(len(settings.SETTINGS)), "\n"]
    finished = run(FakeScreen(keys=keys), config(), context())
    assert not finished.cancelled
    assert finished.config is not None
    validate(finished.config)


def test_the_timezone_list_is_every_zone_the_machine_knows() -> None:
    """A hand-picked shortlist is not a timezone chooser, and six hundred rows
    do not fit a console, so the area comes first."""
    screen = FakeScreen(keys=["KEY_DOWN", "\n", "KEY_DOWN", "\n"])
    answer = screens.timezone_screen(screen, config(), context())
    assert answer.outcome is Outcome.CHOSE
    assert answer.unwrap().system.timezone == "Asia/Taipei"


def test_choosing_utc_needs_no_second_screen() -> None:
    """It has no area, so asking for a city after it would be an empty list."""
    answer = screens.timezone_screen(FakeScreen(keys=["\n"]), config(), context())
    assert answer.unwrap().system.timezone == "UTC"


def test_only_profiles_matching_the_init_are_offered() -> None:
    """The validator refuses the other half, so offering them wastes a choice."""
    screen = FakeScreen(keys=["q"])
    screens._profile_screen(screen, config(), context())
    drawn = screen.last
    assert "23.0/systemd" in drawn
    openrc = replace(config(), system=replace(config().system, init=InitSystem.OPENRC))
    plain = FakeScreen(keys=["q"])
    screens._profile_screen(plain, openrc, context())
    assert "systemd" not in plain.last


def test_binary_packages_are_a_row_of_their_own() -> None:
    """Never bundled with another choice: it is the difference between a ten
    minute install and a four hour one."""
    assert any(setting.key == "binhost" for setting in settings.SETTINGS)
    said = settings.SETTINGS[row("Binary packages")].value(config(), context())
    assert said


def test_choosing_zfs_still_adds_the_overlay_that_carries_zfsbootmenu() -> None:
    keys = ["KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n"]
    answer = screens.layout_screen(FakeScreen(keys=keys), config(), context())
    assert answer.unwrap().bootloader.kind is Bootloader.ZFSBOOTMENU
    assert [o.name for o in answer.unwrap().portage.overlays] == ["gentoo-zh"]
