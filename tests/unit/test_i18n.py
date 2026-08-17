# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

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
    from gentoo_install.tui.screens import (
        BINHOSTS,
        RAM_SHARES,
        DISPLAY_MANAGERS,
        GENTOOZH_CHANNELS,
        GRAPHICS,
        INSTALL_MODES,
        KERNELS,
        LICENSES,
        SYNC_METHODS,
    )

    tables = (
        KERNELS, LICENSES, GRAPHICS, DISPLAY_MANAGERS,
        SYNC_METHODS, GENTOOZH_CHANNELS, BINHOSTS, INSTALL_MODES,
    )
    found = {reason for table in tables for _, reason in table}
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
    from gentoo_install.model.compat import RULES, Trait

    found |= {one.value for one in Trait}
    found |= {one.reason for one in RULES}
    found.add("{when} excludes {excludes}")
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

    from gentoo_install.plan import portage, system

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
    for module in (portage, system):
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
    for group in tui_settings.SETTINGS:
        for row in group.rows or (group,):
            shown = str(row.value(base, at))
            unknown = [
                one for one in word.findall(shown) if one.lower() not in KEPT_IN_ENGLISH
            ]
            if unknown:
                leaked.append(f"{row.key}: {shown!r}")
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
        "add unverified binary package host {} at {}",
        "add verified binary package host {} at {}",
        "authorise {} ssh key(s) for {}",
        "configure {} as {}",
        "create user {} in {} with a password",
        "create user {} in {} with no password",
        "download the newest {} stage3 from {} directly,"
        " verify it against {} and unpack it into the target",
        "download the newest {} stage3 from {} via {},"
        " verify it against {} and unpack it into the target",
        "enable {} in the {} runlevel",
        "import {} from {} and locally sign it for {}",
        "point repository {} at {}",
        "point repository {} at {}, commit signatures verified",
        "run a script from {} and {} commands once, the first time the system boots",
        "set the console keymap to {} and its font to {}",
        "start a login on {} at {} baud",
        "sync repository {}, {} other sites to fall back on",
        "write /etc/portage/make.conf with {}; {} mirrors fastest first, measured",
        "write /etc/portage/make.conf with {}; {} mirrors fastest first, measured,"
        " {} appended",
        "write /etc/portage/make.conf with {}; {} mirrors in the configured order",
        "write /etc/portage/make.conf with {}; {} mirrors in the configured order,"
        " {} appended",
        "write a NetworkManager profile for {} as {}",
        "write {} for {}",
        "{}: emerge {}",
        "{}: emerge {}, building {} here",
        "{}: emerge {}, from source",
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
