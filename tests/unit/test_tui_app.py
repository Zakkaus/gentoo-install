from __future__ import annotations

from dataclasses import replace

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.i18n import Catalog
from gentoo_install.model.config import (
    Bootloader,
    InitSystem,
    Keywords,
    MirrorConfig,
    MirrorRegion,
)
from gentoo_install.model import manual
from gentoo_install.model.device import FilesystemType
from gentoo_install.model.validate import validate
from gentoo_install.tui import screens, settings
from gentoo_install.tui.app import run
from gentoo_install.tui.widgets import Outcome

from .fake_screen import FakeScreen
from .layouts import config, zfs_root

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
    """Where the row sits in `SETTINGS`."""
    return next(index for index, s in enumerate(settings.SETTINGS) if s.label == label)


def steps(label: str, rows: tuple[settings.Setting, ...] | None = None) -> int:
    """How many KEY_DOWNs reach that row. The menu steps over a row it cannot
    open, so this counts the ones it can and not the position in the tuple."""
    reachable = [one for one in (rows or settings.SETTINGS) if one.edit is not None]
    return next(index for index, one in enumerate(reachable) if one.label == label)


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


def test_a_group_is_a_list_and_never_a_wizard() -> None:
    """Nesting is for one subject read as one row; behind it the rows are
    re-enterable in any order, which is the main menu's own loop."""
    grouped = [one for one in settings.SETTINGS if one.rows]
    assert grouped and all(one.edit is not None for one in grouped)
    for setting in settings.SETTINGS:
        if setting.edit is not None:
            assert "wizard" not in setting.edit.__name__


def test_a_row_can_be_opened_and_the_menu_comes_back() -> None:
    """Not a wizard: editing one row returns to the menu rather than moving to
    the next question, so any row can be revisited."""
    keys = [*down(steps("Kernel")), "\n", "\n", "q", "KEY_DOWN", "\n"]
    screen = FakeScreen(keys=keys)
    finished = run(screen, config(), context())
    assert finished.cancelled
    # The kernel screen was drawn, and then the menu again.
    assert any("Kernel" in "\n".join(frame) for frame in screen.frames)


def test_the_same_row_can_be_edited_twice() -> None:
    """A wizard makes the operator cancel and start over to change an early
    answer; this has to not."""
    # The menu remembers where the cursor was, so the second visit is one
    # keystroke and not the walk down again.
    keys = [
        *down(steps("Bootloader")), "\n", "\n",
        "\n", "\n",
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
        config(),
        system=replace(config().system, root_password_hash="$6$test$x"),
        portage=replace(config().portage, mirrors=replace(config().portage.mirrors, site="tuna")),
    )
    # The Install row opens the overview: enter leaves the operation list, and
    # the confirmation starts on No, so Yes takes one more key.
    keys = [*down(len(settings.SETTINGS)), "\n", "\n", "KEY_DOWN", "\n"]
    screen = FakeScreen(keys=keys)
    finished = run(screen, ready, at)
    assert not finished.cancelled
    assert finished.config is not None
    validate(finished.config)
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "Overview" in seen and "operations:" in seen


def test_the_install_row_shows_every_operation_before_it_starts() -> None:
    """`overview_screen` was written and never wired: choosing Install went
    straight to partitioning the disk with no list and no confirmation."""
    at = context()
    at.erase_confirmed = True
    ready = replace(
        config(),
        system=replace(config().system, root_password_hash="$6$test$x"),
        portage=replace(config().portage, mirrors=replace(config().portage.mirrors, site="tuna")),
    )
    # Enter leaves the operation list, then No, which returns to the menu.
    keys = [*down(len(settings.SETTINGS)), "\n", "\n", "\n", "q", "KEY_DOWN", "\n"]
    screen = FakeScreen(keys=keys)
    finished = run(screen, ready, at)
    assert finished.cancelled
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "wipe existing signatures" in seen


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


def test_every_repository_choice_is_on_the_mirror_screen() -> None:
    """One screen: an overlay selected with no mirror behind it, or a binhost
    for a repository nobody selected, are states two rows apart can reach."""
    keys = {setting.key for setting in settings.SETTINGS}
    assert "mirror" in keys
    assert not {"binhost", "sync", "repositories"} & keys


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
    answer = screens._edit_binhost(FakeScreen(keys=["\n"]), modern, config())
    assert answer is not None
    assert answer.portage.binhost.subarch == "x86-64-v3"

    old = context()
    old.supports_v3 = False
    plain = FakeScreen(keys=["\n"])
    refused = screens._edit_binhost(plain, old, config())
    assert refused is not None
    assert refused.portage.binhost.official is False
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
    keys = [*down(steps("Kernel")), "\n", "q", "\n", "q", "KEY_DOWN", "\n"]
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
    """Which of them a mirror serves is not something the operator can guess
    from its name, so each is drawn with where it will come from."""
    at = context()
    screen = FakeScreen(keys=["q"], lines=40, columns=120)
    screens.mirror_screen(screen, config(), at)
    drawn = screen.last
    for label in (
        "Region", "Gentoo mirror", "Gentoo distfiles", "Repository sync",
        "Gentoo tree from", "Gentoo binary packages", "gentoo-zh", "guru",
    ):
        assert label in drawn, label


def test_choosing_a_gentoozh_mirror_is_what_adds_the_overlay() -> None:
    """A mirror chosen for an overlay nobody selected changes nothing, so the
    two are one row."""
    at = context()
    zh = next(index for index, item in enumerate(screens._mirror_fields(config(), at.translate))
              if item.value == screens._ZH_SITE)
    keys = [*down(zh), "\n", "KEY_DOWN", "KEY_DOWN", "\n", *down(20), "\n"]
    answer = screens.mirror_screen(FakeScreen(keys=keys, lines=40), config(), at)
    changed = answer.unwrap().portage
    assert [one.name for one in changed.overlays] == ["gentoo-zh"]
    assert changed.mirrors.gentoo_zh is not MirrorConfig().gentoo_zh


def test_the_gentoozh_distfiles_are_appended_and_never_ranked() -> None:
    """They hold the overlay's own sources, so ranking them with the main
    mirrors would order one repository by how fast the other answers."""
    from gentoo_install.model.config import GentooZhMirror
    from gentoo_install.plan.portage import _appended_distfiles

    off = replace(config().portage, mirrors=MirrorConfig(gentoo_zh_distfiles=False))
    assert _appended_distfiles(off) == ()
    on = replace(
        config().portage,
        mirrors=MirrorConfig(gentoo_zh=GentooZhMirror.NJU, gentoo_zh_distfiles=True),
    )
    appended = _appended_distfiles(on)
    assert appended[0].endswith("nju.edu.cn/gentoo-zh")
    assert "https://distfiles.gentoozh.org" in appended


def test_a_grouped_row_shows_its_own_rows_and_comes_back() -> None:
    """Six decisions about one subject read as six unrelated rows in a menu of
    thirty. Behind one row they read as the subject they belong to."""
    at = context()
    for title, rows in (
        ("Disk", settings.DISK),
        ("Compiler", settings.COMPILER),
        ("SSH", settings.SSH),
    ):
        screen = FakeScreen(keys=["q"], lines=40, columns=120)
        settings.nested(title, rows)(screen, config(), at)
        for row in rows:
            assert row.label in screen.last, (title, row.label)


def test_a_grouped_row_names_the_row_behind_it_that_is_missing() -> None:
    """`Disk` says nothing about which of its six the operator has not reached."""
    blank = replace(config(), system=replace(config().system, root_password_hash=""))
    assert "Root password" in settings.unanswered(blank, context())
    assert "Drive" in settings.unanswered(blank, context()) or config().disk.graph


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


def test_a_chinese_interface_defaults_to_the_patched_kernel() -> None:
    """cjktty is what puts CJK on the console, and it is in gentoo-zh and
    nowhere else, so the overlay is ticked with it rather than after it."""
    from gentoo_install.model.config import KernelSource

    for tag in ("zh-CN", "zh-TW"):
        seeded = screens.with_language(config(), tag)
        assert seeded.kernel.source is KernelSource.CJK, tag
        assert seeded.system.console_cjk, tag
        assert [one.name for one in seeded.portage.overlays] == ["gentoo-zh"], tag
        validate(seeded)


def test_other_languages_are_not_pulled_into_that_overlay() -> None:
    """The patch covers their scripts too, but the overlay is a Chinese one and
    defaulting into it is a decision they did not make."""
    from gentoo_install.model.config import KernelSource

    for tag in ("en", "ja", "ko"):
        seeded = screens.with_language(config(), tag)
        assert seeded.kernel.source is not KernelSource.CJK, tag
        assert seeded.portage.overlays == (), tag


def test_the_tree_row_names_the_address_the_chosen_method_uses() -> None:
    """Showing a git address under a webrsync choice is the interface
    contradicting itself, which is what the maintainer read off the screen."""
    from gentoo_install.model.config import Sync

    at = context()
    for method, expected in (
        (Sync.GIT, ".git"),
        (Sync.RSYNC, "rsync://"),
        (Sync.WEBRSYNC, "GENTOO_MIRRORS"),
    ):
        chosen = replace(config(), portage=replace(config().portage, sync=method))
        rows = screens._mirror_fields(chosen, at.translate)
        detail = next(one.detail for one in rows if one.label == "Gentoo tree from")
        assert expected in detail, method


def test_rsync_configures_the_repository_without_pulling_in_git() -> None:
    """rsync needs none, and the stage3 already carries the binary."""
    from gentoo_install.model.config import Sync
    from gentoo_install.plan import portage as plan_portage

    chosen = replace(config(), portage=replace(config().portage, sync=Sync.RSYNC))
    described = [one.describe() for one in plan_portage.build(chosen, "https://example.invalid")]
    assert not any("dev-vcs/git" in line for line in described)
    assert any("rsync://" in line for line in described)


def test_the_static_address_is_one_page_with_every_field_on_it() -> None:
    """Six fields answered one screen at a time are six questions the operator
    never sees together, and an address is exactly one setting."""
    at = context()
    # `No` is first in a Confirm, and No here means a static address.
    keys = [
        "\n",
        *"enp1s0", "KEY_DOWN",
        *"192.0.2.10/24", "KEY_DOWN",
        *"192.0.2.1", "KEY_DOWN",
        *"2001:db8::2/64", "KEY_DOWN",
        *"fe80::1", "KEY_DOWN",
        *"1.1.1.1 9.9.9.9", "KEY_DOWN", "\n",
    ]
    screen = FakeScreen(keys=keys, lines=30, columns=100)
    answer = screens.address_screen(screen, config(), at)
    system = answer.unwrap().system
    assert system.interface == "enp1s0"
    assert system.addresses == ("192.0.2.10/24", "2001:db8::2/64")
    assert system.gateways == ("192.0.2.1", "fe80::1")
    assert system.dns == ("1.1.1.1", "9.9.9.9")


def test_choosing_dhcp_clears_every_static_field() -> None:
    at = context()
    filled = replace(
        config(),
        system=replace(
            config().system,
            addresses=("192.0.2.10/24",),
            gateways=("192.0.2.1",),
            dns=("1.1.1.1",),
        ),
    )
    answer = screens.address_screen(FakeScreen(keys=["KEY_DOWN", "\n"]), filled, at)
    system = answer.unwrap().system
    assert system.addresses == () and system.gateways == () and system.dns == ()


def test_the_reuse_row_lists_what_is_on_the_disk_and_keeps_it_by_default() -> None:
    """Every partition the operator leaves alone keeps its data, so `keep` is
    the default and formatting is a choice per row."""
    at = context()
    at.existing = (
        ("/dev/vda1", "1 GiB", "vfat"),
        ("/dev/vda2", "40 GiB", "ext4"),
        ("/dev/vda3", "500 GiB", "ntfs"),
    )
    screen = FakeScreen(keys=[*down(3), "\n"], lines=30, columns=110)
    screens.reuse_screen(screen, config(), at)
    kept = at.layout.reused
    assert [one.selector for one in kept] == ["/dev/vda1", "/dev/vda2", "/dev/vda3"]
    assert not any(one.format for one in kept)
    # ntfs has no FilesystemType member: it is mounted and never created, so
    # the row reports the type as unrecognised rather than inventing one.
    assert kept[2].filesystem is None
    assert kept[1].filesystem is FilesystemType.EXT4


def test_reuse_on_a_disk_with_nothing_on_it_says_so() -> None:
    at = context()
    at.existing = ()
    screen = FakeScreen(keys=["\n"], lines=20)
    answer = screens.reuse_screen(screen, config(), at)
    assert answer.outcome is Outcome.BACK
    assert "no partitions" in "\n".join("\n".join(frame) for frame in screen.frames)


def test_the_reuse_layout_is_never_built_from_a_template() -> None:
    """A template has no existing partitions to name, so approximating one
    would silently install somewhere the operator did not choose."""
    from gentoo_install.errors import InvalidLayout
    from gentoo_install.model import templates

    with pytest.raises(InvalidLayout, match="operator's table"):
        templates.build(templates.Choice(disk="/dev/vda", layout=templates.Layout.REUSE))


def test_the_version_list_is_read_from_the_machine_and_not_held_here() -> None:
    """It moves every week, so a table in the source would be wrong by the next
    sync. A testing version is accepted for that atom alone."""
    at = context()
    at.kernel_versions = lambda atom: (("7.1.7", False), ("6.18.41", True))
    screen = FakeScreen(keys=["KEY_DOWN", "\n"], lines=20, columns=100)
    answer = screens.kernel_version_screen(screen, config(), at)
    assert answer.unwrap().kernel.version == "7.1.7"
    assert "~amd64" in screen.frames[0][3]
    assert "amd64" in "\n".join(screen.frames[0])


def test_a_medium_with_no_repository_asks_for_the_version_instead() -> None:
    """The official minimal ISO ships none, and a list nobody can populate is
    worse than a field."""
    at = context()
    at.kernel_versions = lambda atom: ()
    screen = FakeScreen(keys=[*"6.18.43", "\n"], lines=20, columns=100)
    answer = screens.kernel_version_screen(screen, config(), at)
    assert answer.unwrap().kernel.version == "6.18.43"


def test_pinning_a_version_pins_the_atom_and_opens_its_keyword() -> None:
    """Most versions are ~amd64 for their first weeks, so pinning one is
    normally pinning a testing version."""
    from gentoo_install.plan import kernel as plan_kernel

    pinned = replace(config(), kernel=replace(config().kernel, version="7.1.7"))
    described = [one.describe() for one in plan_kernel.build(pinned)]
    assert any("=sys-kernel/gentoo-kernel-7.1.7" in line for line in described)
    assert any("accept sys-kernel/gentoo-kernel-7.1.7 as testing" in line for line in described)
    loose = [one.describe() for one in plan_kernel.build(config())]
    assert not any("as testing" in line for line in loose)


def test_a_plain_switch_flips_where_it_stands() -> None:
    """A yes/no screen over a row that already reads `in use` asks the question
    the row just answered. FakeScreen has no keys: any screen would block."""
    at = context()
    start = config()
    blank = FakeScreen(keys=[], lines=20, columns=100)
    measured = screens._edit_mirror(blank, at, start, screens._MEASURE)
    assert measured is not None
    assert measured.portage.mirrors.speed_test is not start.portage.mirrors.speed_test

    files = screens._edit_mirror(blank, at, start, screens._DISTFILES)
    assert files is not None
    assert files.portage.mirrors.gentoo_distfiles is not start.portage.mirrors.gentoo_distfiles

    added = screens._edit_mirror(blank, at, start, "guru")
    assert added is not None
    assert "guru" in {one.name for one in added.portage.overlays}
    removed = screens._edit_mirror(blank, at, added, "guru")
    assert removed is not None
    assert "guru" not in {one.name for one in removed.portage.overlays}

    cron = screens.cron_screen(blank, start, at)
    assert cron.unwrap().system.cron is not start.system.cron
    assert blank.frames == []


def test_what_still_asks_before_it_changes() -> None:
    """The line between the two: a switch flips, and anything that destroys
    data, opens a second question, or starts the install asks first."""
    import inspect

    source = inspect.getsource(screens)
    asked = {
        "Format it, losing what is on it?",
        "This erases every partition on the disk.",
        "Encrypt the root filesystem?",
        "Encrypt this partition?",
        "Encrypt the pool?",
        "Give this account sudo?",
        "Use DHCP?",
        "Unlock the root over SSH from the initramfs?",
        "Install",
    }
    for title in asked:
        assert title in source, title
    # Eight call sites, nine titles: the slice screen words its question for a
    # pool or for a partition.
    assert source.count("Confirm(") == 8


def test_a_zfs_root_is_offered_no_kernel_the_module_will_not_build_for() -> None:
    """`sys-fs/zfs-2.4.3` carries MODULES_KERNEL_MAX=7.0, so a 7.1 kernel leaves
    the pool with no module and the machine with no root."""
    at = context()
    at.kernel_versions = lambda atom: (("7.1.7", False), ("6.18.43", False))
    at.zfs_kernel_max = "7.0"
    on_zfs = config(zfs_root())
    # No KEY_DOWN: with the ceiling applied the first row is already a version.
    screen = FakeScreen(keys=["\n"], lines=20, columns=100)
    answer = screens.kernel_version_screen(screen, on_zfs, at)
    assert answer.unwrap().kernel.version == "6.18.43"
    assert "7.1.7" not in "\n".join(screen.frames[0])
    # MODULES_KERNEL_MAX only warns, so an unpinned kernel would resolve to
    # 7.1.7 and the ceiling would buy nothing.
    assert "not pinned" not in "\n".join(screen.frames[0])

    # No pool: the ceiling is not this layout's business.
    plain = FakeScreen(keys=["KEY_DOWN", "\n"], lines=20, columns=100)
    assert screens.kernel_version_screen(plain, config(), at).unwrap().kernel.version == "7.1.7"


def test_the_unpinned_row_pins_nothing() -> None:
    at = context()
    at.kernel_versions = lambda atom: (("7.1.7", False),)
    pinned = replace(config(), kernel=replace(config().kernel, version="7.1.7"))
    answer = screens.kernel_version_screen(FakeScreen(keys=["\n"], lines=20), pinned, at)
    assert answer.unwrap().kernel.version == ""


def test_a_required_row_with_no_answer_is_drawn_red() -> None:
    """Asked for by a user reading the screen: the blocked Install row names
    one missing answer, and the rows themselves said nothing."""
    from gentoo_install.tui.widgets import Style

    at = context()
    blank = replace(config(), system=replace(config().system, root_password_hash=""))
    screen = FakeScreen(keys=["q", "KEY_DOWN", "\n"], lines=30, columns=100)
    run(screen, blank, at)
    red = [text for style, text in screen.styled if style is Style.REQUIRED]
    assert any("Root password" in one for one in red), red
    assert any("Mirrors" in one for one in red), red


def test_an_optional_row_never_opened_is_drawn_yellow() -> None:
    """It is running on a default nobody chose, which is worth seeing before
    the install rather than after it."""
    from gentoo_install.tui.widgets import Style

    at = context()
    screen = FakeScreen(keys=["q", "KEY_DOWN", "\n"], lines=30, columns=100)
    run(screen, config(), at)
    yellow = [text for style, text in screen.styled if style is Style.UNTOUCHED]
    assert any("Desktop" in one for one in yellow), yellow

    opened = context()
    opened.visited = {setting.key for setting in settings.SETTINGS}
    plain = FakeScreen(keys=["q", "KEY_DOWN", "\n"], lines=30, columns=100)
    run(plain, config(), opened)
    assert not [text for style, text in plain.styled if style is Style.UNTOUCHED]


def test_a_row_shown_and_not_chosen_is_never_marked_untouched() -> None:
    """Firmware is detected, so there is nothing for the operator to open and
    yellow would be asking for something that cannot be given."""
    from gentoo_install.tui.widgets import Style

    at = context()
    firmware = next(one for one in settings.SETTINGS if one.key == "firmware")
    assert settings.style_of(firmware, config(), at) is Style.PLAIN


def test_colour_repeats_what_the_text_already_says() -> None:
    """A console without colour has to lose nothing, so the value and the
    blocked row carry the same information."""
    at = context()
    blank = replace(config(), system=replace(config().system, root_password_hash=""))
    # Tall enough for the install row: it is the one carrying the reason, and
    # the menu scrolls once the list is longer than the screen.
    screen = FakeScreen(keys=["q", "KEY_DOWN", "\n"], lines=40, columns=100)
    run(screen, blank, at)
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "not set" in seen
    assert "still needs an answer" in seen


def test_choosing_a_disk_again_takes_back_the_erase_confirmation() -> None:
    """The operator typed the name of the disk they were looking at. Carrying
    that to another one unblocks the install for a disk nobody agreed to."""
    at = context()
    at.erase_confirmed = True
    screens.disk_screen(FakeScreen(keys=["KEY_DOWN", "\n"]), config(), at)
    assert not at.erase_confirmed
    assert settings.SETTINGS[row("Confirm erasing the drive")].value(config(), at) == "not set"


def test_swap_is_one_choice_and_not_two_that_accumulate() -> None:
    """Picking a partition and then zram left the operator with both, from a
    menu that presents them as alternatives."""
    at = context()
    from gentoo_install.model.device import Swap

    partition = screens.swap_screen(FakeScreen(keys=["KEY_DOWN", "\n"]), config(), at).unwrap()
    assert partition.system.zram is None
    assert partition.disk.graph.of_type(Swap)

    zram = screens.swap_screen(FakeScreen(keys=[*down(3), "\n"]), partition, at).unwrap()
    assert zram.system.zram is not None
    assert not zram.disk.graph.of_type(Swap)

    none = screens.swap_screen(FakeScreen(keys=["\n"]), zram, at).unwrap()
    assert none.system.zram is None and not none.disk.graph.of_type(Swap)


def test_backing_out_of_a_group_keeps_what_was_edited_inside_it() -> None:
    """Backspace is what the group's own footer calls Back, and it discarded
    every edit made behind that row."""
    at = context()
    rows = settings.DISK
    keys = [*down(5), "\n", *down(3), "\n", "\x7f"]
    answer = settings.nested("Disk", rows)(FakeScreen(keys=keys, lines=30), config(), at)
    assert answer.chosen


def test_a_layout_editor_that_backs_out_leaves_no_manual_table_behind() -> None:
    """The Layout and Partitions rows described a table the configuration did
    not contain, because the flag was set before the editor answered."""
    at = context()
    at.existing = ()
    at.manual = False
    # reuse on a disk with nothing on it: the screen reports and goes back.
    screens.layout_screen(FakeScreen(keys=[*down(5), "\n", "\n"], lines=30), config(), at)
    assert not at.manual


def test_opening_the_partitions_row_directly_marks_the_layout_manual() -> None:
    """It is reachable from the menu as well as from the Layout row, and the
    disk rebuild reads that flag."""
    at = context()
    at.manual = False
    at.layout = manual.suggest(at.choice.disk, at.firmware)
    screen = FakeScreen(keys=[*down(3), "\n"], lines=30, columns=100)
    screens.partitions_screen(screen, config(), at)
    assert at.manual


def test_a_profile_the_operator_picked_survives_choosing_a_desktop() -> None:
    """The desktop overwrote it regardless, throwing away no-multilib and
    anything else chosen on purpose."""
    at = context()
    # A fresh list each time: FakeScreen pops, so one list serves one screen.
    def plasma() -> list[str]:
        return [*down(4), "\n"]  # sorted: "", console, gnome, gnome-full, plasma

    fresh = screens.desktop_screen(FakeScreen(keys=plasma(), lines=30), config(), at).unwrap()
    assert fresh.packages.desktop == "plasma"
    assert fresh.portage.profile.endswith("desktop/plasma/systemd")

    chosen = replace(
        config(),
        portage=replace(config().portage, profile="default/linux/amd64/23.0/no-multilib/systemd"),
    )
    kept = screens.desktop_screen(FakeScreen(keys=plasma(), lines=30), chosen, at).unwrap()
    assert kept.packages.desktop == "plasma"
    assert kept.portage.profile == "default/linux/amd64/23.0/no-multilib/systemd"


def test_choosing_a_kernel_without_cjktty_turns_console_cjk_off_and_says_so() -> None:
    """The rule refused the install with a message about the kernel, and the
    only row that set console_cjk was the language screen before the menu."""
    from gentoo_install.model.config import KernelSource

    at = context()
    chinese = screens.with_language(config(), "zh-TW")
    assert chinese.system.console_cjk

    screen = FakeScreen(keys=["\n", "\n"], lines=30, columns=100)
    plain = screens.kernel_screen(screen, chinese, at).unwrap()
    assert plain.kernel.source is KernelSource.DIST_BIN
    assert not plain.system.console_cjk
    validate(plain)
    assert "cjktty" in "\n".join("\n".join(frame) for frame in screen.frames)


def test_the_patched_kernel_leaves_console_cjk_alone() -> None:
    at = context()
    chinese = screens.with_language(config(), "zh-CN")
    keys = [*down(2), "\n"]
    kept = screens.kernel_screen(FakeScreen(keys=keys, lines=30, columns=100), chinese, at).unwrap()
    assert kept.system.console_cjk


def test_choosing_the_patched_kernel_turns_its_patch_on() -> None:
    """`RequestCjkKernel` reads `system.console_cjk`, so picking
    `sys-kernel/gentoo-cjk-kernel` after answering English wrote `-cjk` and
    compiled the patch out of the package that exists for it."""
    from gentoo_install.model.config import KernelSource
    from gentoo_install.plan import kernel as plan_kernel

    at = context()
    english = screens.with_language(config(), "en")
    assert english.system.console_cjk is False

    keys = ["KEY_DOWN", "KEY_DOWN", "\n", "\n"]
    chosen = screens.kernel_screen(FakeScreen(keys=keys, lines=24, columns=100), english, at)
    picked = chosen.unwrap()
    assert picked.kernel.source is KernelSource.CJK
    assert picked.system.console_cjk is True
    assert any(
        "with cjk on" in one.describe() for one in plan_kernel.build(picked)
    )


def test_the_console_font_is_a_row_and_the_size_with_no_cjk_says_why() -> None:
    """It was settable only by hand-written TOML, and `compat.py` already
    carries a rule about it: 8x8 has no CJK glyphs, so a Chinese console picked
    from the menu could not reach the size that draws it."""
    from gentoo_install.model.config import ConsoleFontSize

    assert "console_font" in {one.key for group in settings.SETTINGS for one in group.rows}
    at = context()
    cjk = replace(config(), system=replace(config().system, console_cjk=True))
    screen = FakeScreen(keys=["q", "KEY_DOWN", "\n"], lines=24, columns=100)
    screens.console_font_screen(screen, cjk, at)
    drawn = screen.last
    assert "8x8" in drawn and "16x32" in drawn
    assert "cjk" in drawn.lower() or "CJK" in drawn

    chosen = screens.console_font_screen(
        FakeScreen(keys=["KEY_DOWN", "KEY_DOWN", "\n"], lines=24), config(), at
    )
    assert chosen.unwrap().system.console_font is ConsoleFontSize.SIZE_16X32


def test_the_overlay_address_is_held_in_one_place() -> None:
    """`_with_gentoo_zh` carried its own literal beside the table in
    `model/mirrors.py`, and the overlay has moved host once already."""
    from gentoo_install.model.config import GentooZhMirror
    from gentoo_install.model import mirrors

    for site in GentooZhMirror:
        chosen = replace(
            config(),
            portage=replace(
                config().portage,
                mirrors=replace(config().portage.mirrors, gentoo_zh=site),
            ),
        )
        added = screens._with_gentoo_zh(chosen).overlays[-1]
        assert added.name == "gentoo-zh"
        assert added.sync_uri == mirrors.gentoozh(site).git

    from pathlib import Path

    # `gig` keeps its address in `PLAIN_OVERLAYS`, which is the table for the
    # overlays that have no mirror; only gentoo-zh has one and belongs there.
    source = Path(screens.__file__).read_text()
    assert "gentoo-zh/overlay" not in source


def test_the_desktops_offered_are_the_ones_with_a_profile_file() -> None:
    """The list was a table beside `data/profiles/`, so a desktop added there
    never reached the menu and one added here installed nothing."""
    at = context()
    from pathlib import Path

    from gentoo_install import data

    offered = screens.desktop_profiles(at.groups)
    shipped = {path.stem for path in (Path(data.__file__).parent / "data/profiles").glob("*.toml")}
    # One row per file, plus the machine with no desktop at all.
    assert set(offered) == {"", *shipped}
    assert offered[""] == screens.BASE_PROFILE
    # Every desktop the menu shows can be built: the profile comes from its file.
    for name, path in offered.items():
        assert path.startswith("default/linux/amd64/23.0"), name


def test_a_required_row_inside_any_group_is_named_by_its_own_label() -> None:
    """`unanswered` walked a hand-written list of three groups and there are
    four, so a required row inside the fourth would have blocked the install
    from a row whose own label says nothing about what is missing."""
    from dataclasses import replace as _replace

    groups = [one for one in settings.SETTINGS if one.rows]
    assert len(groups) == 7
    # Every group row's members are reachable, and no group row is walked itself.
    at = context()
    blank = replace(config(), system=replace(config().system, root_password_hash=""))
    named = settings.unanswered(blank, at)
    assert "Root password" in named
    assert not {one.label for one in groups} & set(named)

    for group in groups:
        for row in group.rows:
            required = _replace(row, required=True, value=lambda c, x: settings.UNSET)
            walked = [
                one for parent in settings.SETTINGS for one in (parent.rows or (parent,))
            ]
            assert row in walked, f"{group.label}/{row.label}"
            assert required.label == row.label


def test_cancelling_out_of_a_group_hands_back_what_was_edited_in_it() -> None:
    """It returned a bare CANCELLED, so declining to leave dropped every answer
    already given inside that group and not only the screen backed out of."""
    from gentoo_install.model.config import Keywords

    at = context()
    # The menu steps over a row it cannot open, so the count is of the rows
    # that can be reached, not of the position in the tuple.
    reachable = [one for one in settings.COMPILER if one.edit is not None]
    keywords = next(n for n, one in enumerate(reachable) if one.label == "Package keywords")
    # Into the keywords row, pick ~amd64, then cancel out of the group menu.
    keys = [*down(keywords), "\n", "KEY_DOWN", "\n", "q"]
    opened = settings.nested("Compiler", settings.COMPILER)
    answer = opened(FakeScreen(keys=keys, lines=30, columns=100), config(), at)
    assert answer.outcome is Outcome.CANCELLED
    assert answer.value is not None
    assert answer.value.portage.keywords is Keywords.TESTING


def test_staying_after_a_cancel_keeps_the_answers_that_came_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`app.run` read a value only from a CHOSE answer, so the configuration a
    cancelled group handed back was thrown away at the main menu."""
    from gentoo_install.tui.widgets import Answer

    at = context()
    # Hostname rather than a value inside a group: `_summary` shows only the
    # first two rows of a group, so the change has to be one the menu draws.
    edited = replace(config(), system=replace(config().system, hostname="kept"))
    cancelled = Answer(Outcome.CANCELLED, edited)
    from gentoo_install.tui import app as tui_app

    index = row("Hostname")
    patched = replace(settings.SETTINGS[index], edit=lambda s, c, x: cancelled)
    replaced = (*settings.SETTINGS[:index], patched, *settings.SETTINGS[index + 1 :])
    monkeypatch.setattr(tui_app, "SETTINGS", replaced)
    # The menu steps over the one row it cannot open.
    reachable = [one for one in replaced if one.edit is not None]
    steps = next(n for n, one in enumerate(reachable) if one.label == "Hostname")
    # Open that row, answer No to leaving, then leave for real.
    keys = [*down(steps), "\n", "\n", "q", "KEY_DOWN", "\n"]
    screen = FakeScreen(keys=keys, lines=30, columns=100)
    run(screen, config(), at)
    assert "kept" in "\n".join(screen.frames[-3])


def test_a_bad_port_keeps_the_address_that_was_typed_beside_it() -> None:
    """The form dropped out to the menu, so an operator who mistyped the port
    retyped the address as well."""
    at = context()
    at.erase_confirmed = True
    with_key = replace(
        config(), system=replace(config().system, authorized_keys=(GOOD_KEY,))
    )
    # Yes to unlocking, then `abc` appended to the port and an address typed.
    # After the message the form comes back holding both, so deleting the three
    # bad characters is the whole correction.
    keys = [
        "KEY_DOWN", "\n",
        *"abc", "KEY_DOWN", *"192.0.2.5", "KEY_DOWN", "\n",
        "\n",
        "KEY_BACKSPACE", "KEY_BACKSPACE", "KEY_BACKSPACE", "KEY_DOWN", "KEY_DOWN", "\n",
    ]
    screen = FakeScreen(keys=keys, lines=30, columns=100)
    answer = screens.remote_unlock_screen(screen, with_key, at)
    unlock = answer.unwrap().kernel.remote_unlock
    assert unlock.port == 222
    assert unlock.address == "192.0.2.5"


def test_a_listed_key_says_that_enter_removes_it() -> None:
    """Enter was the only thing the row did, and a key fetched from a URL went
    without a word."""
    at = context()
    with_key = replace(
        config(), system=replace(config().system, authorized_keys=(GOOD_KEY,))
    )
    screen = FakeScreen(keys=["q"], lines=24, columns=100)
    screens.authorized_keys_screen(screen, with_key, at)
    assert "enter removes it" in screen.last


def test_a_phrase_that_is_not_the_disk_name_says_so_and_asks_again() -> None:
    """It stored False and returned to the menu, so a trailing space read as a
    refusal and the row went back to unset with nothing explaining why."""
    at = context()
    disk = at.choice.disk
    # A wrong name, the message, then the right one.
    keys = [*"/dev/sda", "\n", "\n", *disk, "\n"]
    screen = FakeScreen(keys=keys, lines=24, columns=80)
    answer = screens.erase_screen(screen, config(), at)
    assert answer.outcome is Outcome.CHOSE
    assert at.erase_confirmed is True
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "That is not the name of this disk." in seen


def test_the_erase_question_fits_eighty_columns() -> None:
    """The selector, three sentences and the field did not, and the clause that
    was cut is the one saying what to type."""
    at = context()
    # Backspace on an untouched field is what leaves it; `q` is a character.
    screen = FakeScreen(keys=["KEY_BACKSPACE"], lines=24, columns=80)
    screens.erase_screen(screen, config(), at)
    title = next(line for line in screen.frames[0] if line.strip())
    assert "Type the disk name to confirm." in title
    assert len(title.rstrip()) <= 80
    # The name to type is in the field instead of the title.
    assert at.choice.disk in "\n".join(screen.frames[0])


def test_the_address_row_says_what_the_machine_will_come_up_with() -> None:
    """Only the init's own manager reads the address fields, so a static
    address under NetworkManager was drawn as though it were in effect and
    nothing wrote it anywhere."""
    from gentoo_install.model.config import Networking

    at = context()
    address = next(
        one for group in settings.SETTINGS for one in group.rows if one.key == "address"
    )
    typed = replace(
        config().system, addresses=("192.0.2.10/24",), interface="eth0"
    )
    for chosen in Networking:
        shown = address.value(replace(config(), system=replace(typed, networking=chosen)), at)
        if chosen is Networking.BUILTIN:
            assert shown == "eth0: 192.0.2.10/24"
        else:
            assert "192.0.2.10" not in shown, chosen


def test_a_password_is_typed_twice_before_it_is_hashed() -> None:
    """The field is masked, so a typo is found out at the first login of a
    machine that has already been installed. The passphrase was checked this
    way and the two passwords were not."""
    at = context()
    # Two that differ, the message, then two that match.
    keys = [*"first", "\n", *"second", "\n", "\n", *"right", "\n", *"right", "\n"]
    answer = screens.root_password_screen(FakeScreen(keys=keys, lines=24), config(), at)
    assert answer.unwrap().system.root_password_hash == "$6$test$5"

    account = [*"zakk", "\n", *"one", "\n", *"two", "\n", "\n", *"same", "\n", *"same", "\n", "\n"]
    made = screens.user_screen(FakeScreen(keys=account, lines=24), config(), at)
    user = made.unwrap().system.users[0]
    assert user.name == "zakk" and user.password_hash == "$6$test$4"


def test_the_keyboard_layout_is_picked_from_what_the_machine_ships() -> None:
    """It was a text field, so a name `kbd` has no file for was accepted and
    loaded nothing, and the operator found out at the next boot."""
    at = context()
    at.keymaps = lambda: (("qwerty", "de"), ("qwerty", "us"), ("dvorak", "dvorak"))
    # The families are listed first: two hundred names do not fit a console.
    screen = FakeScreen(keys=["KEY_DOWN", "\n", "KEY_DOWN", "\n"], lines=24, columns=90)
    answer = screens.keymap_screen(screen, config(), at)
    assert answer.unwrap().system.keymap == "us"
    assert "qwerty" in "\n".join(screen.frames[0])
    assert "dvorak" in "\n".join(screen.frames[0])

    # A medium with no keymap tree falls back to typing the name.
    at.keymaps = lambda: ()
    # The field is prefilled with the current keymap, so `us` is cleared first.
    typed = FakeScreen(keys=["KEY_BACKSPACE", "KEY_BACKSPACE", *"fr", "\n"], lines=24, columns=90)
    assert screens.keymap_screen(typed, config(), at).unwrap().system.keymap == "fr"


def test_the_unlock_keyboard_offers_following_the_console() -> None:
    """Empty means the console's own keymap, which a list has to say rather
    than leaving the operator to guess that no row is the answer."""
    at = context()
    at.keymaps = lambda: (("qwerty", "de"), ("qwerty", "us"))
    screen = FakeScreen(keys=["\n"], lines=24, columns=90)
    answer = screens.initramfs_keymap_screen(screen, config(), at)
    assert answer.unwrap().system.keymap_initramfs == ""
    assert "the same as the console" in "\n".join(screen.frames[0])


def test_selecting_gentoo_zh_turns_its_binary_host_on() -> None:
    """The host serves what that overlay builds, and `compat.py` refuses the
    host without the overlay, so the two are one answer."""
    from gentoo_install.model.config import BinhostChannel

    assert config().portage.binhost.community is BinhostChannel.OFF
    added = screens._with_gentoo_zh(config())
    assert any(one.name == "gentoo-zh" for one in added.overlays)
    assert added.binhost.community is BinhostChannel.STABLE

    # A channel the operator already chose is left alone.
    picked = replace(
        config(),
        portage=replace(
            config().portage,
            binhost=replace(config().portage.binhost, community=BinhostChannel.UNSTABLE),
        ),
    )
    assert screens._with_gentoo_zh(picked).binhost.community is BinhostChannel.UNSTABLE


def test_a_grouped_row_uses_the_width_the_terminal_actually_has() -> None:
    """It took two values whatever the terminal was, so a 160-column ssh window
    still read `openrc, syslog-ng +1` with the rest of the line empty."""
    at = context()
    disk = next(one for one in settings.SETTINGS if one.key == "storage")

    at.columns = 80
    narrow = disk.value(config(), at)
    at.columns = 200
    wide = disk.value(config(), at)

    assert "+" in narrow
    assert "+" not in wide
    assert wide.count(",") == len(disk.rows) - 1
    # Narrow still fits: the count is what the line has room for.
    assert len(narrow) < 80


def test_the_rows_say_not_set_in_the_language_the_menu_is_in() -> None:
    """`UNSET` is the sentinel `style_of` compares against, so it reached the
    screen untranslated and one English line sat in a Chinese menu."""
    from gentoo_install.i18n import Catalog
    from gentoo_install.tui.app import _drawn

    at = context()
    at.translate = Catalog("zh-TW")
    blank = replace(config(), system=replace(config().system, root_password_hash=""))
    root = next(one for one in settings.SETTINGS if one.key == "root")
    assert root.value(blank, at) == settings.UNSET
    assert _drawn(root, blank, at) == at.translate(settings.UNSET) != settings.UNSET
