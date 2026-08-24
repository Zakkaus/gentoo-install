# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from typing import Final

import tomllib
from pathlib import Path

import pytest

from gentoo_install.errors import ConfigError
from gentoo_install.i18n import LOCALES, Catalog, tag_for, truncate, width


def shipped(tag: str) -> dict[str, str]:
    """The catalog as data, so no CJK literal has to live in the test tree."""
    raw = tomllib.loads((LOCALES / f"{tag}.toml").read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in raw["strings"].items()}


def test_the_first_variable_that_is_set_decides() -> None:
    """`locale(7)` order, and a variable set to C is a request for the
    untranslated interface rather than an absence."""
    assert tag_for({"LANG": "zh_TW.UTF-8"}) == "zh-TW"
    assert tag_for({"LC_MESSAGES": "zh_CN.UTF-8", "LANG": "en_US.UTF-8"}) == "zh-CN"
    assert tag_for({"LC_ALL": "C", "LANG": "zh_TW.UTF-8"}) == "en"
    assert tag_for({}) == "en"


@pytest.mark.parametrize(
    "given,expected",
    [
        ("zh-cn", "zh-CN"),
        ("ZH_tw", "zh-TW"),
        ("zh_HK", "zh-TW"),
        ("zh_SG", "zh-CN"),
        ("en_GB", "en"),
        ("de_DE.UTF-8", "de"),
    ],
)
def test_a_tag_is_normalised_however_it_was_written(given: str, expected: str) -> None:
    assert tag_for({}, override=given) == expected


def test_chinese_with_no_catalog_falls_back_to_english() -> None:
    """Guessing between the two scripts is worse than English, which the
    operator can at least read."""
    assert tag_for({"LANG": "zh_MO.UTF-8"}) == "en"


def test_a_missing_string_falls_back_to_its_source() -> None:
    catalog = Catalog("zh-TW")
    # Compared against the catalog file rather than a literal here, because no
    # CJK belongs in the test tree.
    assert catalog("Disks") == shipped("zh-TW")["Disks"]
    assert catalog("a string nobody translated") == "a string nobody translated"


def test_a_language_with_no_catalog_reads_as_english(tmp_path: Path) -> None:
    assert Catalog("de", tmp_path)("Disks") == "Disks"


def test_a_broken_catalog_is_named_rather_than_ignored(tmp_path: Path) -> None:
    (tmp_path / "xx.toml").write_text("[strings]\nDisks = 3\n")
    with pytest.raises(ConfigError, match="Disks"):
        Catalog("xx", tmp_path)("Disks")


def test_every_shipped_catalog_translates_the_same_keys() -> None:
    """A key in one catalog and not the other is a screen that changes language
    halfway down."""
    tags = sorted(path.stem for path in LOCALES.glob("*.toml"))
    assert tags == ["ja", "ko", "zh-CN", "zh-TW"]
    keys = [set(shipped(tag)) for tag in tags]
    assert all(other == keys[0] for other in keys[1:])
    for tag in tags:
        catalog = Catalog(tag)
        for key, value in shipped(tag).items():
            assert value and catalog(key) == value, key


#: One English term, one rendering per catalog. Written by codepoint because
#: no CJK literal belongs in the test tree. The pairs are (kept, refused):
#: `zh-TW.toml` translated `bootloader` as the row label in two places and as
#: another word in the two hints beside it, and `zh-CN.toml` had two words for
#: `account` in four strings.
GLOSSARY: dict[str, tuple[tuple[str, str], ...]] = {
    # `\u5f15\u5c0e\u7a0b\u5f0f` rather than `\u958b\u6a5f\u8f09\u5165\u5668`, chosen by the operator on
    # 2026-08-21. What the rule holds is unchanged: one term for one thing.
    "zh-TW": ((
        "\u5f15\u5c0e\u7a0b\u5f0f",
        "\u958b\u6a5f\u8f09\u5165\u5668",
    ),),
    "zh-CN": (
        ("\u5f15\u5bfc\u52a0\u8f7d\u5668", "\u5f15\u5bfc\u7a0b\u5e8f"),
        ("\u8d26\u6237", "\u8d26\u53f7"),
    ),
}


def test_one_english_term_reads_as_one_word_in_a_catalog() -> None:
    """A screen that calls the bootloader one thing in its title and another
    in the hint under it reads as two features. Both catalogs did that."""
    for tag, pairs in GLOSSARY.items():
        catalog = shipped(tag)
        for kept, refused in pairs:
            used = [key for key, value in catalog.items() if refused in value]
            assert not used, (tag, refused, used)
            # The entry has to be about something this catalog says, or it is
            # a rule that cannot fail.
            assert any(kept in value for value in catalog.values()), (tag, kept)


#: A wide character and a combining mark, by codepoint: no CJK literal belongs
#: in the test tree, and these are what the width rule exists for.
WIDE = "\u78c1\u789f"
COMBINING = "e\u0301"


def test_width_counts_cells_and_not_characters() -> None:
    """A CJK character takes two columns and a combining mark takes none, so
    `len` lays the screen out wrongly for exactly the users this is for."""
    assert width("Disks") == 5
    assert width(WIDE) == 4
    assert width(f"{WIDE} Disks") == 10
    assert width(COMBINING) == 1


def test_truncate_never_cuts_a_character_in_half() -> None:
    assert truncate(WIDE + WIDE, 5) == WIDE
    assert truncate("Disks", 99) == "Disks"
    assert width(truncate(WIDE + WIDE, 5)) <= 5


def in_a_table() -> set[str]:
    """Reasons the screens keep in a table and translate where the row is drawn.

    Read from the tables rather than listed again here: a second list is one
    more thing to forget when a row is added.
    """
    from gentoo_install.model.mirrors import GENTOO_SITES, GENTOOZH_SITES
    from gentoo_install.plan.system import LOGGERS
    from gentoo_install.tui.mirror import BINHOSTS, GENTOOZH_CHANNELS, SYNC_METHODS
    from gentoo_install.tui.packages import DISPLAY_MANAGERS, GRAPHICS
    from gentoo_install.tui.screens import (
        RAM_SHARES,
        INSTALL_MODES,
        KERNELS,
        LICENSES,
    )

    tables = (
        KERNELS, LICENSES, GRAPHICS, DISPLAY_MANAGERS,
        SYNC_METHODS, GENTOOZH_CHANNELS, BINHOSTS, INSTALL_MODES,
    )
    found = {reason for table in tables for _, reason in table}
    # Every key the interface answers, from the one table the help page draws.
    from gentoo_install.tui.widgets import KEY_HELP

    found |= {row.does for row in KEY_HELP}
    # Why a mode is not offered. The screen translates the reason it was
    # handed, so the literal is here and not at the call site.
    from gentoo_install.model import refusals

    found |= {
        getattr(refusals, name)
        for name in dir(refusals)
        if name.isupper() and isinstance(getattr(refusals, name), str)
    }
    # What each row decides and the heading it sits under, read from the rows
    # themselves: the right pane translates whatever it was handed.
    from gentoo_install.tui.settings import DD_SETTINGS, SETTINGS

    for table in (SETTINGS, DD_SETTINGS):
        for one in table:
            found |= {one.describes, one.section} - {""}
    # The logger table lives in `plan/` because it also names the package and
    # the service; the menu reads the same rows rather than keeping a copy.
    found |= {choice.reason for choice in LOGGERS.values()}
    # Each status is drawn by its own value, and its sentence lives beside the
    # enum so a status added without one cannot reach a menu.
    from gentoo_install.model.manual import STATUS_REASONS

    found |= {one.value for one in STATUS_REASONS}
    # Every compatibility rule reaches the screen as one sentence, and each
    # half of it is translated: the trait names were not keys, so a Chinese
    # interface drew `root on ZFS excludes BIOS boot` in English in front of a
    # translated reason.
    import ast

    from gentoo_install.model import compat
    from gentoo_install.model.compat import RULES, Trait

    found |= {one.value for one in Trait}
    found |= {one.reason for one in RULES}
    found.add("{when} excludes {excludes}")
    compat_path = compat.__file__
    assert compat_path is not None
    for call in ast.walk(ast.parse(Path(compat_path).read_text(encoding="utf-8"))):
        if (
            isinstance(call, ast.Call)
            and getattr(call.func, "id", "") == "InputProblem"
            and len(call.args) > 1
            and isinstance(call.args[1], ast.Constant)
            and isinstance(call.args[1].value, str)
        ):
            found.add(call.args[1].value)
    # The overview counts the operations per stage, and each stage name is
    # drawn: the whole line was English at the top of a translated screen.
    from gentoo_install.plan.operations import Stage

    found |= {one.value for one in Stage}
    found.add("{count} operations")
    # The panel translates `Added.because` through a variable, so the reasons
    # are read from the table that declares them.
    from gentoo_install.plan.automatic import REASONS

    found |= set(REASONS)
    # The share names are drawn through a variable for the same reason.
    found |= {name for name, _ in RAM_SHARES}
    # Every compatibility refusal is drawn through `Rule.reason`, which is a
    # variable: the menu said why in English inside a translated screen.
    from gentoo_install.model.compat import RULES

    found |= {rule.reason for rule in RULES}
    # `Setting.missing` is drawn through a variable: a confirmation says
    # something other than that a field is empty.
    from gentoo_install.tui.settings import SETTINGS

    found |= {
        one.missing for group in SETTINGS for one in (group, *group.rows) if one.required
    }
    found |= set(STATUS_REASONS.values())
    # A mirror is drawn by its own name and where it is, both translated: a
    # Chinese interface listing "Nanjing University" reads half-finished.
    for site in (*GENTOO_SITES, *GENTOOZH_SITES):
        found.add(site.name)
        found.add(site.area)
    return found


def operation_templates() -> set[str]:
    """Description templates the overview passes to the catalog indirectly."""
    import ast

    from gentoo_install.plan import dd, kernel, portage, system

    def templates(expression: ast.expr) -> set[str]:
        if isinstance(expression, ast.IfExp):
            return templates(expression.body) | templates(expression.orelse)
        if not isinstance(expression, ast.Tuple) or not expression.elts:
            return set()
        template = expression.elts[0]
        if isinstance(template, ast.Constant) and isinstance(template.value, str):
            return {template.value}
        return set()

    found: set[str] = set()
    for module in (dd, kernel, portage, system):
        module_path = module.__file__
        assert module_path is not None
        tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "describe_parts":
                continue
            for returned in ast.walk(node):
                if isinstance(returned, ast.Return) and returned.value is not None:
                    found |= templates(returned.value)
    return found


def displayed() -> set[str]:
    """Every source string the interface passes through the catalog."""
    import ast

    from gentoo_install import tui

    found = in_a_table()
    found |= operation_templates()
    for module in Path(tui.__file__).parent.parent.rglob("*.py"):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if called in {"translate", "_translate"} and node.args and isinstance(
                node.args[0], ast.Constant
            ):
                found.add(str(node.args[0].value))
            if called == "Setting" and len(node.args) > 1:
                if isinstance(node.args[1], ast.Constant):
                    found.add(str(node.args[1].value))
            # `footer(translate, "Start writing to the disks")`: the string
            # names what enter does on that screen and is translated inside.
            if called == "footer" and len(node.args) > 1:
                if isinstance(node.args[1], ast.Constant):
                    found.add(str(node.args[1].value))
            # `_consent_screen(screen, config, context, "Font configuration",
            # summary)`: its fourth argument is the title and it calls
            # `translate(title)` itself. Without this the collector could not
            # see that title at all, so `Font configuration` drew in English
            # on four translated screens and this test stayed green.
            if called == "_consent_screen" and len(node.args) > 3:
                if isinstance(node.args[3], ast.Constant):
                    found.add(str(node.args[3].value))
    return found


def test_the_catalogs_hold_every_string_the_interface_shows() -> None:
    """A string added without its translation reads as English in the middle of
    a translated screen, and a stale key hides a screen nobody updated."""
    shown = displayed()
    keys = set(shipped("zh-TW"))
    assert not shown - keys, sorted(shown - keys)
    assert not keys - shown, sorted(keys - shown)


def test_operation_templates_keep_their_positional_values() -> None:
    for tag in ("ja", "ko", "zh-CN", "zh-TW"):
        strings = shipped(tag)
        for template in operation_templates():
            assert strings[template].count("{}") == template.count("{}"), (tag, template)


def _shown(value: object) -> list[str]:
    """The strings an expression can end up drawing.

    Only the result branches: `subarch.endswith("v3")` decides which branch is
    taken and never reaches the screen, so a walk of the whole expression
    reports the condition as untranslated text.
    """
    import ast

    if isinstance(value, ast.Constant):
        return [str(value.value)] if value.value else []
    if isinstance(value, ast.IfExp):
        return _shown(value.body) + _shown(value.orelse)
    if isinstance(value, ast.BoolOp):
        return [name for one in value.values for name in _shown(one)]
    # A call, an attribute or a name: `translate(...)`, a rule's own `reason`,
    # or a variable, none of which is a literal in the source.
    return []


def test_no_row_is_greyed_out_with_a_string_the_catalog_never_saw() -> None:
    """`disabled_because` is drawn beside the label, so an English literal
    there put one English line in the middle of a translated menu. It is
    either empty, a catalog lookup, or a rule's own reason from `compat.py`."""
    import ast

    from gentoo_install import tui

    for module in Path(tui.__file__).parent.rglob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "disabled_because":
                continue
            literal = _shown(node.value)
            assert not literal, f"{module.name}: disabled_because={literal} is not translated"


def test_a_rule_is_translated_whole_and_not_half() -> None:
    """`root on ZFS excludes BIOS boot:` was drawn in English in front of a
    translated reason: the reason was a catalog key and the trait names were
    not."""
    from gentoo_install.model.compat import RULES, Trait

    said = Catalog("zh-TW")
    for rule in RULES:
        drawn = rule.describe(said)
        # The template first: it is what carried the English `excludes`.
        assert " excludes " not in drawn, drawn
        # Then each half, unless the catalog keeps the term as it stands.
        # `LUKS` and `GRUB` are the names of the things themselves.
        for trait in (rule.when, rule.excludes):
            if said(trait.value) == trait.value:
                continue
            assert trait.value not in drawn, (trait, drawn)


def test_the_blocked_row_reads_the_table_rather_than_the_exception() -> None:
    """`validate` builds its message in English for a log, and that message
    reached the one row the operator reads."""
    import inspect

    from gentoo_install.tui import app

    source = inspect.getsource(app._blocked)
    assert "describe(context.translate)" in source, source


def test_no_setting_row_shows_an_untranslated_english_word() -> None:
    """A row's value is drawn beside a translated label. `2 authorised`,
    `manual, 3 partitions` and `-O2 -pipe (stage3 default)` were English in the
    middle of a translated menu, because they were built with an f-string
    instead of a catalog template.

    Read from the source rather than by rendering: a row whose value depends on
    a probed machine cannot be produced here, and the defect is the literal.
    """
    import ast
    from pathlib import Path

    settings = Path(__file__).resolve().parents[2] / "gentoo_install/tui/settings.py"
    tree = ast.parse(settings.read_text())
    #: Words that are prose rather than an identifier, a unit or an acronym.
    prose = {"authorised", "partitions", "default", "none", "manual", "unset", "and"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for piece in node.values:
            if not isinstance(piece, ast.Constant) or not isinstance(piece.value, str):
                continue
            for word in piece.value.replace("(", " ").replace(",", " ").split():
                if word.lower() in prose:
                    offenders.append(f"line {node.lineno}: {piece.value!r}")
    assert not offenders, offenders


#: Words a translated panel may still show: identifiers the ecosystem uses,
#: package atoms, device names and command flags. Everything else in a row's
#: value is prose, and prose belongs in the catalog.
#: Words a translated row may keep. Every one is a proper noun, a filesystem,
#: a package or a literal the system itself prints. `builtin` was here and is
#: not one of those: it describes what the init already has, and the allowance
#: is what let the network row read `builtin` in a Chinese interface.
KEPT_IN_ENGLISH: frozenset[str] = frozenset(
    {
        "gentoo", "grub", "efi", "uefi", "bios", "zfs", "luks", "lvm", "mdraid", "utf",
        "btrfs", "ext", "xfs", "vfat", "f2fs", "swap", "dhcp", "ssh", "sshd", "gpt", "mbr",
        "zram", "openrc", "systemd", "dracut", "journald", "pipe", "native", "free",
        "binary", "redistributable", "fcitx", "rime", "anthy", "mozc", "hangul", "ibus",
        "plasma", "gnome", "xfce", "sddm", "gdm", "lightdm", "greetd", "amd", "intel",
        "nvidia", "nouveau", "none", "true", "false", "auto", "cronie", "tmpfs", "dist",
        "stage", "bin", "cjk", "default", "console", "linux", "amd64", "desktop",
        "multilib", "virtio", "target", "disk", "whole", "init", "profile", "kernel",
        "sys", "asia", "utc", "taipei", "shanghai", "tokyo", "seoul", "zfsbootmenu",
        "networkmanager", "iwd", "wpa", "supplicant", "guru", "gig", "rsync", "git",
        "webrsync", "official", "community", "off", "cpu", "flags", "ram",
    }
)


def test_the_panel_shows_no_untranslated_prose_under_a_chinese_catalog() -> None:
    """`a partition` and `(this machine)` reached a translated panel: the
    strings were built without `translate`, so the catalog completeness test
    never saw them — it collects what is passed to `translate` and these were
    not passed to anything."""
    import re
    from dataclasses import replace as replaced

    from gentoo_install.i18n import Catalog
    from gentoo_install.tui import settings as tui_settings

    from .layouts import config, ext4_on_gpt
    from .test_tui_app import context

    at = context()
    at.translate = Catalog("zh-TW")
    at.columns = 100
    base = config(ext4_on_gpt())
    base = replaced(base, portage=replaced(base.portage, makeopts=""))

    word = re.compile(r"[A-Za-z]{4,}")
    leaked: list[str] = []
    read = 0
    for group in tui_settings.SETTINGS:
        for row in group.rows or (group,):
            shown = str(row.value(base, at))
            if not shown.strip():
                continue
            read += 1
            unknown = [
                one for one in word.findall(shown) if one.lower() not in KEPT_IN_ENGLISH
            ]
            if unknown:
                leaked.append(f"{row.key}: {shown!r}")
    # The denominator, because a scan of nothing reports nothing: 54 rows all
    # rendered on 2026-08-20, and a row that stops rendering leaves the scan
    # silent rather than red.
    assert read >= 50, read
    assert not leaked, leaked


def test_no_catalog_shows_a_particle_the_writer_had_not_chosen() -> None:
    """Korean picks its subject and object particles by the sound of the word
    before them, and the compatibility message printed both candidates in
    brackets rather than one of them: the operator read a sentence with
    `(wa)` and `(neun)` left in it.

    A value interpolates a word the catalog cannot see, so the sentence has to
    be one that needs no particle chosen at all.
    """
    import re
    import tomllib
    from pathlib import Path

    # Codepoints, not literals: no file under tests/ holds a wide character.
    # A Hangul syllable immediately before `(` is a particle candidate.
    candidates = re.compile("[\uac00-\ud7a3]\\(")
    for catalog in sorted(Path("gentoo_install/data/locale").glob("*.toml")):
        said = tomllib.loads(catalog.read_text())["strings"]
        for source, value in said.items():
            if "{" not in source:
                continue
            assert not candidates.search(value), f"{catalog.name}: {source}"


#: Every operation template carrying more than one placeholder. The values are
#: substituted in order, so a translation that moves them to suit its own word
#: order silently renames them: an agent's four catalogs each read `import
#: <fingerprint> from <key_path>` where the values are `(fingerprint,
#: key_path, binhost)`, and the same slip put the runlevel where the service
#: goes and rotated the three names in the stage3 line.
REVIEWED_TEMPLATES: frozenset[str] = frozenset(
    {
        "write {} for {} with addresses {}; gateways {}; DNS {}",
        "write {} for the wired interface with addresses {}; gateways {}; DNS {}",
        "write {} for {} with DHCP and DNS {}",
        "write {} for the wired interfaces with DHCP and DNS {}",
        "write {} for {} with DHCP",
        "configure {} of compressed swap in {}",
        "create user {} in {} with a password",
        "create user {} in {} with no password",
        "download the newest {} stage3 from {} directly, verify it against {},"
        " unpack it into the target and write {}",
        "download the newest {} stage3 from {} via {}, verify it against {},"
        " unpack it into the target and write {}",
        "enable {} in the {} runlevel",
        "import {} from {}, locally sign it for {}, and verify the local signature",
        "the remote unlock address {} is IPv{} and its gateway {} is IPv{}, so the initramfs has"
        " no route",
        "set the console keymap to {} and its font to {} in {}",
        "set the hostname to {} in {}",
        "keep proxy environment for {} in {}",
        "write {} with locales {} and verify them",
        "write {} pointing {} at {}, commit signatures verified",
        "write {} pointing {} at {}",
        "write {} so repository {} syncs with emerge-webrsync",
        "write {} adding verified binary package host {} at {}",
        "write {} adding unverified binary package host {} at {}",
        "write {} to run a script from {} and {} commands at the first boot",
        "write {} to run a script from {} at the first boot",
        "write {} to run {} commands at the first boot",
        "set the system locale to {} in {}",
        "start a login on {} at {} baud",
        "stream the {} image {} onto {}",
        "sync repository {}, {} other sites to fall back on",
        "write /etc/portage/make.conf with {}; {} mirrors fastest first, measured",
        "write /etc/portage/make.conf with {}; {} mirrors fastest first, measured,"
        " {} appended",
        "write /etc/portage/make.conf with {}; {} mirrors in the configured order",
        "write /etc/portage/make.conf with {}; {} mirrors in the configured order,"
        " {} appended",
        "write /etc/kernel/cmdline with root {}; luks {}; arrays {}; keymap {}; parameters {}",
        "write {} into {}",
        "write {} with {}",
        "{}: emerge {}",
        "{}: emerge {}, building {} here",
        "{}: emerge {}, building {} here, with no binhost",
        "{}: emerge {}, from source",
        "{}: emerge {}, with no binhost",
    }
)


def test_every_multi_placeholder_template_has_been_read_by_a_reviewer() -> None:
    """Placeholder order cannot be checked against meaning by a machine: a
    translation legitimately moves words, and only a reader knows which value
    lands in which slot. Counting placeholders passes a catalog that says
    `from <fingerprint> import <key_path>`, which is what shipped.

    So the set is named here as well as there, and adding a template fails
    until somebody has read its four translations.
    """
    tags = sorted(path.stem for path in LOCALES.glob("*.toml"))
    # The union, not one catalog: a template added to a single locale would
    # otherwise pass here and be caught only by the same-keys test.
    found = {
        key for tag in tags for key in shipped(tag) if key.count("{}") >= 2
    }

    assert found == REVIEWED_TEMPLATES, {
        "added": sorted(found - REVIEWED_TEMPLATES),
        "gone": sorted(REVIEWED_TEMPLATES - found),
    }
    # Same placeholder count in every locale, which is the part a machine can
    # check: a missing slot raises `IndexError` at format time.
    for tag in tags:
        catalog = shipped(tag)
        for key in sorted(REVIEWED_TEMPLATES):
            assert catalog[key].count("{}") == key.count("{}"), (tag, key)


def test_a_cut_value_says_it_was_cut() -> None:
    """Every long value on the Mirrors screen is a URL, and `truncate` alone
    made a cut one read as a whole one. `clip` is what the widgets call."""
    from gentoo_install.i18n import CUT, clip, truncate, width

    url = "https://mirrors.ustc.edu.cn/gentoo/releases/amd64/autobuilds"
    assert clip(url, 20) != truncate(url, 20)
    assert clip(url, 20).endswith(CUT)
    assert width(clip(url, 20)) <= 20
    # A value that fits is untouched, so the mark means what it says.
    assert clip("gentoo", 20) == "gentoo"
    # Cells, not characters, and never half of one.
    wide = "\u5b89\u88dd\u7a0b\u5f0f\u78bc"
    assert width(clip(wide, 5)) <= 5
    assert clip(wide, 5).endswith(CUT)
    assert clip(wide, 0) == ""


def test_every_widget_that_shows_a_value_cuts_through_clip() -> None:
    """A widget calling `truncate` directly cuts without the mark, which is
    the defect this pair exists to close. `spread` is the exception: it pads
    to an exact width and the caller has already clipped what it passes."""
    import ast
    from pathlib import Path as _Path

    for module in ("gentoo_install/tui/widgets.py", "gentoo_install/tui/context.py"):
        tree = ast.parse(_Path(module).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "truncate":
                continue
            enclosing = [
                one.name
                for one in ast.walk(tree)
                if isinstance(one, ast.FunctionDef)
                and one.lineno <= node.lineno <= (one.end_lineno or one.lineno)
            ]
            # `wrap_to_cells` is the third: it measures where a line
            # continues rather than where it ends, so nothing is dropped and
            # no mark is due. `clip` is what cuts, and it marks.
            assert enclosing and enclosing[-1] in {"spread", "clip", "wrap_to_cells"}, (
                module,
                node.lineno,
            )


def test_no_two_rows_on_one_screen_translate_to_the_same_word() -> None:
    """`Disk` names the group and `Drive` names the device inside it; every
    catalog gave both the same word, so `Drive: still needs an answer` was read
    in front of a `Disk` row already showing `/dev/vda`. An agent driving
    spec 5 stalled there."""
    from tests.unit.layouts import config

    from gentoo_install.tui.settings import settings_for

    table = settings_for(config())
    for tag in sorted(path.stem for path in LOCALES.glob("*.toml")):
        catalog = Catalog(tag)
        for group in table:
            said: dict[str, str] = {}
            for label in [group.label, *(row.label for row in group.rows)]:
                drawn = catalog(label)
                assert said.get(drawn, label) == label, (
                    f"{tag}: {said.get(drawn)!r} and {label!r} are both {drawn!r} "
                    f"in {group.label!r}"
                )
                said[drawn] = label


def test_no_catalog_reverses_what_is_written_into_what() -> None:
    """The source describes `/etc/fstab` followed by its full content."""
    import tomllib
    from pathlib import Path

    fstab = "/etc/fstab"
    for catalog in sorted(Path("gentoo_install/data/locale").glob("zh-*.toml")):
        said = tomllib.loads(catalog.read_text())["strings"]
        value = said["write /etc/fstab with {}"]
        assert value.index(fstab) < value.index("{}"), f"{catalog.name}: {value}"

    # The reversed wording this replaces fails the rule.
    reversed_value = "{} \u5199\u5165 /etc/fstab"
    assert reversed_value.index(fstab) > reversed_value.index("{}")


def test_no_catalog_calls_a_password_a_cipher() -> None:
    """Three Japanese values named the wrong thing: a cipher where a password
    was meant, a video where a disk image was meant, and a grant of authority
    where a software licence was meant. None reads as the word the interface
    is about."""
    import tomllib
    from pathlib import Path

    # Codepoints, not literals: no file under tests/ holds a wide character.
    cipher_alone = "\u6697\u53f7\u3002"  # cipher, at the end of a sentence
    video = "\u6620\u50cf"  # moving pictures
    granting = "\u6388\u6a29"  # granting authority
    said = tomllib.loads(
        Path("gentoo_install/data/locale/ja.toml").read_text()
    )["strings"]
    for source, value in said.items():
        for wrong in (cipher_alone, video, granting):
            assert wrong not in value, f"{source}: {value}"

    # Negative control: the word for encryption shares its first character with
    # the one above, and is correct wherever it appears.
    encryption = "\u6697\u53f7\u5316"  # encryption
    assert any(encryption in value for value in said.values())


def test_a_binary_host_is_not_a_binary_package() -> None:
    """`binhost` names the machine that builds and serves packages. Two values
    in each of the Japanese and Korean catalogs dropped the host and spoke only
    of packages, so the row that explains where a package comes from no longer
    said where."""
    import tomllib
    from pathlib import Path

    about_the_host = (
        "~amd64 throughout, so fewer packages match a binary host",
        "what the profile sets, and what a binary host builds",
    )
    for catalog in sorted(Path("gentoo_install/data/locale").glob("*.toml")):
        said = tomllib.loads(catalog.read_text())["strings"]
        for source in about_the_host:
            assert "binhost" in said[source], f"{catalog.name}: {said[source]}"

    # Negative control: the key that is about packages alone does not name a
    # host, so the rule above is not "every value mentions binhost".
    for catalog in sorted(Path("gentoo_install/data/locale").glob("*.toml")):
        said = tomllib.loads(catalog.read_text())["strings"]
        assert "binhost" not in said["Extra packages"], catalog.name


#: Placeholders that are values rather than prose. A package atom, a compiler
#: flag, an address and a port number read the same in every language, and
#: translating one would make the example wrong.
UNTRANSLATED_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {
        "app-editors/vim  media-video/mpv",
        "-O2 -pipe -march=native",
        "192.0.2.10/24",
        "192.0.2.1",
        "2001:db8::2/64",
        "fe80::1",
        "proxy.example.com",
        "3128",
        "222",
    }
)


def test_a_placeholder_is_prose_through_the_catalog_or_a_value_named_here() -> None:
    """`displayed()` grew a fourth wrapper tonight because `Font configuration`
    drew in English on four translated screens and no test saw it.
    `TextField.placeholder` is the fifth: `widgets.py` writes that value
    straight into the field, and a literal there reaches the screen without
    passing the catalog. Twenty-four already go through `translate`; the nine
    that do not are values, and they are listed rather than merely absent."""
    import ast

    from gentoo_install import tui

    bare: list[str] = []
    for source in sorted(Path(tui.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "placeholder":
                    continue
                if isinstance(keyword.value, ast.Constant):
                    bare.append(f"{source.name}:{node.lineno} {keyword.value.value!r}")

    unexpected = [
        one for one in bare
        if one.split(" ", 1)[1].strip("'\"") not in UNTRANSLATED_PLACEHOLDERS
    ]
    assert not unexpected, unexpected
    # And the list holds nothing that has since been translated or removed.
    present = {one.split(" ", 1)[1].strip("'\"") for one in bare}
    assert present == UNTRANSLATED_PLACEHOLDERS, sorted(UNTRANSLATED_PLACEHOLDERS - present)
