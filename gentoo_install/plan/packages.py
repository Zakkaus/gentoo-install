"""Desktop profiles and the applications chosen separately from them.

A profile is data, not code: `data/profiles/*.toml` names packages, services and
the repositories they come from, and an application group has the same shape. So
a user can have an input method without a desktop, or a desktop without one.

The catalog is read by `data.py` and passed in, because this layer stays a pure
function of its arguments.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Final, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import CommandFailed, ConfigError, ValidationFailed
from ..model.config import InitSystem, InstallConfig
from .operations import Context, Operation, Stage
from .portage import Emerge
from .system import CONSOLE_FONTS, EnableService


@dataclass(frozen=True)
class GroupFile:
    """A file a group has to write, such as a modprobe drop-in."""

    path: PurePosixPath
    content: str


@dataclass(frozen=True)
class Group:
    """One profile or application group, exactly as its TOML file declares it."""

    name: str
    #: Operator-facing source text. Empty keeps the group name for groups that
    #: are not presented as a catalog choice.
    label: str = ""
    packages: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    #: Services under systemd, when the two inits name them differently. The
    #: nftables ebuild installs `nftables-load.service` and an OpenRC init
    #: called `nftables`, and no `nftables.service` exists at all.
    systemd_services: tuple[str, ...] = ()
    use: tuple[str, ...] = ()
    #: Repositories the packages come from. Selecting the group is what asks for
    #: them; an overlay is never added behind the user's back.
    repositories: tuple[str, ...] = ()
    #: Values this group adds to `VIDEO_CARDS`, which a driver group needs and
    #: nothing else does.
    video_cards: tuple[str, ...] = ()
    #: Licence groups this group's packages need in `ACCEPT_LICENSE`. A driver
    #: whose licence the default refuses dies an hour into the install.
    accept_license: tuple[str, ...] = ()
    #: The display manager this group installs, if it is one.
    display_manager: str = ""
    #: The Portage profile this desktop is built against. Empty for an
    #: application group, which changes no profile.
    profile: str = ""
    files: tuple[GroupFile, ...] = ()
    #: package.use lines this group needs, written before anything merges.
    package_use: tuple[str, ...] = ()
    #: package.accept_keywords lines this group needs. An atom the tree carries
    #: only under `~amd64` is masked on the default stable channel, and Portage
    #: says so in the package stage, an hour after the disks were written.
    accept_keywords: tuple[str, ...] = ()
    #: Units every user's own systemd instance has to start. openrc has no
    #: equivalent: what it needs is a system service, which `services` covers.
    user_services: tuple[str, ...] = ()
    #: Groups the account has to be in for this group's packages to work.
    #: Added after the packages merge, because the `acct-group` that creates
    #: them comes with the package.
    user_groups: tuple[str, ...] = ()
    #: The input method engine this group provides, if it provides one.
    input_method: str = ""
    #: Which framework that engine belongs to. Two frameworks in one session
    #: fight over the same toolkit modules, so `compat.py` refuses the pair.
    input_framework: str = ""
    #: The language an input engine types, used to group choices without
    #: deriving behavior from package or group names.
    input_language: str = ""
    #: Engine identifier written into a desktop setting when upstream metadata
    #: establishes one. Empty keeps the installed engine under manual control.
    input_source: str = ""
    #: The Fontconfig family exposed by a font package. Regional templates are
    #: resolved from the system locale by the font plan.
    font_family: str = ""
    #: The face kind controls both the menu heading and the generic alias.
    font_category: str = ""
    #: Only families with CJK glyph coverage may lead aliases for a CJK locale.
    font_cjk: bool = False
    #: Package-free choice recording whether Fontconfig aliases were accepted
    #: or declined. Empty preserves the proposed-on default.
    font_configuration: str = ""
    #: Package-free choice recording whether input configuration was accepted
    #: or declined. Empty preserves the proposed-on default.
    input_configuration: str = ""
    #: The desktop entry KWin starts as the input method on Wayland, for a
    #: session that drives one itself. Plasma's Virtual keyboard KCM writes the
    #: same key into kwinrc; this is that choice, made in advance.
    input_method_launcher: str = ""
    #: Lines appended to a file only when the session is Wayland and an input
    #: method is installed. Chromium reaches one over Wayland with a flag and
    #: not otherwise.
    wayland_files: tuple[GroupFile, ...] = ()
    #: Rime schemas the group ships, in the order they should be offered.
    schemas: tuple[str, ...] = ()
    #: Whether the session this group installs is Wayland. On Wayland the input
    #: method environment is set differently, so the group has to say.
    wayland: bool = False


@dataclass(frozen=True, kw_only=True)
class WriteGroupFile(Operation):
    stage: Stage = Stage.PACKAGES
    group: str
    file: GroupFile

    def describe(self) -> str:
        return f"write {self.file.path} for the {self.group} group"

    def apply(self, context: Context) -> None:
        context.write(self.file.path, self.file.content)


@runtime_checkable
class PackageInspection(Protocol):
    """Apply-time access to package-owned state in the installed target."""

    def installed_package_paths(self, package: str) -> frozenset[PurePosixPath]: ...

    def installed_command_help(self, package: str, command: PurePosixPath) -> str: ...

    def target_is_directory(self, path: PurePosixPath) -> bool: ...


def _package_inspection(context: Context, package: str) -> PackageInspection:
    if not isinstance(context, PackageInspection):
        raise CommandFailed(
            f"cannot verify files installed by {package}: the apply context "
            "does not expose package inspection"
        )
    return context


@dataclass(frozen=True, kw_only=True)
class VerifyPackagePaths(Operation):
    stage: Stage = Stage.PACKAGES
    package: str
    paths: tuple[PurePosixPath, ...]

    def describe(self) -> str:
        return f"verify {self.package} installed {', '.join(map(str, self.paths))}"

    def apply(self, context: Context) -> None:
        installed = _package_inspection(context, self.package).installed_package_paths(
            self.package
        )
        missing = tuple(path for path in self.paths if path not in installed)
        if missing:
            raise CommandFailed(
                f"{self.package} was expected to provide "
                f"{', '.join(map(str, missing))}, but its VDB CONTENTS does not list it"
            )


_LONG_OPTION = re.compile(
    r'''(?<![A-Za-z0-9_-])--[A-Za-z0-9][A-Za-z0-9-]*(?=[\s,=\[\]"']|$)'''
)


@dataclass(frozen=True, kw_only=True)
class VerifyCommandOptions(Operation):
    stage: Stage = Stage.PACKAGES
    package: str
    command: PurePosixPath
    options: tuple[str, ...]

    def describe(self) -> str:
        return f"verify {self.package} installed {self.command} with {', '.join(self.options)}"

    def apply(self, context: Context) -> None:
        inspection = _package_inspection(context, self.package)
        installed = inspection.installed_package_paths(self.package)
        if self.command not in installed:
            raise CommandFailed(
                f"{self.package} was expected to provide {self.command}, "
                "but its VDB CONTENTS does not list it"
            )
        help_text = inspection.installed_command_help(self.package, self.command)
        available = frozenset(_LONG_OPTION.findall(help_text))
        missing = tuple(option for option in self.options if option not in available)
        if missing:
            raise CommandFailed(
                f"{self.package} installed {self.command}, but {self.command} --help "
                f"does not list {', '.join(missing)}"
            )


@dataclass(frozen=True, kw_only=True)
class VerifySessionDirectories(Operation):
    stage: Stage = Stage.PACKAGES
    package: str
    paths: tuple[PurePosixPath, ...]

    def describe(self) -> str:
        return f"verify session directories for {self.package}"

    def apply(self, context: Context) -> None:
        if not self.package:
            raise CommandFailed(
                "cannot verify greetd session directories because no desktop package was selected"
            )
        inspection = _package_inspection(context, self.package)
        missing = tuple(path for path in self.paths if not inspection.target_is_directory(path))
        if missing:
            raise CommandFailed(
                f"{self.package} was expected to provide a session in "
                f"{', '.join(map(str, missing))}, but the directory does not exist"
            )


GREETD_PACKAGE: Final[str] = "gui-libs/greetd"
GREETD_CONFIG: Final[PurePosixPath] = PurePosixPath("/etc/greetd/config.toml")
TUIGREET_PACKAGE: Final[str] = "gui-apps/tuigreet"
TUIGREET_COMMAND: Final[PurePosixPath] = PurePosixPath("/usr/bin/tuigreet")


@dataclass(frozen=True, kw_only=True)
class UpdateGreetdConfig(Operation):
    stage: Stage = Stage.PACKAGES
    command: str

    def describe(self) -> str:
        return f"set the greetd default session command in {GREETD_CONFIG}"

    def apply(self, context: Context) -> None:
        installed = _package_inspection(context, GREETD_PACKAGE).installed_package_paths(
            GREETD_PACKAGE
        )
        if GREETD_CONFIG not in installed:
            raise CommandFailed(
                f"{GREETD_PACKAGE} was expected to provide {GREETD_CONFIG}, "
                "but its VDB CONTENTS does not list it"
            )
        current = context.read(GREETD_CONFIG)
        context.write(GREETD_CONFIG, _replace_greetd_command(current, self.command))


_TOML_TABLE = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?(?:\r?\n)?$")
_TOML_COMMAND = re.compile(
    r'^(\s*command\s*=\s*)("(?:[^"\\]|\\.)*")(\s*(?:#.*)?)(\r?\n?)$'
)


def _replace_greetd_command(content: str, command: str) -> str:
    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        raise CommandFailed(f"cannot parse {GREETD_CONFIG} installed by {GREETD_PACKAGE}") from error
    session = parsed.get("default_session")
    if not isinstance(session, dict) or not isinstance(session.get("command"), str):
        raise CommandFailed(
            f"{GREETD_CONFIG} installed by {GREETD_PACKAGE} has no "
            "default_session.command string"
        )

    table = ""
    replaced = 0
    lines: list[str] = []
    for line in content.splitlines(keepends=True):
        header = _TOML_TABLE.match(line)
        if header:
            table = header.group(1).strip()
        match = _TOML_COMMAND.match(line) if table == "default_session" else None
        if match:
            line = f"{match.group(1)}{json.dumps(command)}{match.group(3)}{match.group(4)}"
            replaced += 1
        lines.append(line)
    if replaced != 1:
        raise CommandFailed(
            f"cannot safely update default_session.command in {GREETD_CONFIG} "
            f"installed by {GREETD_PACKAGE}"
        )
    return "".join(lines)


@dataclass(frozen=True, kw_only=True)
class UpdatePackageShellAssignment(Operation):
    stage: Stage = Stage.PACKAGES
    group: str
    package: str
    path: PurePosixPath
    key: str
    value: str

    def describe(self) -> str:
        return f"write {self.path} for the {self.group} group"

    def apply(self, context: Context) -> None:
        installed = _package_inspection(context, self.package).installed_package_paths(
            self.package
        )
        if self.path not in installed:
            raise CommandFailed(
                f"{self.package} was expected to provide {self.path}, "
                "but its VDB CONTENTS does not list it"
            )
        current = context.read(self.path)
        updated = _replace_shell_assignment(current, self.key, self.value, self.path)
        context.write(self.path, updated)


def _replace_shell_assignment(
    content: str, key: str, value: str, path: PurePosixPath
) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ConfigError(f"{key!r} is not a shell variable")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ConfigError(f"{value!r} is not a safe value for {key}")
    assignment = re.compile(
        rf'^(\s*{re.escape(key)}\s*=\s*)("(?:[^"\\]|\\.)*")(\s*(?:#.*)?)(\r?\n?)$'
    )
    replaced = 0
    lines: list[str] = []
    for line in content.splitlines(keepends=True):
        match = assignment.match(line)
        if match:
            line = f'{match.group(1)}"{value}"{match.group(3)}{match.group(4)}'
            replaced += 1
        lines.append(line)
    if replaced != 1:
        raise CommandFailed(f"cannot safely update {key} in {path}")
    return "".join(lines)


Catalog = Mapping[str, Group]


#: Where a session reads environment variables: systemd at user-session start,
#: openrc through `env-update` into /etc/profile.env.
#: Where the input-method variables go, per init.
#:
#: `/etc/environment` on systemd, not `/etc/environment.d/`. The latter reaches
#: only what `systemd --user` starts, and `environment.d(5)` says so under
#: APPLICABILITY; a session sddm, lightdm or greetd launches is a
#: `systemd.scope`, so the variables never arrived. `pam_env` reads
#: `/etc/environment` at every PAM login, which is every graphical one.
ENVIRONMENT_FILE: Final[dict[InitSystem, PurePosixPath]] = {
    InitSystem.SYSTEMD: PurePosixPath("/etc/environment"),
    InitSystem.OPENRC: PurePosixPath("/etc/env.d/90input-method"),
}

#: Console keymaps whose XKB layout is not the part before the first dash or
#: underscore. Checked against `/usr/share/X11/xkb/symbols`: every other
#: two-letter prefix in the keymap tree is a layout name as it stands.
XKB_RENAMED: Final[dict[str, str]] = {
    "uk": "gb",
    "cf": "ca",
    "sg": "ch",
    "sv": "se",
    "sr": "rs",
    "fa": "ir",
    "ky": "kg",
    "en": "us",
    "3l": "de",
}

#: Where the console name carries no country at all. fcitx names a layout, not
#: a variant, so the dvorak and neo families resolve to the layout they sit on.
XKB_FAMILIES: Final[dict[str, str]] = {
    "azerty": "fr",
    "neo": "de",
    "neoqwertz": "de",
    "bone": "de",
    "adnw": "de",
    "koy": "de",
    "dvorak": "us",
    "ansi": "us",
    "carpalx": "us",
    "jp106": "jp",
    "hu101": "hu",
    "croat": "hr",
    "slovene": "si",
    "kazakh": "kz",
    "kyrgyz": "kg",
    "hcesar": "pt",
    "wangbe": "be",
    "wangbe2": "be",
    "bywin": "by",
}

#: What fcitx falls back to, and what every keymap produced before this table
#: existed: `keyboard-de-latin1` is not an entry fcitx has, so the group's
#: first item was invalid and the desktop typed latin anyway.
XKB_DEFAULT: Final[str] = "us"


def xkb_layout(keymap: str) -> str:
    """The XKB layout fcitx wants, from the console keymap that was chosen.

    The two are different namespaces: `de-latin1` is a console keymap and `de`
    is the layout. A name this cannot place falls back to `us`, which is what
    every one of them produced before.
    """
    head = re.split(r"[-_.]", keymap.strip().lower(), maxsplit=1)[0]
    if head in XKB_RENAMED:
        return XKB_RENAMED[head]
    if head in XKB_FAMILIES:
        return XKB_FAMILIES[head]
    return head if len(head) == 2 and head.isalpha() else XKB_DEFAULT


#: KWin's system-wide defaults. A user's own kwinrc still wins, so this is a
#: default and not a decision imposed on them.
KWIN_DEFAULTS: Final[PurePosixPath] = PurePosixPath("/etc/xdg/kwinrc")

#: New users get the same input method as the ones the installer creates.
SKELETON: Final[PurePosixPath] = PurePosixPath("/etc/skel")


@dataclass(frozen=True, kw_only=True)
class WriteGroupKeywords(Operation):
    """Written in the portage phase, for the same reason as the USE flags."""

    stage: Stage = Stage.PORTAGE
    group: str
    lines: tuple[str, ...]

    def describe(self) -> str:
        return f"accept {'; '.join(self.lines)} for the {self.group} group"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath(f"/etc/portage/package.accept_keywords/{self.group}"),
            "".join(f"{line}\n" for line in self.lines),
        )


@dataclass(frozen=True, kw_only=True)
class WriteGroupUse(Operation):
    """Written in the portage phase: the flags have to be set before the
    packages that need them are merged."""

    stage: Stage = Stage.PORTAGE
    group: str
    lines: tuple[str, ...]

    def describe(self) -> str:
        return f"ask for {'; '.join(self.lines)} for the {self.group} group"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath(f"/etc/portage/package.use/{self.group}"),
            "".join(f"{line}\n" for line in self.lines),
        )


@dataclass(frozen=True, kw_only=True)
class EnableUserUnits(Operation):
    """Enable units in every user's own systemd instance.

    `systemctl --global`, because `--user` needs that user's instance running
    and none is during an install. `media-video/pipewire` says it outright:
    "the out-of-the-box experience is automatic on OpenRC, while it needs
    manual intervention on systemd", so without this the packages are merged
    and nothing starts them.
    """

    stage: Stage = Stage.PACKAGES
    group: str
    units: tuple[str, ...]

    def describe(self) -> str:
        return f"enable {', '.join(self.units)} for every user"

    def apply(self, context: Context) -> None:
        # `--force`: wireplumber's own postinst says so, because the symlink it
        # replaces belongs to the session manager it supersedes.
        context.run_in_target(["systemctl", "--global", "--force", "enable", *self.units])


@dataclass(frozen=True, kw_only=True)
class AddUserToGroups(Operation):
    """Put the account in the groups its packages need.

    After the packages, not with the account: the `acct-group` that creates
    each one is a dependency of the package, so `usermod` before the merge
    fails on a group that does not exist yet.
    """

    stage: Stage = Stage.PACKAGES
    user: str
    groups: tuple[str, ...]

    def describe(self) -> str:
        return f"add {self.user} to {', '.join(self.groups)}"

    def apply(self, context: Context) -> None:
        # `-a` as well as `-G`: without it usermod replaces every supplementary
        # group the account has, which takes it out of wheel.
        context.run_in_target(["usermod", "-aG", ",".join(self.groups), self.user])


class Session(Enum):
    """Which of the three input-method situations a session is in.

    From the Fcitx project's own Wayland page: the answer is not the same for
    every compositor, and keying only on Wayland gave GNOME the KWin answer.
    """

    X11 = "x11"
    #: The compositor starts fcitx over `input-method-v2` and drives it over
    #: text-input: Plasma, through its Virtual keyboard KCM. Setting a toolkit
    #: variable here makes the candidate window blink.
    WAYLAND_DRIVEN = "wayland-driven"
    #: Every other Wayland session. mutter has no text-input-v2 and Qt 5 runs
    #: under XWayland there, so Qt reaches fcitx only through its own module.
    WAYLAND_PLAIN = "wayland-plain"


#: The environment each framework needs in each session.
#: `GTK_IM_MODULE` stays unset in both Wayland rows: Gtk 3 and Gtk 4 use
#: text-input-v3, and forcing the module is what the Fcitx page warns against.
INPUT_ENVIRONMENT: Final[dict[tuple[str, Session], tuple[str, ...]]] = {
    ("fcitx", Session.X11): (
        "XMODIFIERS=@im=fcitx",
        "GTK_IM_MODULE=fcitx",
        "QT_IM_MODULE=fcitx",
    ),
    # Qt 6.7 and later take a fallback list, which covers a toolkit that ships
    # no fcitx module without breaking the ones that do.
    ("fcitx", Session.WAYLAND_DRIVEN): (
        "XMODIFIERS=@im=fcitx",
        'QT_IM_MODULES="wayland;fcitx;ibus"',
    ),
    ("fcitx", Session.WAYLAND_PLAIN): (
        "XMODIFIERS=@im=fcitx",
        "QT_IM_MODULE=fcitx",
        'QT_IM_MODULES="wayland;fcitx;ibus"',
    ),
    ("ibus", Session.X11): ("XMODIFIERS=@im=ibus", "GTK_IM_MODULE=ibus", "QT_IM_MODULE=ibus"),
    ("ibus", Session.WAYLAND_DRIVEN): (
        "XMODIFIERS=@im=ibus",
        "GTK_IM_MODULE=ibus",
        "QT_IM_MODULE=ibus",
    ),
    ("ibus", Session.WAYLAND_PLAIN): (
        "XMODIFIERS=@im=ibus",
        "GTK_IM_MODULE=ibus",
        "QT_IM_MODULE=ibus",
    ),
}

GNOME_DESKTOP_GROUP: Final[str] = "gnome"
DCONF_PROFILE: Final[PurePosixPath] = PurePosixPath("/etc/dconf/profile/user")
GNOME_INPUT_SOURCES: Final[PurePosixPath] = PurePosixPath(
    "/etc/dconf/db/local.d/00-gentoo-install-input-sources"
)
INPUT_CONFIGURATION_ENABLED: Final[str] = "enabled"
INPUT_CONFIGURATION_DISABLED: Final[str] = "disabled"
INPUT_CONFIGURATION_STATES: Final[frozenset[str]] = frozenset(
    {INPUT_CONFIGURATION_ENABLED, INPUT_CONFIGURATION_DISABLED}
)
FONT_CONFIGURATION_ENABLED: Final[str] = "enabled"
FONT_CONFIGURATION_DISABLED: Final[str] = "disabled"
FONT_CONFIGURATION_STATES: Final[frozenset[str]] = frozenset(
    {FONT_CONFIGURATION_ENABLED, FONT_CONFIGURATION_DISABLED}
)

CHROMIUM_PACKAGE: Final[str] = "www-client/chromium"
CHROMIUM_CONFIG_PACKAGE: Final[str] = "www-client/chromium-common"
CHROMIUM_COMMAND: Final[PurePosixPath] = PurePosixPath("/usr/bin/chromium")
CHROMIUM_CONFIG: Final[PurePosixPath] = PurePosixPath("/etc/chromium/default")
KBD_PACKAGE: Final[str] = "sys-apps/kbd"


def console_font_path(font: str) -> PurePosixPath:
    """Return the compressed console-font file owned by `sys-apps/kbd`."""
    return PurePosixPath(f"/usr/share/consolefonts/{font}.psfu.gz")


@dataclass(frozen=True, kw_only=True)
class ConfigureKwinInputMethod(Operation):
    """Tell KWin which input method to start on a Wayland session.

    The same key Plasma's Virtual keyboard KCM writes. The launcher entry is
    the one that speaks `input-method-v2`; `org.fcitx.Fcitx5.desktop` is the
    autostart entry and starting fcitx that way integrates incorrectly.
    """

    stage: Stage = Stage.PACKAGES
    launcher: str

    def describe(self) -> str:
        return f"set {KWIN_DEFAULTS} so the Wayland session starts {self.launcher}"

    def apply(self, context: Context) -> None:
        context.write(
            KWIN_DEFAULTS,
            "[Wayland]\n"
            f"InputMethod={self.launcher}\n"
            "VirtualKeyboardEnabled=true\n",
        )


@dataclass(frozen=True, kw_only=True)
class ConfigureGnomeInputSources(Operation):
    """Set the GNOME input-source default to declared IBus engine IDs."""

    stage: Stage = Stage.PACKAGES
    layout: str
    engines: tuple[str, ...]

    def describe(self) -> str:
        return f"write the GNOME dconf default with {', '.join(self.engines)}"

    def apply(self, context: Context) -> None:
        sources = [("xkb", self.layout), *(("ibus", engine) for engine in self.engines)]
        context.write(DCONF_PROFILE, "user-db:user\nsystem-db:local\n")
        context.write(
            GNOME_INPUT_SOURCES,
            "[org/gnome/desktop/input-sources]\n"
            f"sources={sources!r}\n",
        )
        context.run_in_target(["dconf", "update"])


@dataclass(frozen=True, kw_only=True)
class AppendWaylandFlags(Operation):
    """A line an application needs before it can reach an input method.

    Appended after the packages are installed, so the file the package ships is
    already there and Portage has no configuration conflict to leave behind.
    """

    stage: Stage = Stage.PACKAGES
    group: str
    file: GroupFile

    def describe(self) -> str:
        return f"add the Wayland input method flags for {self.group} to {self.file.path}"

    def apply(self, context: Context) -> None:
        if self.file.content.strip() in context.read(self.file.path):
            return
        context.append(self.file.path, f"{self.file.content.rstrip()}\n")


@dataclass(frozen=True, kw_only=True)
class WriteInputMethodEnvironment(Operation):
    """`XMODIFIERS` always; which toolkit variables follow it is per session.

    Plasma drives fcitx over text-input and a toolkit variable there makes the
    candidate window blink. mutter does not, so GNOME needs `QT_IM_MODULE`.
    """

    stage: Stage = Stage.PACKAGES
    init: InitSystem
    framework: str
    session: Session

    def _lines(self) -> tuple[str, ...]:
        return INPUT_ENVIRONMENT[(self.framework, self.session)]

    def describe(self) -> str:
        named = ", ".join(one.split("=")[0] for one in self._lines())
        return f"set {named} in {ENVIRONMENT_FILE[self.init]} for {self.framework}"

    def apply(self, context: Context) -> None:
        where = ENVIRONMENT_FILE[self.init]
        if self.init is InitSystem.SYSTEMD:
            # Appended: `/etc/environment` is a file other things write into,
            # unlike the drop-in this used to replace.
            context.append(where, "\n".join(self._lines()) + "\n")
        else:
            context.write(where, "\n".join(self._lines()) + "\n")
        if self.init is InitSystem.OPENRC:
            # env.d is a source directory: nothing reads it until env-update
            # regenerates /etc/profile.env from it.
            context.run_in_target(["env-update"])


@dataclass(frozen=True, kw_only=True)
class WriteInputMethodProfile(Operation):
    """fcitx starts with no engine configured, so a fresh desktop types latin
    until someone opens the configuration tool and adds one by hand."""

    stage: Stage = Stage.PACKAGES
    #: Every engine the chosen groups provide. All of them, not the first: a
    #: desktop that asked for Chinese and Japanese needs both in the profile,
    #: and the ones left out are installed and unreachable.
    engines: tuple[str, ...]
    schemas: tuple[str, ...]
    layout: str
    #: Home directories to seed, `/etc/skel` included so later users match.
    homes: tuple[tuple[PurePosixPath, str], ...]

    def describe(self) -> str:
        who = ", ".join(owner or "skel" for _, owner in self.homes)
        listed = " ".join(self.schemas) or "no rime schema"
        return f"configure fcitx with {', '.join(self.engines)} and {listed} for {who}"

    def apply(self, context: Context) -> None:
        for home, owner in self.homes:
            context.write(home / ".config/fcitx5/profile", self._profile())
            for version in ("3.0", "4.0"):
                context.write(
                    home / f".config/gtk-{version}/settings.ini",
                    "[Settings]\ngtk-im-module=fcitx\n",
                )
            if self.schemas:
                context.write(
                    home / ".local/share/fcitx5/rime/default.custom.yaml", self._rime()
                )
            if owner:
                context.run_in_target(["chown", "--recursive", f"{owner}:{owner}", str(home)])

    def _profile(self) -> str:
        """fcitx's own ini. The keyboard is first and the default, so a console
        or a password field does not start composing."""
        keyboard = f"keyboard-{self.layout}"
        head = (
            "[Groups/0]\n"
            "Name=Default\n"
            f"Default Layout={self.layout}\n"
            f"DefaultIM={keyboard}\n"
        )
        items = "".join(
            f"\n[Groups/0/Items/{index}]\nName={name}\nLayout=\n"
            for index, name in enumerate((keyboard, *self.engines))
        )
        return f"{head}{items}\n[GroupOrder]\n0=Default\n"

    def _rime(self) -> str:
        listed = "".join(f"    - schema: {schema}\n" for schema in self.schemas)
        return f"patch:\n  schema_list:\n{listed}"


#: Driver groups that cannot be installed together, and why. Two drivers for
#: the same adapter, not two adapters: `amdgpu` and `radeon` cover different
#: AMD generations and a machine can hold one card of each, so they are not
#: here.
EXCLUSIVE_DRIVERS: tuple[tuple[str, str, str], ...] = (
    (
        "nouveau",
        "nvidia",
        "nouveau and nvidia drive the same card, and nvidia-drivers blacklists "
        "nouveau in its own modprobe.d file, so ticking both installs one that "
        "cannot load",
    ),
)


def driver_conflict(config: InstallConfig, catalog: Catalog) -> str:
    """Why the ticked drivers cannot be installed together, or empty.

    Read by `build` and by the screen that offers them, the way `compat.py` is
    read by the validator and by the menu.
    """
    return _driver_conflict(config, groups(config, catalog))


def _driver_conflict(config: InstallConfig, chosen: tuple[Group, ...]) -> str:
    selected = {group.name for group in chosen}
    ticked = {name for name in config.packages.graphics if name in selected}
    for one, other, reason in EXCLUSIVE_DRIVERS:
        if one in ticked and other in ticked:
            return reason
    return ""


def framework_conflict(config: InstallConfig, catalog: Catalog) -> str:
    """Why the chosen groups cannot be installed together, or empty.

    Two input method frameworks in one session both provide the Gtk and Qt
    modules and both claim `XMODIFIERS`, so whichever loses is installed and
    unreachable. Read by `build` and by the screen that offers the groups, the
    way `compat.py` is read by the validator and by the menu.
    """
    return _framework_conflict(groups(config, catalog))


def _framework_conflict(chosen: tuple[Group, ...]) -> str:
    named: dict[str, list[str]] = {}
    for group in chosen:
        if group.input_framework:
            named.setdefault(group.input_framework, []).append(group.name)
    if len(named) < 2:
        return ""
    listed = "; ".join(
        f"{framework} from {', '.join(sorted(names))}" for framework, names in sorted(named.items())
    )
    return (
        f"two input method frameworks were chosen ({listed}); they claim the same "
        "toolkit modules, so pick one"
    )


def _group_file_operations(
    config: InstallConfig, catalog: Catalog, group: Group, wanted: GroupFile
) -> list[Operation]:
    if wanted.path != GREETD_CONFIG:
        return [WriteGroupFile(group=group.name, file=wanted)]
    if group.name != "greetd" or GREETD_PACKAGE not in group.packages:
        raise ConfigError(f"{wanted.path} has no declared package contract")
    try:
        desired = tomllib.loads(wanted.content)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"the {group.name} group has invalid TOML for {wanted.path}") from error
    session = desired.get("default_session")
    command = session.get("command") if isinstance(session, dict) else None
    if not isinstance(command, str):
        raise ConfigError(f"the {group.name} group does not declare default_session.command")
    try:
        words = shlex.split(command)
    except ValueError as error:
        raise ConfigError(f"the {group.name} group has an invalid greeter command") from error
    if not words or PurePosixPath(words[0]).name != TUIGREET_COMMAND.name:
        raise ConfigError(
            f"the {group.name} group expected {TUIGREET_PACKAGE} to provide {words[0] if words else ''}"
        )
    options = tuple(dict.fromkeys(_LONG_OPTION.findall(command)))
    directories: list[PurePosixPath] = []
    for option in ("--sessions", "--xsessions"):
        if option not in words:
            continue
        position = words.index(option)
        if position + 1 >= len(words):
            raise ConfigError(f"the {group.name} group gives {option} no directory")
        directories += [PurePosixPath(path) for path in words[position + 1].split(":")]
    if any(not path.is_absolute() for path in directories):
        raise ConfigError(f"the {group.name} group names a relative session directory")
    desktop = catalog.get(config.packages.desktop)
    provider = desktop.packages[0] if desktop is not None and desktop.packages else ""
    return [
        VerifyPackagePaths(package=GREETD_PACKAGE, paths=(GREETD_CONFIG,)),
        VerifyCommandOptions(
            package=TUIGREET_PACKAGE,
            command=TUIGREET_COMMAND,
            options=options,
        ),
        VerifySessionDirectories(package=provider, paths=tuple(directories)),
        UpdateGreetdConfig(command=command),
    ]


def build(config: InstallConfig, catalog: Catalog) -> list[Operation]:
    chosen = groups(config, catalog)
    _check_repositories(config, chosen)
    conflict = _framework_conflict(chosen) or _driver_conflict(config, chosen)
    if conflict:
        raise ValidationFailed(conflict)
    return _operations(config, catalog, chosen)


def _operations(
    config: InstallConfig, catalog: Catalog, chosen: tuple[Group, ...]
) -> list[Operation]:
    operations: list[Operation] = []
    for group in chosen:
        if group.packages:
            operations.append(
                Emerge(
                    stage=Stage.PACKAGES,
                    packages=group.packages,
                    summary=f"install the {group.name} group",
                    requester=f"the `{group.name}` group",
                )
            )
            if group.name == "console" and KBD_PACKAGE in group.packages:
                operations.append(
                    VerifyPackagePaths(
                        package=KBD_PACKAGE,
                        paths=(console_font_path(CONSOLE_FONTS[config.system.console_font]),),
                    )
                )
        if group.package_use:
            operations.append(WriteGroupUse(group=group.name, lines=group.package_use))
        if group.accept_keywords:
            operations.append(
                WriteGroupKeywords(group=group.name, lines=group.accept_keywords)
            )
        if group.user_services and config.system.init is InitSystem.SYSTEMD:
            operations.append(
                EnableUserUnits(group=group.name, units=group.user_services)
            )
        for wanted in group.files:
            operations += _group_file_operations(config, catalog, group, wanted)
        if group.display_manager:
            operations += _display_manager(
                group.display_manager, group.packages, config.system.init
            )
        named = (
            group.systemd_services
            if group.systemd_services and config.system.init is InitSystem.SYSTEMD
            else group.services
        )
        for service in named:
            # In this stage, not the system one: the unit does not exist until
            # the package that ships it is merged.
            operations.append(
                EnableService(stage=Stage.PACKAGES, service=service, init=config.system.init)
            )
    # After the groups: `rc-update` refuses a service whose package is absent,
    # and both of these arrive as dependencies of the desktop above.
    operations += _session_services(config)
    operations += _input_method(config, chosen)
    if config.packages.extra:
        operations.append(
            Emerge(
                stage=Stage.PACKAGES,
                packages=config.packages.extra,
                summary="install the extra packages",
            )
        )
    extra_groups = _required_user_groups(chosen)
    # Every account, not the first one: two people on one machine both use the
    # sound server, and only the first was put in `pipewire`.
    #
    # Last: `acct-group/<name>` is a dependency of the package that needs the
    # group, so `usermod` before those merges fails on a group that does not
    # exist and stops the install with the disks written.
    for account in config.system.users if extra_groups else ():
        operations.append(AddUserToGroups(user=account.name, groups=extra_groups))
    return operations


def groups(config: InstallConfig, catalog: Catalog) -> tuple[Group, ...]:
    names = [
        config.packages.desktop,
        *config.packages.graphics,
        config.packages.display_manager,
        *config.packages.applications,
    ]
    found: list[Group] = []
    for name in names:
        if not name:
            continue
        group = catalog.get(name)
        if group is None:
            raise ConfigError(f"no package group named {name!r}; the catalog has {_known(catalog)}")
        found.append(group)
    return tuple((*found, *_frameworks_behind(found, catalog)))


#: The group that provides each input framework: the runtime, the toolkit
#: modules and the configuration tool. An engine group ships only the engine.
FRAMEWORK_GROUPS: Final[dict[str, str]] = {"fcitx": "fcitx5", "ibus": "ibus"}


def _frameworks_behind(chosen: Sequence[Group], catalog: Catalog) -> tuple[Group, ...]:
    """The framework each selected engine needs, when it was not selected too.

    `rime` on its own merged `app-i18n/fcitx-rime` and nothing else: no
    `app-i18n/fcitx`, and no `fcitx-gtk` or `fcitx-qt`, so no Gtk or Qt
    application could reach the engine that had just been installed.
    """
    have = {group.name for group in chosen}
    wanted: list[Group] = []
    for group in chosen:
        name = FRAMEWORK_GROUPS.get(group.input_framework, "")
        if not name or name in have or name in {one.name for one in wanted}:
            continue
        framework = catalog.get(name)
        if framework is not None:
            wanted.append(framework)
    return tuple(wanted)


def _check_repositories(config: InstallConfig, chosen: tuple[Group, ...]) -> None:
    """A group whose packages live in an overlay needs that overlay selected.

    Checked here rather than at emerge time, which is an hour into an install
    that has already partitioned the disks.
    """
    have = {overlay.name for overlay in config.portage.overlays}
    for group in chosen:
        missing = [name for name in group.repositories if name not in have]
        if missing:
            raise ConfigError(
                f"the {group.name} group needs the {', '.join(missing)} overlay, "
                "which this configuration does not add"
            )


def required_user_groups(config: InstallConfig, catalog: Catalog) -> tuple[str, ...]:
    """Groups a chosen package group needs the account to be in.

    Only what is not already handed to every account: `plan/system.py`'s
    `USER_GROUPS` is the one table for that, and naming `video` again would
    read as nvidia needing something the installer does not already do.
    """
    return _required_user_groups(groups(config, catalog))


def _required_user_groups(chosen: tuple[Group, ...]) -> tuple[str, ...]:
    from .system import USER_GROUPS

    wanted: list[str] = []
    for group in chosen:
        for name in group.user_groups:
            if name not in wanted and name not in USER_GROUPS:
                wanted.append(name)
    return tuple(wanted)


def required_use(config: InstallConfig, catalog: Catalog) -> tuple[str, ...]:
    wanted: list[str] = []
    for group in groups(config, catalog):
        for flag in group.use:
            if flag not in wanted:
                wanted.append(flag)
    return tuple(wanted)


def required_video_cards(config: InstallConfig, catalog: Catalog) -> tuple[str, ...]:
    """What the configuration asks for, then what a driver group adds."""
    wanted = list(config.portage.video_cards)
    for group in groups(config, catalog):
        for card in group.video_cards:
            if card not in wanted:
                wanted.append(card)
    return tuple(wanted)


#: openrc runs every display manager through one init script, which reads the
#: name from its conf.d file. `gui-libs/display-manager-init` is what ships
#: both, and nothing else pulls it in.
DISPLAY_MANAGER_INIT: Final[str] = "gui-libs/display-manager-init"
DISPLAY_MANAGER_CONF: Final[PurePosixPath] = PurePosixPath("/etc/conf.d/display-manager")


#: What a desktop needs running on openrc and gets from systemd for free, as
#: package, service and runlevel. elogind's init script says `before xdm`, but
#: openrc only orders services that are in a runlevel, so declaring it is not
#: the same as enabling it; its ebuild warns against `default` for elogind.
SESSION_PACKAGES: Final[tuple[tuple[str, str, str], ...]] = (
    ("sys-apps/dbus", "dbus", "default"),
    ("sys-auth/elogind", "elogind", "boot"),
)


def _session_services(config: InstallConfig) -> list[Operation]:
    """What a graphical session needs running before anything can start one.

    Neither package is in a stage3 and neither is in `@system`: they arrive as
    dependencies of the desktop, so both are merged here rather than left to
    whichever group happens to pull them, and enabled after that merge.
    """
    if config.system.init is InitSystem.SYSTEMD:
        return []
    if not (config.packages.desktop or config.packages.display_manager):
        return []
    return [
        Emerge(
            stage=Stage.PACKAGES,
            packages=tuple(atom for atom, _, _ in SESSION_PACKAGES),
            summary="install the session bus and the seat manager",
        ),
        *(
            EnableService(
                stage=Stage.PACKAGES, service=service, init=config.system.init, runlevel=runlevel
            )
            for _, service, runlevel in SESSION_PACKAGES
        ),
    ]


#: `lightdm` and `gdm` both carry `REQUIRED_USE="^^ ( elogind systemd )"` with
#: neither flag on by default, so a profile that does not set one refuses the
#: merge. Which one is the init's answer, and requesting the other is worse
#: than requesting none: it is use-masked on that profile.
SEAT_FLAG: Final[dict[InitSystem, str]] = {
    InitSystem.SYSTEMD: "systemd",
    InitSystem.OPENRC: "elogind",
}


#: Display managers whose `REQUIRED_USE` is `^^ ( elogind systemd )`. Read from
#: their ebuilds: `gui-libs/greetd` has `IUSE="selinux"` and nothing else, so a
#: seat flag written for it is a line Portage warns about and drops.
SEAT_FLAG_WANTED: Final[frozenset[str]] = frozenset({"sddm", "gdm", "lightdm"})


#: `sys-auth/pambase` needs the seat flag too, or the PAM stack has no
#: `pam_elogind.so` and the session it starts registers with no seat.
#: `gnome-base/gdm` says so outright with `sys-auth/pambase[elogind?,systemd?]`
#: and refuses the merge; sddm and lightdm merge and come up seatless. The
#: desktop profiles hide this by putting `elogind` in global USE, but the
#: `console` group is built against the base profile, which does not.
PAM_BASE: Final[str] = "sys-auth/pambase"


def _seat_flag(name: str, packages: Sequence[str], init: InitSystem) -> list[Operation]:
    """The manager's own atom takes the flag, and so does the PAM stack."""
    if name not in SEAT_FLAG_WANTED:
        return []
    atom = next((one for one in packages if one.rsplit("/", 1)[-1] == name), "")
    if not atom:
        return []
    flag = SEAT_FLAG[init]
    return [
        WriteGroupUse(group=name, lines=(f"{atom} {flag}", f"{PAM_BASE} {flag}"))
    ]


def _display_manager(name: str, packages: Sequence[str], init: InitSystem) -> list[Operation]:
    if init is InitSystem.SYSTEMD:
        return [
            *_seat_flag(name, packages, init),
            EnableService(stage=Stage.PACKAGES, service=name, init=init),
        ]
    return [
        *_seat_flag(name, packages, init),
        Emerge(
            stage=Stage.PACKAGES,
            packages=(DISPLAY_MANAGER_INIT,),
            summary="install the openrc display manager script",
        ),
        UpdatePackageShellAssignment(
            group=name,
            package=DISPLAY_MANAGER_INIT,
            path=DISPLAY_MANAGER_CONF,
            key="DISPLAYMANAGER",
            value=name,
        ),
        EnableService(stage=Stage.PACKAGES, service="display-manager", init=init),
    ]


def required_licenses(config: InstallConfig, catalog: Catalog) -> tuple[str, ...]:
    """What the configuration accepts, widened by what a chosen group needs."""
    wanted = list(config.portage.accept_license)
    for group in groups(config, catalog):
        for licence in group.accept_license:
            if licence not in wanted:
                wanted.append(licence)
    return tuple(wanted)


def _known(catalog: Catalog) -> str:
    return ", ".join(sorted(catalog)) or "nothing"


def _wayland_file_operations(group: Group, wanted: GroupFile) -> list[Operation]:
    if group.name != "chromium" or wanted.path != CHROMIUM_CONFIG:
        raise ConfigError(f"{wanted.path} has no declared package contract")
    if CHROMIUM_PACKAGE not in group.packages:
        raise ConfigError(f"the {group.name} group does not install {CHROMIUM_PACKAGE}")
    return [
        VerifyPackagePaths(package=CHROMIUM_CONFIG_PACKAGE, paths=(wanted.path,)),
        VerifyPackagePaths(
            package=CHROMIUM_PACKAGE,
            paths=(CHROMIUM_COMMAND,),
        ),
        AppendWaylandFlags(group=group.name, file=wanted),
    ]


def _framework_package(chosen: Sequence[Group], framework: str) -> str:
    for group in chosen:
        if group.input_framework == framework and not group.input_method and group.packages:
            return group.packages[0]
    return ""


def _input_method(config: InstallConfig, chosen: tuple[Group, ...]) -> list[Operation]:
    """Configure one selected framework and every engine belonging to it."""
    engines: list[str] = []
    for group in chosen:
        if group.input_method and group.input_method not in engines:
            engines.append(group.input_method)
    frameworks = [
        group
        for group in chosen
        if group.input_framework and not group.input_method
    ]
    if not engines and not frameworks:
        return []
    framework = _framework(chosen)
    decisions = {
        group.input_configuration
        for group in chosen
        if group.input_configuration
    }
    unknown = decisions - INPUT_CONFIGURATION_STATES
    if unknown:
        raise ConfigError(
            f"unknown input configuration decision: {', '.join(sorted(unknown))}"
        )
    if len(decisions) > 1:
        raise ConfigError("input configuration was both accepted and declined")
    if INPUT_CONFIGURATION_DISABLED in decisions:
        return []
    wayland = any(group.wayland for group in chosen)
    # The launcher is the desktop saying it starts the input method itself,
    # which is what makes the toolkit variables wrong rather than merely
    # unnecessary. Only Plasma declares one.
    driven = any(group.input_method_launcher for group in chosen)
    if not wayland:
        session = Session.X11
    else:
        session = Session.WAYLAND_DRIVEN if driven else Session.WAYLAND_PLAIN
    operations: list[Operation] = [
        WriteInputMethodEnvironment(
            init=config.system.init, framework=framework, session=session
        )
    ]
    if framework == "fcitx":
        schemas: list[str] = []
        for group in chosen:
            schemas += [schema for schema in group.schemas if schema not in schemas]
        homes: list[tuple[PurePosixPath, str]] = [(SKELETON, "")]
        homes += [
            (PurePosixPath(f"/home/{user.name}"), user.name) for user in config.system.users
        ]
        operations.append(
            WriteInputMethodProfile(
                engines=tuple(engines),
                schemas=tuple(schemas),
                layout=xkb_layout(config.system.keymap),
                homes=tuple(homes),
            )
        )
    if framework == "ibus" and config.packages.desktop == GNOME_DESKTOP_GROUP:
        sources = tuple(group.input_source for group in chosen if group.input_source)
        if sources:
            operations.append(
                ConfigureGnomeInputSources(
                    layout=xkb_layout(config.system.keymap), engines=sources
                )
            )
    if wayland:
        for group in chosen:
            # The launcher names fcitx's own desktop entry, so a session that
            # chose ibus was telling KWin to exec a file no package installed.
            if group.input_method_launcher and framework == "fcitx":
                operations += [
                    VerifyPackagePaths(
                        package=_framework_package(chosen, framework),
                        paths=(PurePosixPath(group.input_method_launcher),),
                    ),
                    ConfigureKwinInputMethod(launcher=group.input_method_launcher),
                ]
            for wanted in group.wayland_files:
                operations += _wayland_file_operations(group, wanted)
    return operations


def input_environment(config: InstallConfig, catalog: Catalog) -> tuple[str, ...]:
    """The lines `WriteInputMethodEnvironment` will write, for the panel.

    Derived here rather than restated there: `plan/automatic.py` exists so the
    operator sees what the installer adds, and a second list would drift.
    """
    for one in _input_method(config, groups(config, catalog)):
        if isinstance(one, WriteInputMethodEnvironment):
            return INPUT_ENVIRONMENT[(one.framework, one.session)]
    return ()


def _framework(chosen: Sequence[Group]) -> str:
    """Which framework the chosen engines belong to.

    `validate` refuses two at once, so the first one found is the only one.
    Empty defaults to fcitx: every engine group in the catalog names it, and a
    group that names none is one nobody has classified yet.
    """
    named = [group.input_framework for group in chosen if group.input_framework]
    return named[0] if named else "fcitx"
