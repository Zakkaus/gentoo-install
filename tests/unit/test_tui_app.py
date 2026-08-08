from __future__ import annotations

from dataclasses import replace

from gentoo_install.data import load_catalog
from gentoo_install.i18n import Catalog
from gentoo_install.model.config import (
    Bootloader,
    InitSystem,
    Keywords,
    MirrorConfig,
    MirrorRegion,
)
from gentoo_install.model.validate import validate
from gentoo_install.tui import screens, settings
from gentoo_install.tui.app import run
from gentoo_install.tui.widgets import Outcome

from .fake_screen import FakeScreen
from .layouts import config

DISKS = [("/dev/disk/by-id/virtio-target0", "20 GiB"), ("/dev/disk/by-id/virtio-target1", "40 GiB")]

#: A real key, from ssh-keygen: the checker walks the body, so a made-up
#: string would fail for the wrong reason.
GOOD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB+85deBslaLOMFw71dx23wo7fFT76GVcEyQS9IdVvvT test@example"


#: What the screen handed to `stage_passphrase`, so a test can assert the
#: passphrase left the screen without appearing on it.
STAGED: list[str] = []


def staged(text: str) -> str:
    STAGED.append(text)
    return "/run/keys/tui"


def context() -> screens.Context:
    STAGED.clear()
    return screens.Context(
        translate=Catalog("en"),
        disks=DISKS,
        groups=load_catalog(),
        hash_password=lambda password: f"$6$test${len(password)}",
        stage_passphrase=staged,
        timezones=("UTC", "Asia/Shanghai", "Asia/Taipei", "Europe/London"),
    )


def row(label: str) -> int:
    return next(index for index, s in enumerate(settings.SETTINGS) if s.label == label)


def down(count: int) -> list[str]:
    return ["KEY_DOWN"] * count


def test_every_row_is_reachable_and_shows_its_current_value() -> None:
    """More rows than an 80x24 console holds, so the list scrolls and the test
    walks it rather than reading one frame."""
    at = context()
    screen = FakeScreen(keys=[*down(len(settings.SETTINGS)), "q", "KEY_DOWN", "\n"])
    run(screen, config(), at)
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    for setting in settings.SETTINGS:
        assert setting.label in seen, setting.label
        assert setting.value(config(), at) in seen or setting.required, setting.label


def test_the_firmware_row_is_shown_and_not_chosen() -> None:
    """The UEFI and BIOS paths differ, and installing for the one the machine
    did not boot is a mistake rather than an option."""
    firmware = next(s for s in settings.SETTINGS if s.key == "firmware")
    assert firmware.edit is None
    at = context()
    screen = FakeScreen(keys=["q", "KEY_DOWN", "\n"])
    run(screen, config(), at)
    # `last` is now the confirmation, so look at the menu frame before it.
    assert any(
        "uefi (detected) - detected from this machine" in "\n".join(frame)
        for frame in screen.frames
    )


def test_the_menu_is_flat() -> None:
    """One row per decision. Nesting hides a choice behind a heading nobody
    opens, which is what the maintainer asked to be rid of."""
    assert len(settings.SETTINGS) > 20
    for setting in settings.SETTINGS:
        if setting.edit is not None:
            assert "menu" not in setting.edit.__name__


def test_a_row_can_be_opened_and_the_menu_comes_back() -> None:
    """Not a wizard: editing one row returns to the menu rather than moving to
    the next question, so any row can be revisited."""
    keys = [*down(row("Kernel")), "\n", "\n", "q", "KEY_DOWN", "\n"]
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
        "q", "KEY_DOWN", "\n",
    ]
    finished = run(FakeScreen(keys=keys), config(), context())
    assert finished.cancelled


def test_install_is_blocked_while_something_required_is_missing() -> None:
    """And the row says what is missing rather than silently doing nothing."""
    blank = replace(config(), system=replace(config().system, root_password_hash=""))
    at = context()
    screen = FakeScreen(keys=[*down(len(settings.SETTINGS)), "q", "KEY_DOWN", "\n"])
    run(screen, blank, at)
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "Root password" in seen
    assert "still needs an answer" in seen


def test_install_hands_back_the_configuration() -> None:
    at = context()
    at.erase_confirmed = True
    ready = replace(
        config(), system=replace(config().system, root_password_hash="$6$test$x")
    )
    keys = [*down(len(settings.SETTINGS)), "\n"]
    finished = run(FakeScreen(keys=keys), ready, at)
    assert not finished.cancelled
    assert finished.config is not None
    validate(finished.config)


def test_erasing_the_drive_is_a_row_that_has_to_be_confirmed() -> None:
    """It is required, so the install row stays blocked until the operator has
    typed the drive name once."""
    at = context()
    screen = FakeScreen(keys=[*down(len(settings.SETTINGS)), "q", "KEY_DOWN", "\n"])
    run(screen, config(), at)
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "Confirm erasing the drive" in seen


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
    screen = FakeScreen(keys=["q", "KEY_DOWN", "\n"])
    screens._profile_screen(screen, config(), context())
    drawn = screen.last
    assert "23.0/systemd" in drawn
    openrc = replace(config(), system=replace(config().system, init=InitSystem.OPENRC))
    plain = FakeScreen(keys=["q", "KEY_DOWN", "\n"])
    screens._profile_screen(plain, openrc, context())
    assert "systemd" not in plain.last


def test_binary_packages_are_a_row_of_their_own() -> None:
    """Never bundled with another choice: it is the difference between a ten
    minute install and a four hour one."""
    assert any(setting.key == "binhost" for setting in settings.SETTINGS)
    said = settings.SETTINGS[row("Binary packages")].value(config(), context())
    assert said


def test_choosing_zfs_asks_before_adding_the_overlay() -> None:
    """ZFSBootMenu is in gentoo-zh and in no other repository, so choosing it
    is consenting to that overlay rather than having it added silently."""
    zfs = ["KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n"]
    screen = FakeScreen(keys=[*zfs, "\n"])
    answer = screens.layout_screen(screen, config(), context())
    assert answer.unwrap().bootloader.kind is Bootloader.ZFSBOOTMENU
    assert [o.name for o in answer.unwrap().portage.overlays] == ["gentoo-zh"]
    assert "gentoo-zh" in screen.last or "gentoo-zh" in "\n".join(
        "\n".join(frame) for frame in screen.frames
    )


def test_declining_the_overlay_leaves_zfs_on_systemd_boot() -> None:
    """The other bootloader a ZFS root can use, and it needs no overlay."""
    zfs = ["KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n"]
    answer = screens.layout_screen(FakeScreen(keys=[*zfs, "KEY_DOWN", "\n"]), config(), context())
    assert answer.unwrap().bootloader.kind is Bootloader.SYSTEMD_BOOT
    assert answer.unwrap().portage.overlays == ()


def test_the_passphrase_is_typed_here_and_never_drawn() -> None:
    """The operator types a passphrase, not the name of a file holding one, and
    the screen stages the file itself."""
    at = context()
    typed = list("hunter2hunter2")
    keys = ["KEY_DOWN", "\n", *typed, "\n", *typed, "\n"]
    screen = FakeScreen(keys=keys)
    answer = screens.encryption_screen(screen, config(), at)
    assert answer.outcome is Outcome.CHOSE
    assert STAGED == ["hunter2hunter2"]
    assert at.choice.passphrase_file == "/run/keys/tui"
    drawn = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "hunter2hunter2" not in drawn
    assert "*" * 14 in drawn


def test_a_passphrase_typed_twice_differently_is_refused() -> None:
    at = context()
    keys = [
        "KEY_DOWN", "\n",
        *list("hunter2hunter2"), "\n",
        *list("hunter2different"), "\n",
        "\n",
        *list("hunter2hunter2"), "\n",
        *list("hunter2hunter2"), "\n",
    ]
    answer = screens.encryption_screen(FakeScreen(keys=keys), config(), at)
    assert answer.outcome is Outcome.CHOSE
    assert STAGED == ["hunter2hunter2"]


def test_a_passphrase_zfs_would_refuse_is_caught_before_the_disks_are_touched() -> None:
    """`zpool create` rejects anything under eight characters, and it rejects
    it after the vdevs are partitioned."""
    at = context()
    keys = [
        "KEY_DOWN", "\n",
        *list("short"), "\n",
        "\n",
        *list("longenough"), "\n",
        *list("longenough"), "\n",
    ]
    screen = FakeScreen(keys=keys)
    screens.encryption_screen(screen, config(), at)
    assert STAGED == ["longenough"]
    assert "too short" in "\n".join("\n".join(frame) for frame in screen.frames)


def test_declining_encryption_clears_the_passphrase() -> None:
    at = context()
    at.choice = replace(at.choice, passphrase_file="/run/keys/old")
    screens.encryption_screen(FakeScreen(keys=["\n"]), config(), at)
    assert at.choice.passphrase_file == ""


def test_escape_asks_before_throwing_the_answers_away() -> None:
    """One stray escape should not discard everything the operator entered."""
    at = context()
    # Answer No to the question, then leave properly the second time.
    screen = FakeScreen(keys=["q", "\n", "q", "KEY_DOWN", "\n"])
    finished = run(screen, config(), at)
    assert finished.cancelled
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "Leave without installing?" in seen
    # The menu was drawn again after the first refusal.
    assert seen.count("Keyboard layout") >= 2


def test_v3_is_recommended_only_when_this_cpu_runs_it() -> None:
    """`ld.so` lists the subarchitectures it would search, so a machine that
    cannot run them is told rather than left to meet an illegal instruction."""
    modern = context()
    modern.supports_v3 = True
    screen = FakeScreen(keys=["\n"])
    answer = screens.binhost_screen(screen, config(), modern)
    assert answer.unwrap().portage.binhost.subarch == "x86-64-v3"

    old = context()
    old.supports_v3 = False
    plain = FakeScreen(keys=["\n"])
    answer = screens.binhost_screen(plain, config(), old)
    assert answer.unwrap().portage.binhost.subarch == "x86-64"
    assert "this CPU cannot run it" in plain.last


def test_a_key_typed_into_the_screen_is_checked_before_it_is_kept() -> None:
    """A truncated key is discovered at the first login attempt, by which time
    the console the operator would fix it from is gone."""
    at = context()
    keys = ["\n", *"ssh-ed25519 AAAAtruncated", "\n", "\n", *down(3), "\n"]
    screen = FakeScreen(keys=keys)
    answer = screens.authorized_keys_screen(screen, config(), at)
    assert answer.unwrap().system.authorized_keys == ()
    assert "base64" in "\n".join("\n".join(frame) for frame in screen.frames)


def test_a_key_can_be_fetched_from_a_url() -> None:
    """Both forms of the paste address reach the text, and the comment line the
    paste carries is skipped rather than stored as a key."""
    at = context()
    asked: list[str] = []

    def fetched(url: str) -> str:
        asked.append(url)
        return f"# {url}\n{GOOD_KEY}\n"

    at.fetch_text = fetched
    keys = [*down(2), "\n", *"https://paste.gentoozh.org/abc", "\n", *down(4), "\n"]
    answer = screens.authorized_keys_screen(FakeScreen(keys=keys), config(), at)
    assert answer.unwrap().system.authorized_keys == (GOOD_KEY,)
    assert asked == ["https://paste.gentoozh.org/abc"]


def test_a_screen_cancelled_by_mistake_asks_before_discarding_everything() -> None:
    """Escape inside a row used to end the run outright, which threw away every
    other answer without asking."""
    at = context()
    # Open the kernel row, escape out of it, answer No, then leave properly.
    keys = [*down(row("Kernel")), "\n", "q", "\n", "q", "KEY_DOWN", "\n"]
    screen = FakeScreen(keys=keys)
    finished = run(screen, config(), at)
    assert finished.cancelled
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "Leave without installing?" in seen
    # The menu came back after the refusal rather than the run ending there.
    assert seen.count("gentoo-install") >= 2


def test_a_paste_needs_only_its_identifier() -> None:
    """Typing the whole address onto a console by hand is what this avoids; the
    host never changes, so only the identifier is asked for."""
    at = context()
    asked: list[str] = []

    def fetched(url: str) -> str:
        asked.append(url)
        return GOOD_KEY

    at.fetch_text = fetched
    keys = ["KEY_DOWN", "\n", *"hjq+353Jzfk", "\n", *down(4), "\n"]
    answer = screens.authorized_keys_screen(FakeScreen(keys=keys), config(), at)
    assert asked == ["https://paste.gentoozh.org/raw/hjq+353Jzfk"]
    assert answer.unwrap().system.authorized_keys == (GOOD_KEY,)


def test_choosing_the_traditional_catalog_moves_the_defaults_with_it() -> None:
    """Someone reading Traditional Chinese is in Taipei rather than Shanghai, and the CN
    mirrors are the wrong side of a border for them."""
    taiwan = screens.with_language(config(), "zh-TW")
    assert taiwan.system.timezone == "Asia/Taipei"
    assert taiwan.system.locale == "zh_TW.UTF-8"
    assert taiwan.portage.mirrors.region is MirrorRegion.GLOBAL
    china = screens.with_language(config(), "zh-CN")
    assert china.system.timezone == "Asia/Shanghai"
    assert china.system.locale == "zh_CN.UTF-8"
    assert china.portage.mirrors.region is MirrorRegion.CN


def test_every_interface_language_has_defaults_of_its_own() -> None:
    """A language offered on the first screen and missing from the table leaves
    the operator on someone else's timezone."""
    offered = {tag for tag, _, _ in screens.INTERFACE_LANGUAGES}
    assert offered == set(screens.LANGUAGE_DEFAULTS)
    for tag, chosen in screens.LANGUAGE_DEFAULTS.items():
        seeded = screens.with_language(config(), tag)
        assert chosen.locale in seeded.system.locales, tag


def test_the_kernel_row_names_the_package() -> None:
    """`dist-bin` says nothing about which kernel is about to be installed."""
    at = context()
    screen = FakeScreen(keys=["q"], lines=30, columns=100)
    screens.kernel_screen(screen, config(), at)
    assert "sys-kernel/gentoo-cjk-kernel" in screen.last
    assert "sys-kernel/gentoo-kernel-bin" in screen.last
    assert settings.SETTINGS[row("Kernel")].value(config(), at).startswith("sys-kernel/")


def test_keywords_are_a_row_of_their_own() -> None:
    """Whether the installed system tracks ~amd64 is a decision, not a detail
    of another one."""
    at = context()
    answer = screens.keywords_screen(FakeScreen(keys=["KEY_DOWN", "\n"]), config(), at)
    assert answer.unwrap().portage.keywords is Keywords.TESTING


def test_the_mirror_row_shows_every_service_and_lets_each_be_chosen() -> None:
    """Four services, and which of them a mirror serves is not something the
    operator can guess from its name."""
    at = context()
    screen = FakeScreen(keys=["q"], lines=30, columns=110)
    screens.mirror_screen(screen, config(), at)
    drawn = screen.last
    for label in ("Region", "Gentoo mirror", "Repository sync", "rsync", "gentoo-zh mirror"):
        assert label in drawn, label


def test_the_two_repositories_are_chosen_apart() -> None:
    """They hold different files and do not offer the same set of sites."""
    at = context()
    keys = [*down(5), "\n", "KEY_DOWN", "\n", *down(8), "\n"]
    answer = screens.mirror_screen(FakeScreen(keys=keys, lines=30), config(), at)
    chosen = answer.unwrap().portage.mirrors
    assert chosen.gentoo_zh is not MirrorConfig().gentoo_zh
    assert chosen.region is MirrorConfig().region


def test_the_gentoozh_distfiles_are_appended_and_never_ranked() -> None:
    """They hold the overlay's own sources, so ranking them with the main
    mirrors would order one repository by how fast the other answers."""
    from gentoo_install.model.config import GentooZhMirror
    from gentoo_install.plan.portage import _appended_distfiles

    off = replace(config().portage, mirrors=MirrorConfig())
    assert _appended_distfiles(off) == ()
    on = replace(
        config().portage,
        mirrors=MirrorConfig(gentoo_zh=GentooZhMirror.NJU, gentoo_zh_distfiles=True),
    )
    appended = _appended_distfiles(on)
    assert appended[0].endswith("nju.edu.cn/gentoo-zh")
    assert appended[-1] == "https://distfiles.gentoozh.org"


def test_the_compiler_row_gathers_the_four_that_are_read_together() -> None:
    at = context()
    screen = FakeScreen(keys=["q"], lines=30, columns=110)
    screens.compiler_screen(screen, config(), at)
    drawn = screen.last
    for label in ("Compile jobs", "Compiler flags", "CPU flags", "Licenses", "Package keywords"):
        assert label in drawn, label


def test_the_driver_is_one_choice_and_not_a_row_to_tick() -> None:
    """A machine has one graphics driver, so ticking two in the applications
    list would put two VIDEO_CARDS values in make.conf."""
    at = context()
    answer = screens.graphics_screen(FakeScreen(keys=[*down(5), "\n"], lines=30), config(), at)
    assert answer.unwrap().packages.graphics == "nvidia"
    offered = FakeScreen(keys=["q"], lines=30, columns=100)
    screens.packages_screen(offered, config(), at)
    for name, _ in screens.GRAPHICS:
        if name:
            assert f"  {name} " not in offered.last, name


def test_a_desktop_no_longer_picks_the_login_screen_for_the_operator() -> None:
    """Which login screen to run is a decision of its own, and the profiles
    used to each name one."""
    at = context()
    for name in ("plasma", "gnome", "xfce"):
        group = at.groups[name]
        assert not group.services, name
        assert not any(atom.endswith(("sddm", "gdm", "lightdm")) for atom in group.packages), name
    answer = screens.display_manager_screen(FakeScreen(keys=["KEY_DOWN", "\n"]), config(), at)
    assert answer.unwrap().packages.display_manager == "sddm"


def test_the_proprietary_driver_widens_the_licences_it_needs() -> None:
    """NVIDIA-2025 is in @BINARY-REDISTRIBUTABLE, so the default @FREE refuses
    it and the emerge dies an hour into the install."""
    from gentoo_install.plan import packages as plan_packages

    chosen = replace(config(), packages=replace(config().packages, graphics="nvidia"))
    assert "@BINARY-REDISTRIBUTABLE" in plan_packages.required_licenses(chosen, load_catalog())
    plain = replace(config(), packages=replace(config().packages, graphics="nouveau"))
    assert plan_packages.required_licenses(plain, load_catalog()) == ("@FREE",)


def test_the_nvidia_drop_in_does_not_collide_with_the_one_the_ebuild_installs() -> None:
    """x11-drivers/nvidia-drivers ships /etc/modprobe.d/nvidia.conf itself and
    adds it to dracut's install_items; writing over it is a collision."""
    nvidia = load_catalog()["nvidia"]
    assert [str(one.path) for one in nvidia.files] == ["/etc/modprobe.d/nvidia-modeset.conf"]
    assert "modeset=1" in nvidia.files[0].content
