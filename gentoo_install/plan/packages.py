"""Desktop profiles and the applications chosen separately from them.

A profile is data, not code: `data/profiles/*.toml` names packages, services and
the repositories they come from, and an application group has the same shape. So
a user can have an input method without a desktop, or a desktop without one.

The catalog is read by `data.py` and passed in, because this layer stays a pure
function of its arguments.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Mapping, Sequence

from ..errors import ConfigError, ValidationFailed
from ..model.config import InitSystem, InstallConfig
from .operations import Context, Operation, Stage
from .portage import Emerge
from .system import EnableService


@dataclass(frozen=True)
class GroupFile:
    """A file a group has to write, such as a modprobe drop-in."""

    path: PurePosixPath
    content: str


@dataclass(frozen=True)
class Group:
    """One profile or application group, exactly as its TOML file declares it."""

    name: str
    packages: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
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
    #: The input method engine this group provides, if it provides one.
    input_method: str = ""
    #: Which framework that engine belongs to. Two frameworks in one session
    #: fight over the same toolkit modules, so `compat.py` refuses the pair.
    input_framework: str = ""
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


Catalog = Mapping[str, Group]


#: Where a session reads environment variables: systemd at user-session start,
#: openrc through `env-update` into /etc/profile.env.
ENVIRONMENT_FILE: Final[dict[InitSystem, PurePosixPath]] = {
    InitSystem.SYSTEMD: PurePosixPath("/etc/environment.d/90-input-method.conf"),
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


#: The environment each framework needs, by framework, off Wayland and on it.
#: fcitx on Wayland deliberately sets neither toolkit variable: KWin drives it
#: over text-input and setting them makes the candidate window blink. ibus has
#: no such path and needs them in both sessions.
INPUT_ENVIRONMENT: Final[dict[tuple[str, bool], tuple[str, ...]]] = {
    ("fcitx", False): ("XMODIFIERS=@im=fcitx", "GTK_IM_MODULE=fcitx", "QT_IM_MODULE=fcitx"),
    # Qt 6.7 and later take a fallback list, which covers a toolkit that ships
    # no fcitx module without breaking the ones that do.
    ("fcitx", True): ("XMODIFIERS=@im=fcitx", 'QT_IM_MODULES="wayland;fcitx;ibus"'),
    ("ibus", False): ("XMODIFIERS=@im=ibus", "GTK_IM_MODULE=ibus", "QT_IM_MODULE=ibus"),
    ("ibus", True): ("XMODIFIERS=@im=ibus", "GTK_IM_MODULE=ibus", "QT_IM_MODULE=ibus"),
}


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
    """`XMODIFIERS` always, the other two only off Wayland.

    A Wayland compositor drives fcitx over the text-input protocol, and setting
    `GTK_IM_MODULE` or `QT_IM_MODULE` there makes the candidate window blink.
    """

    stage: Stage = Stage.PACKAGES
    init: InitSystem
    framework: str
    wayland: bool

    def _lines(self) -> tuple[str, ...]:
        return INPUT_ENVIRONMENT[(self.framework, self.wayland)]

    def describe(self) -> str:
        named = ", ".join(one.split("=")[0] for one in self._lines())
        return f"set {named} in {ENVIRONMENT_FILE[self.init]} for {self.framework}"

    def apply(self, context: Context) -> None:
        context.write(ENVIRONMENT_FILE[self.init], "\n".join(self._lines()) + "\n")
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


def framework_conflict(config: InstallConfig, catalog: Catalog) -> str:
    """Why the chosen groups cannot be installed together, or empty.

    Two input method frameworks in one session both provide the Gtk and Qt
    modules and both claim `XMODIFIERS`, so whichever loses is installed and
    unreachable. Read by `build` and by the screen that offers the groups, the
    way `compat.py` is read by the validator and by the menu.
    """
    named: dict[str, list[str]] = {}
    for group in groups(config, catalog):
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


def build(config: InstallConfig, catalog: Catalog) -> list[Operation]:
    _check_repositories(config, catalog)
    conflict = framework_conflict(config, catalog)
    if conflict:
        raise ValidationFailed(conflict)
    operations: list[Operation] = []
    for group in groups(config, catalog):
        if group.packages:
            operations.append(
                Emerge(
                    stage=Stage.PACKAGES,
                    packages=group.packages,
                    summary=f"install the {group.name} group",
                )
            )
        if group.package_use:
            operations.append(WriteGroupUse(group=group.name, lines=group.package_use))
        for wanted in group.files:
            operations.append(WriteGroupFile(group=group.name, file=wanted))
        if group.display_manager:
            operations += _display_manager(
                group.display_manager, group.packages, config.system.init
            )
        for service in group.services:
            # In this stage, not the system one: the unit does not exist until
            # the package that ships it is merged.
            operations.append(
                EnableService(stage=Stage.PACKAGES, service=service, init=config.system.init)
            )
    # After the groups: `rc-update` refuses a service whose package is absent,
    # and both of these arrive as dependencies of the desktop above.
    operations += _session_services(config)
    operations += _input_method(config, catalog)
    if config.packages.extra:
        operations.append(
            Emerge(
                stage=Stage.PACKAGES,
                packages=config.packages.extra,
                summary="install the extra packages",
            )
        )
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
    return tuple(found)


def _check_repositories(config: InstallConfig, catalog: Catalog) -> None:
    """A group whose packages live in an overlay needs that overlay selected.

    Checked here rather than at emerge time, which is an hour into an install
    that has already partitioned the disks.
    """
    have = {overlay.name for overlay in config.portage.overlays}
    for group in groups(config, catalog):
        missing = [name for name in group.repositories if name not in have]
        if missing:
            raise ConfigError(
                f"the {group.name} group needs the {', '.join(missing)} overlay, "
                "which this configuration does not add"
            )


def required_repositories(config: InstallConfig, catalog: Catalog) -> tuple[str, ...]:
    """What the selected groups need, so the interface can say so before the
    user commits instead of failing at emerge time."""
    wanted: list[str] = []
    for group in groups(config, catalog):
        for repository in group.repositories:
            if repository not in wanted:
                wanted.append(repository)
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


def _seat_flag(name: str, packages: Sequence[str], init: InitSystem) -> list[Operation]:
    """The one atom of the group that is the manager itself takes the flag; a
    greeter that has no such flag would be a package.use line Portage warns
    about and ignores."""
    atom = next((one for one in packages if one.rsplit("/", 1)[-1] == name), "")
    if not atom:
        return []
    return [WriteGroupUse(group=name, lines=(f"{atom} {SEAT_FLAG[init]}",))]


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
        WriteGroupFile(
            group=name,
            file=GroupFile(path=DISPLAY_MANAGER_CONF, content=f'DISPLAYMANAGER="{name}"\n'),
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


def _input_method(config: InstallConfig, catalog: Catalog) -> list[Operation]:
    """Nothing at all unless a selected group provides an engine."""
    chosen = groups(config, catalog)
    engines: list[str] = []
    for group in chosen:
        if group.input_method and group.input_method not in engines:
            engines.append(group.input_method)
    if not engines:
        return []
    framework = _framework(chosen)
    wayland = any(group.wayland for group in chosen)
    operations: list[Operation] = [
        WriteInputMethodEnvironment(
            init=config.system.init, framework=framework, wayland=wayland
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
    if wayland:
        for group in chosen:
            if group.input_method_launcher:
                operations.append(
                    ConfigureKwinInputMethod(launcher=group.input_method_launcher)
                )
            operations += [
                AppendWaylandFlags(group=group.name, file=one) for one in group.wayland_files
            ]
    return operations


def _framework(chosen: Sequence[Group]) -> str:
    """Which framework the chosen engines belong to.

    `validate` refuses two at once, so the first one found is the only one.
    Empty defaults to fcitx: every engine group in the catalog names it, and a
    group that names none is one nobody has classified yet.
    """
    named = [group.input_framework for group in chosen if group.input_framework]
    return named[0] if named else "fcitx"
