"""Desktop profiles and the applications chosen separately from them.

A profile is data, not code: `data/profiles/*.toml` names packages, services and
the repositories they come from, and an application group has the same shape. So
a user can have an input method without a desktop, or a desktop without one.

The catalog is read by `data.py` and passed in, because this layer stays a pure
function of its arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Mapping

from ..errors import ConfigError
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
    #: The fcitx engine this group provides, if it provides one.
    input_method: str = ""
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


@dataclass(frozen=True, kw_only=True)
class WriteInputMethodEnvironment(Operation):
    """`XMODIFIERS` always, the other two only off Wayland.

    A Wayland compositor drives fcitx over the text-input protocol, and setting
    `GTK_IM_MODULE` or `QT_IM_MODULE` there makes the candidate window blink.
    """

    stage: Stage = Stage.PACKAGES
    init: InitSystem
    wayland: bool

    def describe(self) -> str:
        named = (
            "XMODIFIERS and QT_IM_MODULES, since the session is Wayland"
            if self.wayland
            else "XMODIFIERS, GTK_IM_MODULE and QT_IM_MODULE"
        )
        return f"set the input method environment in {ENVIRONMENT_FILE[self.init]}: {named}"

    def apply(self, context: Context) -> None:
        lines = ["XMODIFIERS=@im=fcitx"]
        if self.wayland:
            # Qt 6.7 and later take a fallback list, which covers a toolkit
            # that ships no fcitx module without breaking the ones that do.
            lines.append('QT_IM_MODULES="wayland;fcitx;ibus"')
        else:
            lines += ["GTK_IM_MODULE=fcitx", "QT_IM_MODULE=fcitx"]
        context.write(ENVIRONMENT_FILE[self.init], "\n".join(lines) + "\n")
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


def build(config: InstallConfig, catalog: Catalog) -> list[Operation]:
    _check_repositories(config, catalog)
    operations: list[Operation] = _session_services(config)
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
            operations += _display_manager(group.display_manager, config.system.init)
        for service in group.services:
            # In this stage, not the system one: the unit does not exist until
            # the package that ships it is merged.
            operations.append(
                EnableService(stage=Stage.PACKAGES, service=service, init=config.system.init)
            )
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
        config.packages.graphics,
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


#: What a desktop needs running on openrc and gets from systemd for free.
#: elogind's init script says `before xdm`, but openrc only orders services
#: that are in a runlevel, so declaring it is not the same as enabling it.
OPENRC_SESSION: Final[tuple[tuple[str, str], ...]] = (("dbus", "default"), ("elogind", "boot"))


def _session_services(config: InstallConfig) -> list[Operation]:
    """What a graphical session needs running before anything can start one.

    Emitted for the desktop and not for the display manager: a desktop chosen
    with no manager still needs dbus and elogind, and without them the machine
    boots to a console with a desktop it cannot start.
    """
    if config.system.init is InitSystem.SYSTEMD or not config.packages.desktop:
        return []
    return [
        EnableService(stage=Stage.PACKAGES, service=service, init=config.system.init, runlevel=runlevel)
        for service, runlevel in OPENRC_SESSION
    ]


def _display_manager(name: str, init: InitSystem) -> list[Operation]:
    if init is InitSystem.SYSTEMD:
        return [EnableService(stage=Stage.PACKAGES, service=name, init=init)]
    return [
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
    schemas: list[str] = []
    for group in chosen:
        schemas += [schema for schema in group.schemas if schema not in schemas]
    homes: list[tuple[PurePosixPath, str]] = [(SKELETON, "")]
    homes += [
        (PurePosixPath(f"/home/{user.name}"), user.name) for user in config.system.users
    ]
    return [
        WriteInputMethodEnvironment(
            init=config.system.init,
            wayland=any(group.wayland for group in chosen),
        ),
        WriteInputMethodProfile(
            engines=tuple(engines),
            schemas=tuple(schemas),
            layout=config.system.keymap,
            homes=tuple(homes),
        ),
    ]
