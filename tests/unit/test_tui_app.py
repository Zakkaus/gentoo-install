from __future__ import annotations

from dataclasses import replace

from gentoo_install.data import load_catalog
from gentoo_install.i18n import Catalog
from gentoo_install.model.config import Bootloader, InstallConfig
from gentoo_install.model.templates import Layout
from gentoo_install.model.validate import validate
from gentoo_install.tui import screens
from gentoo_install.tui.app import run
from gentoo_install.tui.widgets import Answer, Outcome, Screen

from .fake_screen import FakeScreen
from .layouts import config

DISKS = [("/dev/disk/by-id/virtio-target0", "20 GiB"), ("/dev/disk/by-id/virtio-target1", "40 GiB")]


def context() -> screens.Context:
    return screens.Context(
        translate=Catalog("en"),
        disks=DISKS,
        groups=load_catalog(),
        hash_password=lambda password: f"$6$test${len(password)}",
    )


def test_the_whole_walk_produces_a_configuration_that_validates() -> None:
    """The interface cannot produce something the file parser would reject:
    both end at the same model and the same validator."""
    disk = DISKS[1][0]
    keys = [
        "KEY_DOWN", "\n",                       # second disk
        "\n",                                   # ext4 whole disk
        "\n",                                   # no swap
        *list(disk), "\n",                      # type the disk name to erase
        "\n",                                   # locale: zh_TW
        "\n",                                   # timezone: Asia/Shanghai
        *["\x7f"] * len("gentoo"), *list("box"), "\n",  # clear the default, type a name
        "\n",                                   # init: systemd
        *list("secret"), "\n",                  # root password
        "\n",                                   # official binary packages
        "\n",                                   # kernel source
        "\n",                                   # bootloader
        "\n",                                   # no applications
        "\n",                                   # no sshd
        "\n",                                   # overview: scroll to the end
        "KEY_DOWN", "\n",                       # confirm the install
    ]
    finished = run(FakeScreen(keys=keys), config(), context())
    assert not finished.cancelled
    assert finished.config is not None
    validate(finished.config)
    assert finished.config.system.hostname == "box"
    assert finished.config.system.root_password_hash == "$6$test$6"
    assert finished.config.system.locale == "zh_TW.UTF-8"


def test_going_back_discards_only_the_last_answer() -> None:
    """Back has to restore what the previous screen was given, not replay the
    screens before it with an answer already applied."""
    seen: list[str] = []

    def first(screen: Screen, current: InstallConfig, context: screens.Context) -> Answer[InstallConfig]:
        seen.append("first")
        return Answer(Outcome.CHOSE, replace(current, system=replace(current.system, hostname="one")))

    def second(screen: Screen, current: InstallConfig, context: screens.Context) -> Answer[InstallConfig]:
        seen.append("second")
        if seen.count("second") == 1:
            return Answer(Outcome.BACK)
        return Answer(Outcome.CHOSE, current)

    finished = run(FakeScreen(), config(), context(), steps=(first, second))
    assert seen == ["first", "second", "first", "second"]
    assert finished.config is not None
    assert finished.config.system.hostname == "one"


def test_back_on_the_first_screen_leaves_the_installer() -> None:
    assert run(FakeScreen(keys=["KEY_LEFT"]), config(), context()).cancelled


def test_choosing_zfs_adds_the_only_overlay_that_carries_zfsbootmenu() -> None:
    """Otherwise the walk ends at a configuration the validator rejects for a
    reason the operator was never shown."""
    keys = ["\n", "KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n"]
    finished = run(FakeScreen(keys=keys), config(), context(), steps=screens.STEPS[:2])
    assert finished.config is not None
    assert finished.config.bootloader.kind is Bootloader.ZFSBOOTMENU
    assert [overlay.name for overlay in finished.config.portage.overlays] == ["gentoo-zh"]
    validate(finished.config)


def test_the_overview_lists_what_the_installer_will_actually_do() -> None:
    """Built from the operation sequence itself, so the screen cannot promise
    something the installer does not perform."""
    screen = FakeScreen(keys=["\n", "KEY_DOWN", "\n"])
    finished = run(screen, config(), context(), steps=(screens.overview_screen,))
    assert not finished.cancelled
    drawn = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "wipe existing signatures" in drawn
    assert "operations:" in drawn


def test_declining_the_overview_goes_back_rather_than_installing() -> None:
    screen = FakeScreen(keys=["\n", "\n"])
    finished = run(screen, config(), context(), steps=(screens.overview_screen,))
    assert finished.cancelled
