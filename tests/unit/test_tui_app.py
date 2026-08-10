from __future__ import annotations

from dataclasses import replace

import pytest

from gentoo_install.data import load_catalog
from gentoo_install.errors import ConfigError
from gentoo_install.i18n import Catalog
from gentoo_install.model.size import Size
from gentoo_install.model.config import (
    Bootloader,
    InitSystem,
    InstallConfig,
    Keywords,
    MirrorConfig,
    MirrorRegion,
)
from gentoo_install.model import manual
from gentoo_install.model.device import FilesystemType
from gentoo_install.model.validate import validate
from gentoo_install.model import compat
from gentoo_install.tui import screens, settings
from gentoo_install.tui.app import run
from gentoo_install.tui.widgets import Outcome

from .fake_screen import FakeScreen
from .layouts import config, encrypted_root, zfs_root

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
        # The rows that offer a share of memory have nothing to offer at zero,
        # which is not the machine any of these tests is about.
        memory=Size(16 * 1024**3),
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
    # The value alone beside the reason: the row said `detected` twice.
    assert any("uefi - detected" in "\n".join(frame) for frame in screen.frames)
    assert not any("(detected)" in "\n".join(frame) for frame in screen.frames)


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
    at.confirmed = {one.selector for one in compat.destroyed(config().disk.graph)}
    # A required row is answered when it has been opened: the mirror and the
    # disk start on a value read from this machine, and an install that erases
    # a drive nobody looked at is what the requirement exists to prevent.
    at.visited.update(
        one.key
        for group in settings.SETTINGS
        for one in (group.rows if any(r.required for r in group.rows) else (group,))
        if one.required
    )
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
    at.confirmed = {one.selector for one in compat.destroyed(config().disk.graph)}
    # A required row is answered when it has been opened: the mirror and the
    # disk start on a value read from this machine, and an install that erases
    # a drive nobody looked at is what the requirement exists to prevent.
    at.visited.update(
        one.key
        for group in settings.SETTINGS
        for one in (group.rows if any(r.required for r in group.rows) else (group,))
        if one.required
    )
    ready = replace(
        config(),
        system=replace(config().system, root_password_hash="$6$test$x"),
        portage=replace(config().portage, mirrors=replace(config().portage.mirrors, site="tuna")),
    )
    # Enter leaves the overview, then No, which returns to the menu.
    keys = [*down(len(settings.SETTINGS)), "\n", "\n", "\n", "q", "KEY_DOWN", "\n"]
    # Tall enough to hold the summary and the operations under it.
    screen = FakeScreen(keys=keys, lines=90, columns=100)
    finished = run(screen, ready, at)
    assert finished.cancelled
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    # Every row with its own label, then the operations the plan produced.
    assert "Root password  set" in seen
    assert "Bootloader  grub" in seen
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
    # Both menus open on what the configuration holds, `Asia/Shanghai`, so one
    # step down in the city list is the next Asian zone.
    screen = FakeScreen(keys=["\n", "KEY_DOWN", "\n"])
    answer = screens.timezone_screen(screen, config(), context())
    assert answer.outcome is Outcome.CHOSE
    assert answer.unwrap().system.timezone == "Asia/Taipei"


def test_reopening_the_timezone_and_accepting_keeps_it() -> None:
    """Two menus, and both used to start at their first row: reopening a
    configured timezone and pressing enter twice moved the machine to UTC."""
    answer = screens.timezone_screen(FakeScreen(keys=["\n", "\n"]), config(), context())
    assert answer.unwrap().system.timezone == "Asia/Shanghai"


def test_choosing_utc_needs_no_second_screen() -> None:
    """It has no area, so asking for a city after it would be an empty list."""
    from dataclasses import replace

    here = config()
    utc = replace(here, system=replace(here.system, timezone="UTC"))
    answer = screens.timezone_screen(FakeScreen(keys=["\n"]), utc, context())
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
    # Two, not three: the cursor starts on the row the configuration already
    # holds, and the default filesystem is xfs, which is the second row.
    zfs = ["KEY_DOWN", "KEY_DOWN", "\n"]
    screen = FakeScreen(keys=[*zfs, "\n"])
    answer = screens.layout_screen(screen, config(), context())
    assert answer.unwrap().bootloader.kind is Bootloader.ZFSBOOTMENU
    assert [o.name for o in answer.unwrap().portage.overlays] == ["gentoo-zh"]
    assert "gentoo-zh" in screen.last or "gentoo-zh" in "\n".join(
        "\n".join(frame) for frame in screen.frames
    )


def test_declining_the_overlay_leaves_zfs_on_systemd_boot() -> None:
    """The other bootloader a ZFS root can use, and it needs no overlay."""
    # Two, not three: the cursor starts on the row the configuration already
    # holds, and the default filesystem is xfs, which is the second row.
    zfs = ["KEY_DOWN", "KEY_DOWN", "\n"]
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


def test_the_passphrase_field_says_the_length_before_it_is_typed() -> None:
    """`The passphrase is too short.` arrives after the operator has typed one
    and says nothing about how long it has to be, so the second attempt is a
    guess. The field states the minimum above it, and states the number the
    code enforces rather than one written out beside it."""
    at = context()
    keys = ["KEY_DOWN", "\n", *list("longenough"), "\n", *list("longenough"), "\n"]
    screen = FakeScreen(keys=keys)
    screens.encryption_screen(screen, config(), at)
    drawn = "\n".join("\n".join(frame) for frame in screen.frames)
    assert str(screens.PASSPHRASE_MINIMUM) in drawn, drawn
    typed = [frame for frame in screen.frames if any("Passphrase" in one for one in frame)]
    assert typed, "no passphrase field was drawn"
    for frame in typed:
        assert any(str(screens.PASSPHRASE_MINIMUM) in one for one in frame), frame


def test_every_catalog_translates_the_passphrase_hint() -> None:
    """A hint that falls back to English on a Chinese console is a hint the
    operator it was written for cannot read."""
    import tomllib
    from pathlib import Path as _Path

    source = "At least {count} characters."
    for catalog in sorted(_Path("gentoo_install/data/locale").glob("*.toml")):
        said = tomllib.loads(catalog.read_text())["strings"]
        assert source in said, f"{catalog.name} has no hint"
        assert "{count}" in said[source], f"{catalog.name} dropped the placeholder"
        assert said[source] != source, f"{catalog.name} left it in English"


def test_declining_encryption_clears_the_passphrase() -> None:
    """`No` is one row up from where the cursor now starts: an encrypted
    layout opens on `Yes`, so pressing enter keeps what it had."""
    at = context()
    at.choice = replace(at.choice, passphrase_file="/run/keys/old")
    screens.encryption_screen(FakeScreen(keys=["KEY_UP", "\n"]), config(), at)
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


def test_the_answers_can_be_saved_to_a_file_on_the_way_out() -> None:
    """Leaving with nothing written throws away every answer, and the operator
    who wanted an unattended run has to type them all again."""
    at = context()
    written: list[tuple[str, str]] = []

    def save(config: InstallConfig, name: str) -> str:
        written.append((name, config.system.hostname))
        return f"/root/{name}"

    at.save_config = save
    # Escape, step to the third row, take the name the field offers.
    screen = FakeScreen(keys=["q", *down(2), "\n", "\n"])
    finished = run(screen, config(), at)
    assert finished.cancelled
    assert finished.saved == "/root/my-install.toml"
    assert written == [("my-install.toml", config().system.hostname)]


def test_a_name_that_cannot_be_written_is_asked_again() -> None:
    """The alternative is ending the run on a typo, with the answers gone."""
    at = context()
    refused = ["/proc/nope.toml"]

    def save(config: InstallConfig, name: str) -> str:
        if name in refused:
            raise ConfigError(f"cannot write {name}: Permission denied")
        return f"/root/{name}"

    at.save_config = save
    keys = ["q", *down(2), "\n", *"/proc/nope.toml", "\n", *"kept.toml", "\n"]
    screen = FakeScreen(keys=keys)
    finished = run(screen, config(), at)
    assert finished.saved == "/root/kept.toml"
    assert "Permission denied" in "\n".join("\n".join(frame) for frame in screen.frames)


def test_leaving_without_saving_writes_nothing() -> None:
    at = context()
    at.save_config = lambda config, name: pytest.fail("nothing asked for a file")
    assert run(FakeScreen(keys=["q", "KEY_DOWN", "\n"]), config(), at).saved == ""


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
    """Locale and timezone follow the language. The mirror region does not: a
    Taiwanese machine reading Chinese is not behind the Great Firewall and one
    in China reading English is, so that answer comes from the egress."""
    taiwan = screens.with_language(config(), "zh-TW")
    assert taiwan.system.timezone == "Asia/Taipei"
    assert taiwan.system.locale == "zh_TW.UTF-8"
    china = screens.with_language(config(), "zh-CN")
    assert china.system.timezone == "Asia/Shanghai"
    assert china.system.locale == "zh_CN.UTF-8"
    assert china.portage.mirrors.region is taiwan.portage.mirrors.region


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
        "Gentoo repository", "Gentoo binary packages", "gentoo-zh", "guru",
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
    named = [one.label for one in settings.unanswered(blank, context())]
    assert "Root password" in named
    assert "Drive" in named or config().disk.graph


def test_more_than_one_driver_can_be_ticked_and_not_in_the_application_list() -> None:
    """A hybrid machine has two adapters: an AMD laptop with an NVIDIA card
    needs `amdgpu radeonsi nvidia`, which one group cannot name. The drivers
    are still their own screen, so none of them is a row in Applications."""
    at = context()
    # Rows: intel, amdgpu, radeon, nouveau, nvidia, virtual-machine. Down to
    # amdgpu and tick, down to nvidia and tick, enter, yes to the confirmation.
    answer = screens.graphics_screen(
        FakeScreen(keys=[*down(1), " ", *down(3), " ", "\n", *down(1), "\n"], lines=30),
        config(),
        at,
    )
    chosen = answer.unwrap()
    assert chosen.packages.graphics == ("amdgpu", "nvidia")
    assert chosen.portage.video_cards == ("amdgpu", "radeonsi", "nvidia")
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
    # With a desktop chosen: a manager on its own is a login screen with no
    # session, so the rows are only offered once there is something to start.
    chosen = replace(config(), packages=replace(config().packages, desktop="plasma"))
    # sddm carries USE=sddm, so the choice confirms before it is taken.
    answer = screens.display_manager_screen(
        FakeScreen(keys=["KEY_DOWN", "\n", "KEY_DOWN", "\n"]), chosen, at
    )
    assert answer.unwrap().packages.display_manager == "sddm"
    assert "sddm" in answer.unwrap().portage.use


def test_a_login_screen_is_not_offered_without_a_desktop_to_start() -> None:
    at = context()
    screen = FakeScreen(keys=["q"], lines=24, columns=100)
    screens.display_manager_screen(screen, config(), at)
    drawn = "\n".join(screen.frames[0])
    assert "sddm  the one Plasma expects (+sddm) - choose a desktop first" in drawn
    # `none` stays selectable: it is the answer that needs no desktop.
    assert "none  a text console login" in drawn
    assert "console login - choose" not in drawn


def test_the_proprietary_driver_widens_the_licences_it_needs() -> None:
    """NVIDIA-2025 is in @BINARY-REDISTRIBUTABLE, so the default @FREE refuses
    it and the emerge dies an hour into the install."""
    from gentoo_install.plan import packages as plan_packages

    chosen = replace(config(), packages=replace(config().packages, graphics=("nvidia",)))
    assert "@BINARY-REDISTRIBUTABLE" in plan_packages.required_licenses(chosen, load_catalog())
    plain = replace(config(), packages=replace(config().packages, graphics=("nouveau",)))
    assert plan_packages.required_licenses(plain, load_catalog()) == ("@FREE",)


def test_the_nvidia_group_writes_no_file_of_its_own() -> None:
    """x11-drivers/nvidia-drivers ships /etc/modprobe.d/nvidia.conf itself and
    adds it to dracut's install_items. 595.84 removed the modeset line from it
    because the driver defaults to `nvidia-drm.modeset=1` regardless, so a
    drop-in of ours would set a default and a second file would collide."""
    nvidia = load_catalog()["nvidia"]
    assert nvidia.files == ()
    assert nvidia.video_cards == ("nvidia",)
    assert nvidia.package_use == ("x11-drivers/nvidia-drivers dist-kernel",)


def test_a_chinese_interface_defaults_to_the_patched_kernel() -> None:
    """cjktty is what puts CJK on the console, and it is in gentoo-zh and
    nowhere else, so the overlay is ticked with it rather than after it."""
    from gentoo_install.model.config import KernelSource

    for tag in ("zh-CN", "zh-TW"):
        seeded = screens.with_language(config(), tag)
        assert seeded.kernel.source is KernelSource.CJK_BIN, tag
        assert seeded.system.console_cjk, tag
        assert [one.name for one in seeded.portage.overlays] == ["gentoo-zh"], tag
        validate(seeded)


def test_every_cjk_catalog_takes_the_patched_kernel_and_english_does_not() -> None:
    """cjktty is what draws Chinese, Japanese and Korean on the console, so all
    four of those catalogs need it; English has nothing to gain from the
    overlay and is not defaulted into one."""
    from gentoo_install.model.config import KernelSource

    for tag in ("zh-TW", "zh-CN", "ja", "ko"):
        seeded = screens.with_language(config(), tag)
        assert seeded.kernel.source is KernelSource.CJK_BIN, tag
        assert [one.name for one in seeded.portage.overlays] == ["gentoo-zh"], tag

    english = screens.with_language(config(), "en")
    assert english.kernel.source is not KernelSource.CJK_BIN
    assert english.portage.overlays == ()


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
        detail = next(one.detail for one in rows if one.label == "Gentoo repository")
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


def test_the_table_lists_what_is_on_the_disk_and_keeps_it_by_default() -> None:
    """Every partition the operator leaves alone keeps its data, so `keep` is
    the row's default and everything else is a choice made on that row."""
    at = context()
    at.manual = True
    at.layout = manual.Layout()
    at.existing = (
        ("/dev/vda1", "1 GiB", "vfat"),
        ("/dev/vda2", "40 GiB", "ext4"),
        ("/dev/vda3", "500 GiB", "ntfs"),
    )
    screen = FakeScreen(keys=["q"], lines=30, columns=110)
    screens.partitions_screen(screen, config(), at)
    rows = at.layout.slices
    assert [one.selector for one in rows] == ["/dev/vda1", "/dev/vda2", "/dev/vda3"]
    assert all(one.status is manual.SliceStatus.KEEP for one in rows)
    # ntfs has no FilesystemType member: it is mounted and never created, so
    # the row reports the type as unrecognised rather than inventing one.
    assert rows[2].filesystem is None
    assert rows[1].filesystem is FilesystemType.EXT4


def test_an_empty_disk_opens_on_the_template_proposal() -> None:
    """There is nothing to list, and an empty table is not a starting point."""
    at = context()
    at.manual = True
    at.layout = manual.Layout()
    at.existing = ()
    screen = FakeScreen(keys=["q"], lines=20, columns=100)
    screens.partitions_screen(screen, config(), at)
    assert at.layout.slices
    assert all(one.status is manual.SliceStatus.CREATE for one in at.layout.slices)

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
        "This erases every partition on the disk.",
        "Encrypt the root filesystem?",
        "Encrypt this partition?",
        "Encrypt the pool?",
        "Encrypt this array?",
        "Use DHCP?",
        "Unlock the root over SSH from the initramfs?",
        "Install",
    }
    for title in asked:
        assert title in source, title
    # Seven call sites, eight titles: the slice screen words its question for a
    # pool or for a partition. sudo left the list when the account became one
    # form: a tick beside the other four answers, not a screen of its own.
    assert source.count("Confirm(") == 7
    # `settle` asks the same kind of question with three answers rather than
    # two, because the third opens the row the values land on.
    assert "This choice also sets" in source


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
    # Wide enough for the reason: at 100 columns the row is truncated to
    # `still needs an ans`, which is the behaviour under test being cut off.
    screen = FakeScreen(keys=["q", "KEY_DOWN", "\n"], lines=40, columns=130)
    run(screen, blank, at)
    seen = "\n".join("\n".join(frame) for frame in screen.frames)
    assert "required" in seen
    assert "still needs an answer" in seen
    # The legend names the colours in use, on the line that has room for it.
    assert "* required" in seen


def test_choosing_a_disk_again_takes_back_the_erase_confirmation() -> None:
    """The operator typed the name of the disk they were looking at. Carrying
    that to another one unblocks the install for a disk nobody agreed to."""
    at = context()
    at.confirmed = {one.selector for one in compat.destroyed(config().disk.graph)}
    screens.disk_screen(FakeScreen(keys=["KEY_DOWN", "\n"]), config(), at)
    assert not at.confirmed
    assert settings.SETTINGS[row("Confirm erasing the drive")].value(config(), at) == "not set"


def test_a_swap_partition_and_zram_are_two_rows_and_not_alternatives() -> None:
    """One list said the operator had to choose. A machine can hold a partition
    for hibernation and zram for the pressure it meets while running, so the
    two are separate rows and setting one leaves the other alone."""
    at = context()
    from gentoo_install.model.device import Swap

    partition = screens.swap_screen(FakeScreen(keys=["KEY_DOWN", "\n"]), config(), at).unwrap()
    assert partition.system.zram is None
    assert partition.disk.graph.of_type(Swap)

    both = screens.zram_screen(
        FakeScreen(keys=["KEY_DOWN", "\n"], lines=24), partition, at
    ).unwrap()
    assert both.system.zram is not None
    assert both.disk.graph.of_type(Swap), "choosing zram removed the partition"

    # `off` and `none` are one row up from where the cursor starts: each menu
    # opens on the value it holds, so enter alone keeps it.
    off = screens.zram_screen(FakeScreen(keys=["KEY_UP", "\n"], lines=24), both, at).unwrap()
    assert off.system.zram is None and off.disk.graph.of_type(Swap)

    none = screens.swap_screen(FakeScreen(keys=["KEY_UP", "\n"]), off, at).unwrap()
    assert not none.disk.graph.of_type(Swap)


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
    # Into the manual editor and straight back out of it.
    manual_row = next(
        n for n, one in enumerate(("ext4", "xfs", "btrfs", "zfs", "manual")) if one == "manual"
    )
    screens.layout_screen(
        FakeScreen(keys=[*down(manual_row), "\n", "q"], lines=30), config(), at
    )
    assert not at.manual


def test_opening_the_partitions_row_directly_marks_the_layout_manual() -> None:
    """It is reachable from the menu as well as from the Layout row, and the
    disk rebuild reads that flag."""
    at = context()
    at.manual = False
    at.layout = manual.suggest(at.choice.disk, at.firmware)
    done = [item.label for item in screens._partition_rows(at)].index("Done")
    screen = FakeScreen(keys=[*down(done), "\n"], lines=30, columns=100)
    screens.partitions_screen(screen, config(), at)
    assert at.manual


def test_a_profile_the_operator_picked_survives_choosing_a_desktop() -> None:
    """The desktop overwrote it regardless, throwing away no-multilib and
    anything else chosen on purpose."""
    at = context()
    # A fresh list each time: FakeScreen pops, so one list serves one screen.
    def plasma() -> list[str]:
        # sorted: "", console, gnome, gnome-full, plasma; then yes to the
        # confirmation naming the profile the desktop moves to.
        return [*down(4), "\n", *down(1), "\n"]

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

    # A Chinese interface holds `cjk-bin`, so the menu opens there and reaching
    # a kernel without the patch takes two steps up.
    screen = FakeScreen(keys=["KEY_UP", "KEY_UP", "\n", "\n"], lines=30, columns=100)
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

    # The menu opens on what the configuration holds, `dist-source`, so one
    # step down is the prebuilt patched kernel.
    keys = ["KEY_DOWN", "\n", "\n"]
    chosen = screens.kernel_screen(FakeScreen(keys=keys, lines=24, columns=100), english, at)
    picked = chosen.unwrap()
    assert picked.kernel.source is KernelSource.CJK_BIN
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
    assert len(groups) == 8
    # Every group row's members are reachable, and no group row is walked itself.
    at = context()
    blank = replace(config(), system=replace(config().system, root_password_hash=""))
    named = [one.label for one in settings.unanswered(blank, at)]
    assert "Root password" in named
    # A group is named only when it is required and nothing behind it is, so
    # `Disk` never stands in for the `Drive` row that is actually missing.
    carrying = {one.label for one in groups if any(r.required for r in one.rows)}
    assert not carrying & set(named)

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
    at.confirmed = {one.selector for one in compat.destroyed(config().disk.graph)}
    # An encrypted root as well as a key: with neither there is no passphrase
    # prompt to reach, and the screen says so instead of asking.
    with_key = replace(
        config(encrypted_root()), system=replace(config().system, authorized_keys=(GOOD_KEY,))
    )
    # Yes to unlocking, then `abc` appended to the port and an address typed.
    # After the message the form comes back holding both, so deleting the three
    # bad characters is the whole correction.
    keys = [
        "KEY_DOWN", "\n",
        *"abc", "KEY_DOWN", *"192.0.2.5/24", "KEY_DOWN", *"192.0.2.1",
        "KEY_DOWN", *"eth0", "KEY_DOWN", "\n",
        "\n",
        "KEY_BACKSPACE", "KEY_BACKSPACE", "KEY_BACKSPACE",
        "KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n",
    ]
    screen = FakeScreen(keys=keys, lines=30, columns=100)
    answer = screens.remote_unlock_screen(screen, with_key, at)
    unlock = answer.unwrap().kernel.remote_unlock
    assert unlock.port == 222
    # All three survived the message, not only the one beside the bad field.
    assert (unlock.address, unlock.gateway, unlock.interface) == (
        "192.0.2.5/24", "192.0.2.1", "eth0"
    )


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
    # The selector the graph will destroy, which is what the screen now names.
    disk = compat.destroyed(config().disk.graph)[0].selector
    # A wrong name, the message, then the right one.
    keys = [*"/dev/sda", "\n", "\n", *disk, "\n"]
    screen = FakeScreen(keys=keys, lines=24, columns=80)
    answer = screens.erase_screen(screen, config(), at)
    assert answer.outcome is Outcome.CHOSE
    assert at.confirmed
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
    # The name to type is in the field instead of the title, and it is the
    # short one: the configuration keeps the selector, the screen does not.
    named = at.shown_as(compat.destroyed(config().disk.graph)[0].selector)
    assert named in "\n".join(screen.frames[0])


def test_the_address_row_says_what_the_machine_will_come_up_with() -> None:
    """`WriteNetworkConfig` writes a mode-0600 NetworkManager keyfile from the
    same fields, so answering with the manager's name hid an address the
    install was about to configure."""
    from gentoo_install.model.config import Networking

    at = context()
    address = next(
        one for group in settings.SETTINGS for one in group.rows if one.key == "address"
    )
    typed = replace(
        config().system,
        addresses=("192.0.2.10/24",),
        interface="eth0",
        gateways=("192.0.2.1",),
        dns=("192.0.2.53",),
    )
    for chosen in Networking:
        shown = address.value(replace(config(), system=replace(typed, networking=chosen)), at)
        if chosen is Networking.NONE:
            assert shown == at.translate("no networking")
            continue
        assert "192.0.2.10/24" in shown, (chosen, shown)
        assert "192.0.2.1" in shown, (chosen, shown)
        assert "192.0.2.53" in shown, (chosen, shown)

    # DHCP says so rather than showing an empty list.
    dhcp = replace(config(), system=replace(config().system, networking=Networking.BUILTIN))
    assert address.value(dhcp, at) == "DHCP"

def test_a_password_is_typed_twice_before_it_is_hashed() -> None:
    """The field is masked, so a typo is found out at the first login of a
    machine that has already been installed. The passphrase was checked this
    way and the two passwords were not."""
    at = context()
    # Two that differ, the message, then two that match.
    keys = [*"first", "\n", *"second", "\n", "\n", *"right", "\n", *"right", "\n"]
    answer = screens.root_password_screen(FakeScreen(keys=keys, lines=24), config(), at)
    assert answer.unwrap().system.root_password_hash == "$6$test$5"

    # One form: the two passwords differ, so it redraws with a message and the
    # name is still there. Down to the second field, fix it, then Done.
    account = [
        *"zakk", "KEY_DOWN", *"one", "KEY_DOWN", *"two", "KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n",
        "KEY_DOWN", *"same", "KEY_DOWN", *"same", "KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n",
    ]
    made = screens.user_screen(FakeScreen(keys=account, lines=24, columns=100), config(), at)
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

    # Five of the seven disk rows answer with nothing, and a summary made of
    # those reads as a string of words with no subject.
    quiet = {at.translate(one) for one in settings.QUIET}
    said = [one for one in (row.value(config(), at) for row in disk.rows) if one not in quiet]
    assert len(said) < len(disk.rows)

    at.columns = 200
    wide = disk.value(config(), at)
    assert "+" not in wide
    assert wide.count(",") == len(said) - 1

    at.columns = 40
    narrow = disk.value(config(), at)
    assert "+" in narrow and len(narrow) < 40


def test_the_rows_say_not_set_in_the_language_the_menu_is_in() -> None:
    """`UNSET` is the sentinel `style_of` compares against, so it reached the
    screen untranslated and one English line sat in a Chinese menu."""
    from gentoo_install.i18n import Catalog
    from gentoo_install.tui.app import _drawn

    at = context()
    at.translate = Catalog("zh-TW")
    blank = replace(config(), system=replace(config().system, root_password_hash=""))
    # An optional row, because a required one says `required` instead: both
    # are drawn red and `not set` reads as a state that can be left alone.
    use = next(one for one in settings.COMPILER if one.key == "use")
    assert use.value(blank, at) == settings.UNSET
    assert _drawn(use, blank, at) == at.translate(settings.UNSET) != settings.UNSET
    root = next(one for one in settings.SETTINGS if one.key == "root")
    assert root.value(blank, at) == settings.UNSET
    assert _drawn(root, blank, at) == at.translate("required")


def test_a_detected_row_has_to_be_opened_and_not_only_filled_in() -> None:
    """The mirror and the disk start on a value read from this machine, so a
    check for `UNSET` alone let an install erase a drive nobody looked at.

    Only those rows. A row with no detected default is answered by its value:
    requiring a visit there made the Install row say `root password: still
    needs an answer` beside a row that read `set`.
    """
    at = context()
    at.confirmed = {one.selector for one in compat.destroyed(config().disk.graph)}
    ready = replace(
        config(),
        system=replace(config().system, root_password_hash="$6$test$x"),
        portage=replace(config().portage, mirrors=replace(config().portage.mirrors, site="tuna")),
    )
    # The group itself counts when nothing behind it is required: `Compiler`
    # has a usable value on every row and still has to be looked at.
    required = [
        one
        for group in settings.SETTINGS
        for one in (group.rows if any(r.required for r in group.rows) else (group,))
        if one.required
    ]
    detected = [one for one in required if one.detected]
    named = settings.unanswered(ready, at)
    assert {one.label for one in detected} <= {one.label for one in named}
    assert "Root password" not in {one.label for one in named}
    at.visited.update(one.key for one in required)
    assert settings.unanswered(ready, at) == ()


def test_the_timezone_can_follow_the_machine_the_installer_is_running_on() -> None:
    """A live medium sets `/etc/localtime` from the firmware clock and its own
    default, and the operator is standing next to the machine, so that guess is
    worth one row rather than six hundred."""
    at = context()
    at.timezone_here = "Australia/Melbourne"
    screen = FakeScreen(keys=["\n"], lines=24, columns=90)
    answer = screens.timezone_screen(screen, config(), at)
    assert answer.unwrap().system.timezone == "Australia/Melbourne"
    drawn = "\n".join(screen.frames[0])
    assert "follow the BIOS" in drawn and "Australia/Melbourne" in drawn

    # Unreadable on this medium: the row is absent rather than empty, and the
    # area menu opens on the configured zone instead.
    at.timezone_here = ""
    plain = FakeScreen(keys=["\n", "\n"], lines=24, columns=90)
    kept = screens.timezone_screen(plain, config(), at).unwrap()
    assert kept.system.timezone == "Asia/Shanghai"
    assert "follow the BIOS" not in "\n".join(plain.frames[0])


def test_the_cpu_flags_row_offers_the_baseline_as_well_as_this_machine() -> None:
    """The detected list builds for the CPU in front of the operator and for no
    other, which is wrong for an image, for a disk moved to another machine,
    and for anything a binary host built against the baseline."""
    at = context()
    at.cpu_flags = ("avx2", "aes")
    row = next(one for group in settings.SETTINGS for one in group.rows if one.key == "cpu_flags")
    assert row.edit is not None

    detected = screens.cpu_flags_screen(
        FakeScreen(keys=["\n"], lines=20, columns=96), config(), at
    )
    assert detected.unwrap().portage.cpu_flags == ("avx2", "aes")

    baseline = screens.cpu_flags_screen(
        FakeScreen(keys=["KEY_DOWN", "\n"], lines=20, columns=96), config(), at
    )
    assert baseline.unwrap().portage.cpu_flags == ()


def test_an_overlay_only_application_cannot_be_ticked_without_its_overlay() -> None:
    """It raised `ConfigError` out of `plan.build`, which `_blocked` did not
    catch, so Install stayed enabled and pressing it left curses with a
    traceback and every answer gone."""
    at = context()
    plain = config()
    assert not plain.portage.overlays
    screen = FakeScreen(keys=["q"], lines=40, columns=110)
    screens.packages_screen(screen, plain, at)
    drawn = "\n".join(screen.frames[0])
    assert "wechat" in drawn
    assert "needs the overlay gentoo-zh" in drawn


def test_the_install_row_says_why_a_plan_that_cannot_be_built_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace as replaced

    from gentoo_install.tui import app as tui_app

    at = context()
    wanted = replaced(
        config(), packages=replace(config().packages, applications=("wechat",))
    )
    # The unanswered rows are reported first and would hide this; the branch
    # under test is the one after them.
    monkeypatch.setattr(tui_app, "unanswered", lambda config, context: ())
    assert "gentoo-zh" in tui_app._blocked(wanted, at)


def test_the_overview_says_so_rather_than_leaving_curses_with_a_traceback() -> None:
    """Reaching it is a defect, since the row above is disabled for the same
    reason; a message beats losing the session."""
    from dataclasses import replace as replaced

    at = context()
    wanted = replaced(
        config(), packages=replace(config().packages, applications=("wechat",))
    )
    screen = FakeScreen(keys=["\n"], lines=30, columns=110)
    answer = screens.overview_screen(screen, wanted, at)
    assert answer.outcome is Outcome.CANCELLED
    assert "gentoo-zh" in "\n".join(screen.frames[0])


def test_one_unrelated_problem_does_not_grey_out_the_whole_bootloader_screen() -> None:
    """`compat.violations` reports every rule the configuration breaks, so a
    remote unlock with no ssh key disabled all three bootloaders and named a
    reason that belongs to another row."""
    from dataclasses import replace as replaced

    from gentoo_install.model.config import KernelConfig, RemoteUnlock

    at = context()
    from .layouts import zfs_root

    encrypted = config(zfs_root())
    unreachable = replaced(
        encrypted, kernel=KernelConfig(remote_unlock=RemoteUnlock(enabled=True))
    )
    screen = FakeScreen(keys=["q"], lines=24, columns=110)
    screens.bootloader_screen(screen, unreachable, at)
    drawn = "\n".join(screen.frames[0])
    # The unlock rule is broken whichever bootloader is chosen, so it belongs
    # to none of them; the ZFS rule belongs to GRUB alone and still shows.
    assert "authorized_keys" not in drawn
    assert "zfsbootmenu" in drawn


def test_remote_unlock_is_refused_without_a_key_and_without_encryption() -> None:
    """Its own docstring said so and the body never looked; both rules only
    surfaced at the Install row, six screens away from the answer."""
    at = context()
    for wanted, expected in (
        (config(encrypted_root()), "authorized_keys"),
        (
            replace(config(), system=replace(config().system, authorized_keys=(GOOD_KEY,))),
            "not encrypted",
        ),
    ):
        screen = FakeScreen(keys=["\n"], lines=24, columns=110)
        answer = screens.remote_unlock_screen(screen, wanted, at)
        assert answer.outcome is Outcome.BACK
        assert expected in "\n".join(screen.frames[0])


def test_remote_unlock_is_offered_once_both_hold() -> None:
    at = context()
    both = replace(
        config(encrypted_root()),
        system=replace(config().system, authorized_keys=(GOOD_KEY,)),
    )
    screen = FakeScreen(keys=["q"], lines=24, columns=110)
    screens.remote_unlock_screen(screen, both, at)
    assert "Unlock the root over SSH" in "\n".join(screen.frames[0])


def test_the_main_menu_reads_the_same_precondition_the_nested_one_does() -> None:
    """`nested()` consults `Setting.unavailable` when drawing and again before
    dispatching; the top-level loop read it in neither place, so every fix that
    added a reason to a top-level row would have done nothing."""
    from dataclasses import replace as replaced

    from gentoo_install.tui import app as tui_app
    from gentoo_install.tui import settings as tui_settings

    at = context()
    blocked = tuple(
        replaced(one, unavailable=lambda config, context: "not on this machine")
        if one.key == "kernel"
        else one
        for one in tui_settings.SETTINGS
    )
    screen = FakeScreen(keys=["q", "KEY_DOWN", "\n"], lines=40, columns=110)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tui_app, "SETTINGS", blocked)
        tui_app.run(screen, config(), at)
    assert "not on this machine" in "\n".join(screen.frames[0])

def test_the_address_row_says_something_the_manager_row_did_not() -> None:
    """`Network` drew `networkmanager-wpa, networkmanager-wpa`: the address row
    answered with the manager's name, which the row beside it already carried,
    so the group summary spent both its slots on one fact."""
    from dataclasses import replace

    from gentoo_install.model.config import Networking, SystemConfig

    for manager in Networking:
        installation = replace(config(), system=SystemConfig(networking=manager))
        named = settings.shown_value(settings.NETWORK[0], installation, context())
        address = settings.shown_value(settings.NETWORK[1], installation, context())
        assert named != address, manager


def test_reopening_a_setting_and_accepting_keeps_what_it_held() -> None:
    """Press enter on a selector without navigating and the configuration must
    come back unchanged. Most menus started on their first row, so reopening
    turned encryption off, moved the root to the first disk and set root SSH
    login to allowed."""
    from dataclasses import replace

    from gentoo_install.model.config import (
        Bootloader,
        BootloaderConfig,
        ConsoleFontSize,
        InitSystem,
        Keywords,
        KernelConfig,
        KernelSource,
        MirrorRegion,
        Networking,
        RemoteUnlock,
        SystemConfig,
    )

    at = context()
    base = config()
    # One non-default value per setting the menu can reach. The first row of
    # each of these menus is a different value, so an ignored current value
    # shows up as a change.
    held = replace(
        base,
        system=replace(
            base.system,
            init=InitSystem.OPENRC,
            console_font=ConsoleFontSize.SIZE_8X8,
            networking=Networking.NETWORKMANAGER_IWD,
            sshd=True,
            sshd_root_login=True,
        ),
        kernel=replace(
            KernelConfig(),
            source=KernelSource.CJK_BIN,
            remote_unlock=RemoteUnlock(enabled=True, interface="eth0"),
        ),
        bootloader=BootloaderConfig(kind=Bootloader.SYSTEMD_BOOT, firmware=base.bootloader.firmware),
        # The profile follows the init, and an inconsistent pair would make the
        # profile screen change it for a reason that is not this one.
        portage=replace(
            base.portage, keywords=Keywords.TESTING, profile="default/linux/amd64/23.0"
        ),
    )
    # Three rows answer a different question and are not selectors:
    # `Cron` flips where it stands, `Drive` reads `context.choice` rather than
    # the configuration, and `Bootloader` cannot keep a kind the layout forbids.
    flipped = {"Cron", "Drive", "Bootloader"}
    # Four screens enter is not an answer to, named rather than caught: an
    # `except Exception: continue` around the call meant every screen could
    # raise on enter and this test still passed with an empty list.
    # `Mirrors` and `SSH public keys` toggle entries and leave on cancel,
    # `Partitions` is a table editor, and `Confirm erasing the drive` wants
    # the disk name typed.
    not_an_answer = {"Mirrors", "Partitions", "SSH public keys", "Confirm erasing the drive"}
    wrong: list[str] = []
    opened: list[str] = []
    reachable = [
        one
        for group in settings.SETTINGS
        for one in (group.rows or (group,))
        if one.edit is not None and not one.rows
    ]
    for setting in reachable:
        if setting.label in flipped or setting.label in not_an_answer:
            continue
        edit = setting.edit
        assert edit is not None
        # Enough for the longest of these: `User account` asks for a name and
        # a password twice. A screen that wants more asks for a key the test
        # did not supply and fails, which is what the caught exception hid.
        screen = FakeScreen(keys=["\n"] * 8, lines=40, columns=120)
        answer = edit(screen, held, at)
        opened.append(setting.label)
        if not answer.chosen:
            continue
        # The row's own value, not the whole configuration: the locale screen
        # deliberately rewrites `system.locales` whichever entry is chosen.
        after = answer.unwrap()
        if setting.value(after, at) != setting.value(held, at):
            wrong.append(setting.label)
    assert not wrong, wrong
    assert len(opened) > 5, opened


def test_confirming_one_disk_does_not_authorise_a_second() -> None:
    """`erase_confirmed` was a single flag and the prompt named
    `context.choice.disk`, so a second disk added in manual partitioning was
    destroyed under a confirmation that never mentioned it."""
    from dataclasses import replace
    from pathlib import PurePosixPath

    from gentoo_install.model import compat
    from gentoo_install.model.config import DiskConfig
    from gentoo_install.model.device import (
        DeviceGraph,
        DeviceId,
        Existing,
        Filesystem,
        FilesystemType,
        Mountpoint,
        Partition,
        PartitionRole,
        PartitionTable,
        TableType,
    )
    from gentoo_install.model.size import Size

    nodes = [
        Existing(id=DeviceId("first"), selector="/dev/disk/by-id/one", wipe=True),
        PartitionTable(id=DeviceId("t1"), disk=DeviceId("first"), table=TableType.GPT),
        Partition(
            id=DeviceId("p1"),
            table=DeviceId("t1"),
            index=1,
            role=PartitionRole.DATA,
            size=Size(8 * 1024**3),
        ),
        Filesystem(id=DeviceId("fs1"), device=DeviceId("p1"), kind=FilesystemType.EXT4),
        Mountpoint(id=DeviceId("root"), source=DeviceId("fs1"), path=PurePosixPath("/")),
        Existing(id=DeviceId("second"), selector="/dev/disk/by-id/two", wipe=True),
        PartitionTable(id=DeviceId("t2"), disk=DeviceId("second"), table=TableType.GPT),
    ]
    two = replace(
        config(), disk=DiskConfig(graph=DeviceGraph.build(nodes), root=DeviceId("root"))
    )
    at = context()
    named = [one.selector for one in compat.destroyed(two.disk.graph)]
    assert set(named) == {"/dev/disk/by-id/one", "/dev/disk/by-id/two"}

    at.confirmed = {"/dev/disk/by-id/one"}
    row = next(one for one in settings.SETTINGS if one.key == "erase")
    assert settings.shown_value(row, two, at) != at.translate("confirmed")

    at.confirmed = set(named)
    assert settings.shown_value(row, two, at) == at.translate("confirmed")


def test_the_zfs_bootloader_prompt_returns_only_installable_answers() -> None:
    """It changed the bootloader and left the esp at `/efi`, so choosing
    systemd-boot returned a configuration `validate` refuses -- from the very
    screen that offered it. The old test asserted the enum and the overlay and
    never validated."""
    from gentoo_install.model.validate import validate

    at = context()
    start = config(zfs_root())
    for keys in (["\n"], ["KEY_DOWN", "\n"]):
        answered = screens._zfs_bootloader(
            FakeScreen(keys=keys, lines=24, columns=100), start, at
        )
        assert answered is not None, "neither key cancels"
        validate(answered)


def test_the_erase_field_is_visibly_empty_before_anything_is_typed() -> None:
    """The selector was drawn inside the box as a placeholder, where it is
    indistinguishable from a value already entered: an operator pressed enter
    on what looked like a filled field and was told it was the wrong name."""
    from gentoo_install.model import compat

    at = context()
    at.confirmed.clear()
    screen = FakeScreen(keys=["KEY_BACKSPACE"], lines=24, columns=100)
    screens.erase_screen(screen, config(), at)
    drawn = [line for line in screen.frames[0] if line.strip()]
    named = at.shown_as(compat.destroyed(config().disk.graph)[0].selector)
    # The name to type is on its own line, and the field holds only the caret.
    assert any(line.strip() == named for line in drawn), drawn
    field = next(line for line in drawn if line.lstrip().startswith("["))
    assert field.split("[", 1)[1].strip(" ]") == "_", field


def test_the_kernel_name_confirms_the_same_disk() -> None:
    """The installer renames a disk to its `/dev/disk/by-id/` form, and an
    operator reading `lsblk` types `/dev/sda`. Refusing it told them the name
    of their own disk was wrong."""
    from gentoo_install.model import compat

    at = context()
    at.confirmed.clear()
    named = compat.destroyed(config().disk.graph)[0].selector
    at.names_for = lambda selector: (selector, "/dev/sda", "sda")
    for typed in ("/dev/sda", "sda"):
        at.confirmed.clear()
        answer = screens.erase_screen(
            FakeScreen(keys=[*typed, "\n"], lines=24, columns=100), config(), at
        )
        assert answer.outcome is Outcome.CHOSE, typed
        assert at.confirmed == {named}, typed


def test_the_short_form_of_a_selector_confirms_the_same_disk() -> None:
    """A `/dev/disk/by-id/` selector is sixty characters read off the screen,
    and its last component names the same disk."""
    from gentoo_install.model import compat

    at = context()
    at.confirmed.clear()
    named = compat.destroyed(config().disk.graph)[0].selector
    short = named.rsplit("/", 1)[-1]
    answer = screens.erase_screen(
        FakeScreen(keys=[*short, "\n"], lines=24, columns=100), config(), at
    )
    assert answer.outcome is Outcome.CHOSE
    assert at.confirmed == {named}


def test_a_disk_is_shown_by_its_kernel_name_and_stored_by_its_selector() -> None:
    """The configuration keeps `/dev/disk/by-id/...` because a kernel name is
    assigned at probe time and one saved today installs somewhere else after
    the next reboot. Nobody reads sixty characters of it: `lsblk` says
    `/dev/sda` and so should every screen."""
    from dataclasses import replace

    from gentoo_install.model.config import DiskConfig
    from gentoo_install.model.device import DeviceGraph, Existing

    from .layouts import ext4_on_gpt, i

    named = "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi0"
    nodes = [
        replace(one, selector=named) if isinstance(one, Existing) else one
        for one in ext4_on_gpt()
    ]
    installation = replace(
        config(), disk=DiskConfig(graph=DeviceGraph.build(nodes), root=i("mnt-root"))
    )
    at = context()
    at.names_for = lambda selector: (selector, "/dev/sda", selector.rsplit("/", 1)[-1], "sda")

    row = next(
        one
        for group in settings.SETTINGS
        for one in (group.rows or (group,))
        if one.key == "disk"
    )
    assert settings.shown_value(row, installation, at) == "/dev/sda"

    at.confirmed.clear()
    screen = FakeScreen(keys=["KEY_BACKSPACE"], lines=24, columns=100)
    screens.erase_screen(screen, installation, at)
    drawn = [line.strip() for line in screen.frames[0] if line.strip()]
    assert "/dev/sda" in drawn, drawn
    assert named not in drawn, drawn

    # And the configuration still holds the stable one.
    assert [one.selector for one in installation.disk.graph.of_type(Existing)] == [named]


def test_no_screen_prints_the_by_id_selector() -> None:
    """The selector is what the configuration stores, not what a person reads:
    sixty characters of `scsi-0QEMU_QEMU_HARDDISK_drive-scsi0` said nothing
    `lsblk` does not say in eight."""
    from dataclasses import replace

    from gentoo_install.model.config import DiskConfig
    from gentoo_install.model.device import DeviceGraph, Existing

    from .layouts import ext4_on_gpt, i

    named = "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi0"
    nodes = [
        replace(one, selector=named) if isinstance(one, Existing) else one
        for one in ext4_on_gpt()
    ]
    installation = replace(
        config(), disk=DiskConfig(graph=DeviceGraph.build(nodes), root=i("mnt-root"))
    )
    at = context()
    at.disks = [(named, "32G QEMU HARDDISK")]
    at.choice = replace(at.choice, disk=named)
    at.names_for = lambda selector: (selector, "/dev/sda", selector.rsplit("/", 1)[-1], "sda")

    for name, keys in (
        ("erase", ["KEY_BACKSPACE"]),
        ("disks", ["q"]),
    ):
        screen = FakeScreen(keys=keys, lines=30, columns=120)
        at.confirmed.clear()
        if name == "erase":
            screens.erase_screen(screen, installation, at)
        else:
            screens.disk_screen(screen, installation, at)
        drawn = "\n".join("\n".join(frame) for frame in screen.frames)
        assert named not in drawn, (name, drawn)
        assert "/dev/sda" in drawn, (name, drawn)


def test_a_filled_row_is_not_still_asked_for() -> None:
    """The Install row said `root password: still needs an answer` beside a row
    reading `set`. A visit is required only of a row that starts on a value
    read from this machine, because that is the one an operator can accept
    without having looked at it."""
    from dataclasses import replace

    from gentoo_install.tui import app, settings

    from .fake_screen import FakeScreen
    from .layouts import config, ext4_on_gpt

    at = context()
    at.columns = 200
    base = config(ext4_on_gpt())
    empty = replace(base, system=replace(base.system, root_password_hash="", users=()))
    assert "Root password" in app._blocked(empty, at)

    row = next(one for one in settings.SETTINGS if one.key == "root")
    assert row.edit is not None
    typed = list("hunter2") + ["\n"] + list("hunter2") + ["\n"]
    # Deliberately not marking it visited: the value is the answer.
    done = row.edit(FakeScreen(keys=typed, lines=24), empty, at).unwrap()
    assert settings._root(done, at) == "set"
    assert "Root password" not in app._blocked(done, at)


def test_a_detected_row_still_has_to_be_opened() -> None:
    """The mirror and the drive both start on something read from the machine,
    and an install that erases a drive nobody looked at is what the
    requirement exists to prevent."""
    from gentoo_install.tui import app, settings

    from .layouts import config, ext4_on_gpt

    at = context()
    at.columns = 200
    whole = config(ext4_on_gpt())
    for key in ("mirror", "storage", "compiler"):
        row = next(one for one in settings.SETTINGS if one.key == key)
        assert row.detected, key
        assert not settings.settled(row, whole, at), key
    # `Disk` is a group whose own rows carry the requirement, so the row
    # behind it is what gets named.
    said = app._blocked(whole, at)
    for label in ("Mirrors", "Drive", "Compiler"):
        assert label in said, said


def test_a_reopened_selector_starts_on_what_is_already_set() -> None:
    """`Menu.current` exists because without it the first row wins: its own
    comment records encryption enabled becoming disabled. These selectors were
    built without it, so reopening the row and pressing enter without
    navigating answered with the first item rather than what was set."""
    import ast
    from pathlib import Path

    #: Every one holds a value the configuration already carries. A prompt that
    #: creates something, or asks a question with no prior answer, is not here.
    WANTED: frozenset[str] = frozenset(
        {
            "Init system",
            "Region",
            "Gentoo mirror",
            "Console font",
            "A ZFS root cannot boot from GRUB. Which bootloader?",
        }
    )
    tree = ast.parse(Path("gentoo_install/tui/screens.py").read_text())
    seen: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Menu":
            continue
        title = ""
        for one in node.keywords:
            if one.arg == "title" and isinstance(one.value, ast.Call) and one.value.args:
                argument = one.value.args[0]
                if isinstance(argument, ast.Constant):
                    title = str(argument.value)
        if title in WANTED:
            seen[title] = any(one.arg == "current" for one in node.keywords)
    assert set(seen) == WANTED, f"a selector was renamed or removed: {sorted(seen)}"
    assert all(seen.values()), sorted(one for one, has in seen.items() if not has)


def test_cancelling_the_zfs_bootloader_question_undoes_the_layout() -> None:
    """The layout is written and the graph rebuilt before the question is
    asked, so cancelling it committed a ZFS root with GRUB — a combination
    `model/compat.py` refuses and no later screen would have offered a way
    out of."""
    from gentoo_install.model.config import Bootloader
    from gentoo_install.model.device import ZfsPool
    from gentoo_install.tui.widgets import Outcome as Answered

    from .layouts import ext4_on_gpt

    at = context()
    start = config(ext4_on_gpt())
    assert not list(start.disk.graph.of_type(ZfsPool))
    before = at.choice

    # Down to the ZFS row, enter, then escape out of the bootloader question.
    keys = ["KEY_DOWN", "KEY_DOWN", "KEY_DOWN", "\n", "\x1b"]
    answer = screens.layout_screen(FakeScreen(keys=keys, lines=24, columns=100), start, at)

    assert answer.outcome is not Answered.CHOSE, answer.outcome
    assert at.choice == before, "the choice goes back with the layout"
    assert start.bootloader.kind is not Bootloader.ZFSBOOTMENU
