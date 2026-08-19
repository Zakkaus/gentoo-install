# SPDX-License-Identifier: GPL-2.0-or-later
"""The package screens: desktop, graphics, fonts, input method, and the rest.

The closure of the seven screens that choose packages, which share one set of
helpers — `Effects`, `derive_effects`, `settle` and the operator's own
choices. Six names cross outward and none inward; the boundary was computed
the way `tui/partitions.py`'s was, not chosen by subject.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Final, Sequence

from ..errors import ConfigError
from ..i18n import Catalog
from ..model import atoms
from ..model.config import (
    Binhost,
    BinhostChannel,
    InitSystem,
    InstallConfig,
    Networking,
    Overlay,
    PackagesConfig,
    PortageConfig,
)
from ..plan import automatic as automatic_values
from ..plan.fonts import CJK_SANS_PREFERENCE, CjkFontconfigLocale, FontCategory
from ..plan.packages import Catalog as Groups
from ..plan.packages import FRAMEWORK_GROUPS
from ..plan.packages import FONT_CONFIGURATION_DISABLED, FONT_CONFIGURATION_ENABLED
from ..plan.packages import INPUT_CONFIGURATION_DISABLED, INPUT_CONFIGURATION_ENABLED
from ..plan.packages import driver_conflict, framework_conflict
from .context import (
    Context,
    DONE,
    FieldDescriptor,
    ValueKind,
    ValueProvenance,
    ValueSource,
    answers,
    current_menu,
    footer,
    pick,
    say,
    with_gentoo_zh,
)
from .widgets import (
    Accepts,
    Answer,
    Confirm,
    Field,
    Form,
    FormRejected,
    Item,
    Menu,
    MultipleChoiceMenu,
    Outcome,
    Screen,
    TextField,
)

def _profile_was_chosen(config: InstallConfig, groups: Groups) -> bool:
    """Whether the profile is something other than what the current desktop and
    init imply, which is the only evidence the operator set it by hand."""
    implied = desktop_profiles(groups).get(config.packages.desktop)
    if implied is None:
        return True
    return config.portage.profile != _profile_for(implied, config.system.init)


def _profile_for(profile: str, init: InitSystem) -> str:
    """The profile has to follow the init: a systemd profile has `systemd` as a
    path component, and the two disagreeing builds packages for the other."""
    parts = [part for part in profile.split("/") if part != "systemd"]
    if init is InitSystem.SYSTEMD:
        parts.append("systemd")
    return "/".join(parts)
#: What a machine with no desktop is built against. Every other answer names
#: its own profile in `data/profiles/<name>.toml`.


BASE_PROFILE: Final[str] = "default/linux/amd64/23.0"


def desktop_profiles(groups: Groups, profile_paths: Sequence[str] = ()) -> dict[str, str]:
    """The desktops and the profile each is built against, read from the files
    that declare them: a table beside `data/profiles/` meant a desktop added
    there never reached the menu, and one added here installed nothing."""
    release = next(
        (path.split("/")[3] for path in profile_paths if path.startswith("default/linux/amd64/")),
        BASE_PROFILE.split("/")[3],
    )
    prefix = f"default/linux/amd64/{release}"
    found = {"": prefix}
    for name, group in groups.items():
        if group.profile:
            found[name] = group.profile.replace(BASE_PROFILE, prefix, 1)
    return found


def desktop_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """The desktop decides the profile as well as the packages, the same way
    the init system does."""
    translate = context.translate
    detail = {
        "plasma": translate("the session only"),
        "plasma-full": translate("with the KDE application set"),
        "gnome": translate("the session only"),
        "gnome-full": translate("with the GNOME application set"),
    }
    items = [
        Item(
            label=translate("no desktop") if not name else name,
            value=name,
            # Plasma and GNOME both bring `wayland`, and the operator reads
            # that here rather than after choosing.
            detail=detail.get(name, "")
            + _adds(config, context, lambda packages, one: replace(packages, desktop=one), name),
        )
        for name in sorted(desktop_profiles(context.groups, context.profile_paths))
    ]
    menu: Menu[str] = Menu(
        title=translate("Desktop and applications"),
        preamble=(translate("The desktop selects its profile and proposes login and network services."),),
        items=items,
        current=config.packages.desktop,
        footer=footer(translate),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    desktop = answer.unwrap()
    changed = replace(config, packages=replace(config.packages, desktop=desktop))
    if desktop != config.packages.desktop:
        changed = _set_input_configuration(changed, context.groups, None)
    if not _profile_was_chosen(config, context.groups):
        # Only while the profile is still the one the last desktop implied.
        # Overwriting it regardless threw away a profile the operator had
        # picked on purpose, such as no-multilib.
        changed = replace(
            changed,
            portage=replace(
                config.portage,
                profile=_profile_for(
                    desktop_profiles(context.groups, context.profile_paths)[desktop], config.system.init
                ),
            ),
        )
    manager = config.packages.display_manager
    was_proposed = _has_derived(context, ValueKind.DISPLAY_MANAGER, manager)
    kept_manager = (
        manager
        if manager and not was_proposed and desktop != config.packages.desktop
        else ""
    )
    changed = _desktop_proposes(changed, config, context, desktop)
    login = LOGIN_SCREEN.get(desktop, "")
    effects = derive_effects(config, changed, context, kept_manager)
    answered = settle(
        screen,
        context,
        config,
        changed,
        kept_display_manager=kept_manager,
    )
    if answered.chosen:
        _record_derived(context, answered.unwrap(), effects)
    return answered
#: What each desktop's own login screen is. Proposed rather than fixed: the
#: row stays editable, and pulling the login screen out of the desktop profile
#: was right — leaving it empty was not, because the row is then required and
#: red on a menu whose operator has already said which desktop they want.


LOGIN_SCREEN: Final[dict[str, str]] = {
    "plasma": "sddm",
    "plasma-full": "sddm",
    "gnome": "gdm",
    "gnome-full": "gdm",
    "xfce": "lightdm",
}


@dataclass(frozen=True)


class Effects:
    """Derived values changed by one TUI choice."""

    use_flags: tuple[str, ...] = ()
    video_cards: tuple[str, ...] = ()
    user_groups: tuple[str, ...] = ()
    profile: str = ""
    display_manager_changed: bool = False
    networking_changed: bool = False
    kept_display_manager: str = ""
    withdrawn_use: tuple[str, ...] = ()
    withdrawn_video_cards: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        """Whether the choice has a value to confirm."""
        return bool(
            self.use_flags
            or self.video_cards
            or self.user_groups
            or self.profile
            or self.display_manager_changed
            or self.networking_changed
            or self.kept_display_manager
        )

    @property
    def editable_row(self) -> Callable[..., Answer[InstallConfig]] | None:
        """The row containing values that can be inspected after consent."""
        if self.use_flags:
            return use_flags_screen
        if self.video_cards:
            return video_cards_screen
        return None


def derive_effects(
    before: InstallConfig,
    after: InstallConfig,
    context: Context,
    kept_display_manager: str = "",
) -> Effects:
    """Derive every value one choice adds, replaces, or withdraws."""
    return Effects(
        use_flags=_new(automatic_values.use_flags, before, after, context.groups),
        video_cards=_new(automatic_values.video_cards, before, after, context.groups),
        user_groups=_new(automatic_values.user_groups, before, after, context.groups),
        profile=after.portage.profile if after.portage.profile != before.portage.profile else "",
        display_manager_changed=(
            after.packages.display_manager != before.packages.display_manager
        ),
        networking_changed=after.system.networking is not before.system.networking,
        kept_display_manager=kept_display_manager,
        withdrawn_use=tuple(
            one.value
            for one in context.provenance
            if one.kind is ValueKind.USE_FLAG and one.source is ValueSource.DERIVED
        ),
        withdrawn_video_cards=tuple(
            one.value
            for one in context.provenance
            if one.kind is ValueKind.VIDEO_CARD and one.source is ValueSource.DERIVED
        ),
    )


def apply_effects(after: InstallConfig, effects: Effects) -> InstallConfig:
    """Pin additions while removing only values derived by the replaced choice."""
    kept_use = tuple(one for one in after.portage.use if one not in effects.withdrawn_use)
    kept_cards = tuple(
        one for one in after.portage.video_cards if one not in effects.withdrawn_video_cards
    )
    return replace(
        after,
        portage=replace(
            after.portage,
            use=(*kept_use, *effects.use_flags),
            video_cards=(*kept_cards, *effects.video_cards),
        ),
    )


def _desktop_proposes(
    changed: InstallConfig, before: InstallConfig, context: Context, desktop: str
) -> InstallConfig:
    """The login screen and the network manager a desktop implies.

    Only over what the operator has not set: a login screen they picked stands,
    and so does a network manager. The Plasma and GNOME profiles already carry
    `USE=networkmanager`, and nothing moved `system.networking` with it, so the
    installed desktop had the settings panel and not the service behind it.
    """
    if (
        _has_derived(context, ValueKind.DISPLAY_MANAGER, before.packages.display_manager)
    ):
        changed = replace(
            changed,
            packages=replace(changed.packages, display_manager=""),
        )
    if not desktop:
        return changed
    login = LOGIN_SCREEN.get(desktop, "")
    if login and not changed.packages.display_manager and login in context.groups:
        changed = replace(
            changed, packages=replace(changed.packages, display_manager=login)
        )
    if not _has_operator(context, ValueKind.NETWORKING, before.system.networking.value):
        changed = replace(
            changed,
            system=replace(changed.system, networking=Networking.NETWORKMANAGER_WPA),
        )
    return changed


def settle(
    screen: Screen,
    context: Context,
    before: InstallConfig,
    after: InstallConfig,
    kept_display_manager: str = "",
) -> Answer[InstallConfig]:
    """Confirm what a choice changes outside its own row, then write it down.

    Choosing a driver or a desktop moves `VIDEO_CARDS`, `USE` and the profile.
    Deriving those silently at build time meant the operator met them for the
    first time in the installed system, so they are listed here and, once
    confirmed, put into the configuration as the operator's own values. Pinned
    rather than left derived: a value in `portage.use` survives a later change
    to the desktop, and `automatic.use_flags` stops reporting a flag that is
    already there, so nothing appears twice.

    Declining cancels the choice. The flags are not optional extras that come
    with it; they are what makes it work.
    """
    effects = derive_effects(before, after, context, kept_display_manager)
    if not effects.has_changes:
        return Answer(Outcome.CHOSE, after)
    translate = context.translate
    lines = []
    if effects.video_cards:
        lines.append(f"VIDEO_CARDS: {' '.join(effects.video_cards)}")
    if effects.use_flags:
        lines.append(f"USE: {' '.join(effects.use_flags)}")
    if effects.display_manager_changed:
        lines.append(
            f"{translate('Display manager')}: {after.packages.display_manager}"
        )
    elif effects.kept_display_manager:
        lines.append(
            f"{translate('Display manager')}: {effects.kept_display_manager} "
            f"({translate('kept')})"
        )
    if effects.networking_changed:
        lines.append(f"{translate('Network')}: {after.system.networking.value}")
    if effects.user_groups:
        # Not pinned into the account: `AddUserToGroups` derives them from the
        # same catalog at build time, so the account is put in them whether or
        # not one exists when the driver is chosen.
        lines.append(f"{translate('Extra groups')}: {' '.join(effects.user_groups)}")
    if effects.profile:
        lines.append(f"{translate('Profile')}: {effects.profile}")
    # A third answer only when there is something to open. The row is derived
    # from what actually changed: with a profile and no flags it offered `Yes,
    # and open VIDEO_CARDS`, which edits a value this choice did not touch.
    where = effects.editable_row
    named = translate("USE flags") if effects.use_flags else translate("VIDEO_CARDS")
    # Yes first and preselected. Declining cancels the desktop the operator
    # just chose, and these values are what makes it work rather than extras
    # that come with it, so the cursor starting on `No` offered the refusal as
    # the default answer to a question the choice itself had already implied.
    items = [
        Item(label=translate("Yes"), value="yes"),
        Item(label=translate("No"), value="no"),
    ]
    if where is not None:
        # The row the values land on is somewhere else in the menu, and an
        # operator who wants to look at them now would otherwise have to leave,
        # find it, and remember what they were checking.
        items.append(
            Item(
                label=f"{translate('Yes, and open')} {named}",
                value="open",
                detail=translate("editable"),
            )
        )
    asked: Menu[str] = Menu(
        title=translate("This choice also sets"),
        preamble=tuple(lines),
        items=items,
        footer=footer(translate),
        current="yes",
    )
    answered = asked.run(screen)
    if not answered.chosen or answered.unwrap() == "no":
        return Answer(Outcome.BACK, before)
    # Pinned without what the choice being replaced had derived. Adding to
    # `portage.use` and never taking anything out meant switching from Plasma
    # to GNOME left `qt6` and `sddm` behind and read as
    # `wayland qt6 networkmanager sddm gnome gtk`. What the operator typed is
    # not derivable and stays; what the last choice derived goes with it.
    pinned = apply_effects(after, effects)
    if answered.unwrap() == "open" and where is not None:
        opened = where(screen, pinned, context)
        if opened.chosen:
            return Answer(Outcome.CHOSE, opened.unwrap())
        if opened.outcome is Outcome.CANCELLED:
            # Cancelling reaches the application's leave confirmation. Reading
            # it as `keep what was pinned` committed a desktop and a profile
            # the operator had just refused.
            return Answer(Outcome.CANCELLED)
    return Answer(Outcome.CHOSE, pinned)


def _has_derived(context: Context, kind: ValueKind, value: str) -> bool:
    """Whether this value came from the current automatic proposal."""
    return ValueProvenance(kind, value, ValueSource.DERIVED) in context.provenance


def _has_operator(context: Context, kind: ValueKind, value: str) -> bool:
    """Whether the operator selected this value in its own row."""
    return ValueProvenance(kind, value, ValueSource.OPERATOR) in context.provenance


def _record_derived(context: Context, after: InstallConfig, effects: Effects) -> None:
    """Replace the previous choice's derived values with the new choice's."""
    context.provenance = {
        one for one in context.provenance if one.source is not ValueSource.DERIVED
    }
    context.provenance.update(
        ValueProvenance(ValueKind.USE_FLAG, value, ValueSource.DERIVED)
        for value in effects.use_flags
    )
    context.provenance.update(
        ValueProvenance(ValueKind.VIDEO_CARD, value, ValueSource.DERIVED)
        for value in effects.video_cards
    )
    if effects.networking_changed:
        context.provenance.add(
            ValueProvenance(
                ValueKind.NETWORKING,
                after.system.networking.value,
                ValueSource.DERIVED,
            )
        )
    if effects.display_manager_changed:
        context.provenance.add(
            ValueProvenance(
                ValueKind.DISPLAY_MANAGER,
                after.packages.display_manager,
                ValueSource.DERIVED,
            )
        )


def _record_operator(context: Context, kind: ValueKind, values: Sequence[str]) -> None:
    """Mark values edited in their own row as operator choices."""
    kept = {
        one
        for one in context.provenance
        if one.kind is not kind or one.source is not ValueSource.DERIVED
    }
    context.provenance = kept | {
        ValueProvenance(kind, value, ValueSource.OPERATOR) for value in values
    }


def _new(
    derive: Callable[[InstallConfig, Groups], tuple[automatic_values.Added, ...]],
    before: InstallConfig,
    after: InstallConfig,
    groups: Groups,
) -> tuple[str, ...]:
    """What the change added, in order, without what was already there.

    Only what the same function said before. Unioning `portage.use` as well
    crossed two namespaces: `pipewire` is both a USE flag and the group its
    ebuild asks the account to join, so pinning the flag hid the group. Each
    `derive` already drops what the operator typed.
    """
    had = {one.value for one in derive(before, groups)}
    return tuple(one.value for one in derive(after, groups) if one.value not in had)
#: The graphics groups, in the order the menu lists them, and what each is
#: for. Kept out of the applications list: a driver is one choice, not a set of
#: things to tick.


GRAPHICS: tuple[tuple[str, str], ...] = (
    ("", "no driver package: i915, amdgpu, radeon and nouveau are in the kernel"),
    ("intel", "i915 and xe, with the firmware they need"),
    ("amdgpu", "GCN 1.2 and newer"),
    ("radeon", "AMD up to Sea Islands"),
    ("nouveau", "the in-kernel NVIDIA driver"),
    ("nvidia", "proprietary, accepts its own licence for its own packages, and blacklists nouveau itself"),
    ("virtual-machine", "virtio-gpu, QXL and VMware, with fbdev left as the fallback"),
)
#: The display managers. A desktop no longer names one, because which login
#: screen to run is a decision of its own.


DISPLAY_MANAGERS: tuple[tuple[str, str], ...] = (
    ("", "a text console login"),
    ("sddm", "the one Plasma expects"),
    ("gdm", "the one GNOME expects, and it installs gnome-shell whatever you run"),
    ("lightdm", "the one Xfce expects"),
    ("greetd", "a console greeter"),
)


def graphics_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Which drivers, which is what VIDEO_CARDS and the firmware follow.

    More than one can be ticked: a hybrid machine has more than one adapter,
    and an AMD laptop with an NVIDIA card needs `amdgpu radeonsi nvidia`. Each
    row says what it adds, so the line the ticks build is readable before any
    of them is pressed.
    """
    translate = context.translate
    named = tuple((name, reason) for name, reason in GRAPHICS if name)
    items = [
        Item(
            label=name,
            value=name,
            detail=translate(reason)
            + _adds(config, context, lambda packages, one: replace(packages, graphics=(one,)), name),
        )
        for name, reason in named
    ]
    ticked = set(config.packages.graphics)
    while True:
        menu: MultipleChoiceMenu[str] = MultipleChoiceMenu(
            title=translate("Graphics"),
            preamble=(translate("The drivers set VIDEO_CARDS and install required firmware or packages."),),
            items=items,
            selected={index for index, item in enumerate(items) if item.value in ticked},
            footer=footer(translate),
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        chosen = replace(
            config, packages=replace(config.packages, graphics=tuple(answer.unwrap()))
        )
        # Checked here rather than at the Install row: the conflict is between
        # two ticks on this screen and this is where either can be unticked.
        clash = driver_conflict(chosen, context.groups)
        if not clash:
            return settle(screen, context, config, chosen)
        ticked = set(answer.unwrap())
        say(screen, context, translate(clash))


def display_manager_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    # A manager with no desktop installs a login screen that has no session to
    # start, so every entry but `none` says to pick a desktop first.
    without = "" if config.packages.desktop else context.translate("choose a desktop first")
    chosen = _one_group(
        screen,
        config,
        context,
        "Display manager",
        DISPLAY_MANAGERS,
        lambda packages, name: replace(packages, display_manager=name),
        current=config.packages.display_manager,
        unavailable=lambda name: without if name else "",
        say_what_it_adds=True,
    )
    if not chosen.chosen:
        return chosen
    answered = settle(screen, context, config, chosen.unwrap())
    if answered.chosen:
        _record_operator(
            context,
            ValueKind.DISPLAY_MANAGER,
            (answered.unwrap().packages.display_manager,),
        )
    return answered


def _needs_an_overlay(
    wanted: Sequence[str], have: set[str], translate: Catalog
) -> str:
    """Why a group cannot be ticked: its packages are in no repository this
    configuration adds. Said here rather than at the Install row, because this
    is the screen the tick is on."""
    missing = [name for name in wanted if name not in have]
    if not missing:
        return ""
    return f"{translate('needs the overlay')} {', '.join(missing)}"


def _one_group(
    screen: Screen,
    config: InstallConfig,
    context: Context,
    title: str,
    offered: tuple[tuple[str, str], ...],
    apply: Callable[[PackagesConfig, str], PackagesConfig],
    current: str,
    unavailable: Callable[[str], str] = lambda name: "",
    say_what_it_adds: bool = False,
) -> Answer[InstallConfig]:
    """A row that holds one group name, drawn from a table of them."""
    translate = context.translate
    answer = current_menu(
        screen,
        context,
        translate(title),
        [
            Item(
                label=name or translate("none"),
                value=name,
                detail=translate(reason) + _adds(config, context, apply, name)
                if say_what_it_adds
                else translate(reason),
                disabled_because=unavailable(name),
            )
            for name, reason in offered
        ],
        current,
    )
    if not answer.chosen:
        return Answer(answer.outcome)
    return Answer(
        Outcome.CHOSE, replace(config, packages=apply(config.packages, answer.unwrap()))
    )


def _adds(
    config: InstallConfig,
    context: Context,
    apply: Callable[[PackagesConfig, str], PackagesConfig],
    name: str,
) -> str:
    """What choosing this row would put in `make.conf`, on the row itself.

    `settle` asks the same question after the choice, and the panel lists the
    values on the USE row afterwards. This is the third place, and the earliest
    one: an operator comparing `nouveau` against `nvidia` reads what each costs
    without having to pick one to find out.
    """
    would = replace(config, packages=apply(config.packages, name))
    effects = derive_effects(config, would, context)
    added = (*effects.video_cards, *effects.use_flags)
    return f" (+{' '.join(added)})" if added else ""


FRAMEWORK_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("fcitx", "Fcitx 5"),
    ("ibus", "IBus"),
)


def input_method_groups(groups: Groups) -> tuple[str, ...]:
    """Framework and engine groups, classified by their catalog metadata."""
    return tuple(sorted(name for name, group in groups.items() if group.input_framework))


def input_framework_groups(groups: Groups) -> tuple[str, ...]:
    """Framework providers from the plan layer's canonical registry."""
    return tuple(
        sorted(name for name in FRAMEWORK_GROUPS.values() if name in groups)
    )


def input_engine_groups(groups: Groups, framework: str) -> tuple[str, ...]:
    """Selectable engines belonging to one framework."""
    providers = set(input_framework_groups(groups))
    return tuple(
        sorted(
            name
            for name, group in groups.items()
            if group.input_framework == framework
            and group.input_method
            and name not in providers
        )
    )


def input_engine_sections(
    groups: Groups, framework: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Engine groups under the language each catalog entry declares."""
    names = input_engine_groups(groups, framework)
    languages = sorted({groups[name].input_language for name in names})
    return tuple(
        (
            language,
            tuple(name for name in names if groups[name].input_language == language),
        )
        for language in languages
        if language
    )


def _selected_input_framework(config: InstallConfig, groups: Groups) -> str:
    frameworks = set(input_framework_groups(groups))
    selected = [
        name for name in config.packages.applications if name in frameworks
    ]
    if len(selected) > 1:
        raise ConfigError("more than one input framework is selected")
    return selected[0] if selected else ""


def select_input_framework(
    config: InstallConfig, groups: Groups, framework_group: str
) -> InstallConfig:
    """Choose one framework and discard every earlier framework and engine."""
    frameworks = set(input_framework_groups(groups))
    if framework_group and framework_group not in frameworks:
        raise ConfigError(f"{framework_group!r} is not an input framework group")
    current = _selected_input_framework(config, groups)
    if current == framework_group:
        return config
    input_groups = set(input_method_groups(groups))
    kept = tuple(
        name for name in config.packages.applications if name not in input_groups
    )
    chosen = (framework_group,) if framework_group else ()
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *chosen)),
    )


def select_input_engines(
    config: InstallConfig, groups: Groups, engine_groups: Sequence[str]
) -> InstallConfig:
    """Select engines only when their framework is already selected."""
    framework_group = _selected_input_framework(config, groups)
    if engine_groups and not framework_group:
        raise ConfigError("an input engine needs its framework selected first")
    framework = groups[framework_group].input_framework if framework_group else ""
    offered = set(input_engine_groups(groups, framework))
    wrong = [name for name in engine_groups if name not in offered]
    if wrong:
        raise ConfigError(
            f"the {', '.join(wrong)} engine groups do not belong to {framework}"
        )
    every_engine = {
        name
        for name in input_method_groups(groups)
        if name not in input_framework_groups(groups)
    }
    kept = tuple(
        name for name in config.packages.applications if name not in every_engine
    )
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *engine_groups)),
    )


def cjk_font_groups(groups: Groups) -> tuple[str, ...]:
    """Font groups classified by declared family metadata."""
    return tuple(sorted(name for name, group in groups.items() if group.font_family))


def font_sections(groups: Groups) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Font groups in the category order that also owns generic aliases."""
    return tuple(
        (
            category.heading,
            tuple(
                name
                for name in cjk_font_groups(groups)
                if groups[name].font_category == category.catalog
            ),
        )
        for category in FontCategory
        if any(
            groups[name].font_category == category.catalog
            for name in cjk_font_groups(groups)
        )
    )


def select_cjk_fonts(
    config: InstallConfig,
    groups: Groups,
    chosen: Sequence[str],
    preferred: Sequence[str],
) -> InstallConfig:
    """Store each generic's preferred font before other installed fonts."""
    offered = set(cjk_font_groups(groups))
    wrong = [name for name in chosen if name not in offered]
    if wrong:
        raise ConfigError(f"the {', '.join(wrong)} groups do not provide CJK fonts")
    missing = [name for name in preferred if name not in chosen]
    if missing:
        raise ConfigError("a preferred font must also be installed")
    unsupported = [name for name in preferred if not groups[name].font_cjk]
    if unsupported:
        raise ConfigError("a preferred CJK font must provide CJK glyphs")
    generics = [
        FontCategory.selected(groups[name].font_category).generic for name in preferred
    ]
    if len(generics) != len(set(generics)):
        raise ConfigError("only one font may be preferred for each generic")
    fonts = set(cjk_font_groups(groups))
    kept = tuple(
        name for name in config.packages.applications if name not in fonts
    )
    ordered = (*preferred, *(name for name in chosen if name not in preferred))
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *ordered)),
    )


def preferred_font_groups(config: InstallConfig, groups: Groups) -> tuple[str, ...]:
    """The first selected font for each generic owns its preferred mark."""
    selected = [
        name for name in config.packages.applications if name in cjk_font_groups(groups)
    ]
    found: list[str] = []
    generics: set[str] = set()
    for name in selected:
        if not groups[name].font_cjk:
            continue
        generic = FontCategory.selected(groups[name].font_category).generic
        if generic not in generics:
            generics.add(generic)
            found.append(name)
    return tuple(found)


def font_configuration_group(groups: Groups, decision: str) -> str:
    names = [
        name for name, group in groups.items() if group.font_configuration == decision
    ]
    if len(names) != 1:
        raise ConfigError(
            f"the catalog must declare exactly one {decision} font configuration group"
        )
    return names[0]


def input_configuration_group(groups: Groups, decision: str) -> str:
    names = [
        name
        for name, group in groups.items()
        if group.input_configuration == decision
    ]
    if len(names) != 1:
        raise ConfigError(
            f"the catalog must declare exactly one {decision} input configuration group"
        )
    return names[0]


def _set_font_configuration(
    config: InstallConfig, groups: Groups, enabled: bool | None
) -> InstallConfig:
    accepted = font_configuration_group(groups, FONT_CONFIGURATION_ENABLED)
    declined = font_configuration_group(groups, FONT_CONFIGURATION_DISABLED)
    decisions = {accepted, declined}
    kept = tuple(
        name for name in config.packages.applications if name not in decisions
    )
    chosen = () if enabled is None else ((accepted,) if enabled else (declined,))
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *chosen)),
    )


def _set_input_configuration(
    config: InstallConfig, groups: Groups, enabled: bool | None
) -> InstallConfig:
    accepted = input_configuration_group(groups, INPUT_CONFIGURATION_ENABLED)
    declined = input_configuration_group(groups, INPUT_CONFIGURATION_DISABLED)
    decisions = {accepted, declined}
    kept = tuple(
        name for name in config.packages.applications if name not in decisions
    )
    chosen = () if enabled is None else ((accepted,) if enabled else (declined,))
    return replace(
        config,
        packages=replace(config.packages, applications=(*kept, *chosen)),
    )


def _consent_screen(
    screen: Screen,
    config: InstallConfig,
    context: Context,
    title: str,
    summary: str,
) -> Answer[InstallConfig]:
    translate = context.translate
    declined = font_configuration_group(
        context.groups, FONT_CONFIGURATION_DISABLED
    )
    answer = Menu(
        title=translate(title),
        preamble=(summary,),
        items=[
            Item(label=translate("Yes"), value="yes"),
            Item(label=translate("No"), value="no"),
        ],
        current=("no" if declined in config.packages.applications else "yes"),
        footer=footer(translate),
    ).run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    edited = _set_font_configuration(
        config, context.groups, answer.unwrap() == "yes"
    )
    return Answer(Outcome.CHOSE, edited)


def _input_consent_screen(
    screen: Screen,
    config: InstallConfig,
    context: Context,
    summary: str,
) -> Answer[InstallConfig]:
    translate = context.translate
    declined = input_configuration_group(
        context.groups, INPUT_CONFIGURATION_DISABLED
    )
    answer = Menu(
        title=translate("Input method configuration"),
        preamble=(summary,),
        items=[
            Item(label=translate("Yes"), value="yes"),
            Item(label=translate("No"), value="no"),
        ],
        current=(
            "no" if declined in config.packages.applications else "yes"
        ),
        footer=footer(translate),
    ).run(screen)
    if not answer.chosen:
        return Answer(answer.outcome)
    edited = _set_input_configuration(
        config, context.groups, answer.unwrap() == "yes"
    )
    return Answer(Outcome.CHOSE, edited)


def _input_configuration_summary(
    config: InstallConfig, groups: Groups, framework: str, translate: Catalog
) -> str:
    selected = [
        groups[name]
        for name in config.packages.applications
        if name in groups and groups[name].input_method
    ]
    engines = ", ".join(group.input_method for group in selected)
    subject = engines or translate("the selected framework")
    desktop = groups.get(config.packages.desktop)
    if (
        framework == "fcitx"
        and engines
        and desktop is not None
        and desktop.input_method_launcher
    ):
        return translate(
            "Write /etc/xdg/kwinrc and the Fcitx profile for: {engines}."
        ).format(
            engines=subject
        )
    sources = ", ".join(group.input_source for group in selected if group.input_source)
    if framework == "ibus" and config.packages.desktop == "gnome" and sources:
        return translate(
            "Write /etc/dconf/db/local.d/00-gentoo-install-input-sources with "
            "IBus engines: {engines}."
        ).format(engines=sources)
    if framework == "fcitx":
        return translate(
            "Write the Fcitx profile and input environment for: {engines}."
        ).format(engines=subject)
    return translate("Write the IBus input environment.")


def input_method_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    translate = context.translate
    frameworks = input_framework_groups(context.groups)
    labels: dict[str, str] = dict(FRAMEWORK_LABELS)
    framework_items = [Item(label=translate("none"), value="")]
    framework_items += [
        Item(
            label=translate(labels.get(context.groups[name].input_framework, name)),
            value=name,
        )
        for name in frameworks
    ]
    framework_answer = Menu(
        title=translate("Input framework"),
        items=framework_items,
        current=_selected_input_framework(config, context.groups),
        footer=footer(translate),
    ).run(screen)
    if not framework_answer.chosen:
        return Answer(framework_answer.outcome)
    with_framework = select_input_framework(
        config, context.groups, framework_answer.unwrap()
    )
    framework_group = _selected_input_framework(with_framework, context.groups)
    if not framework_group:
        without_configuration = _set_input_configuration(
            with_framework, context.groups, None
        )
        return settle(screen, context, config, without_configuration)
    framework = context.groups[framework_group].input_framework
    sections = input_engine_sections(context.groups, framework)
    names = tuple(name for _, section in sections for name in section)
    have = {overlay.name for overlay in config.portage.overlays}
    items = [
        Item(
            label=translate(context.groups[name].label or name),
            value=name,
            heading=translate(language) if offset == 0 else "",
            disabled_because=_needs_an_overlay(
                context.groups[name].repositories, have, translate
            ),
        )
        for language, section in sections
        for offset, name in enumerate(section)
    ]
    selected = set(with_framework.packages.applications) & set(names)
    engine_answer = MultipleChoiceMenu(
        title=translate("Input engines"),
        items=items,
        selected={
            index for index, item in enumerate(items) if item.value in selected
        },
        footer=footer(translate),
    ).run(screen)
    if not engine_answer.chosen:
        return Answer(engine_answer.outcome)
    edited = select_input_engines(
        with_framework, context.groups, tuple(engine_answer.unwrap())
    )
    summary = _input_configuration_summary(
        edited, context.groups, framework, translate
    )
    consented = _input_consent_screen(screen, edited, context, summary)
    if not consented.chosen:
        return Answer(consented.outcome)
    return settle(screen, context, config, consented.unwrap())


def cjk_fonts_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    translate = context.translate
    sections = font_sections(context.groups)
    names = tuple(name for _, section in sections for name in section)
    have = {overlay.name for overlay in config.portage.overlays}
    items = [
        Item(
            label=translate(context.groups[name].label or name),
            value=name,
            heading=translate(heading) if offset == 0 else "",
            preference_group=(
                FontCategory.selected(context.groups[name].font_category).generic
                if context.groups[name].font_cjk
                else ""
            ),
            disabled_because=_needs_an_overlay(
                context.groups[name].repositories, have, translate
            ),
        )
        for heading, section in sections
        for offset, name in enumerate(section)
    ]
    selected = [name for name in config.packages.applications if name in names]
    preferred = set(preferred_font_groups(config, context.groups))
    font_menu = MultipleChoiceMenu(
        title=translate("Fonts"),
        items=items,
        tri_state=True,
        selected={
            index for index, item in enumerate(items) if item.value in selected
        },
        preferred={
            index for index, item in enumerate(items) if item.value in preferred
        },
        footer="  ".join(
            (
                f"[-] {translate('installed')}",
                f"[x] {translate('installed and preferred')}",
                translate("one preferred per generic"),
                footer(translate),
            )
        ),
    )
    font_answer = font_menu.run(screen)
    if not font_answer.chosen:
        return Answer(font_answer.outcome)
    chosen = tuple(font_answer.unwrap())
    chosen_preferred = tuple(
        items[index].value for index in sorted(font_menu.preferred)
    )
    edited = select_cjk_fonts(config, context.groups, chosen, chosen_preferred)
    locale = CjkFontconfigLocale.selected(edited.system.locale)
    if not chosen or locale is None:
        without_configuration = _set_font_configuration(
            edited, context.groups, None
        )
        return settle(screen, context, config, without_configuration)
    preferred_sans = next(
        (
            name
            for name in chosen_preferred
            if FontCategory.selected(context.groups[name].font_category).generic
            == "sans-serif"
        ),
        "",
    )
    leading_sans = (
        locale.resolve(context.groups[preferred_sans].font_family)
        if preferred_sans
        else locale.face
    )
    summary = translate("Write {path}; {face} will lead sans-serif.").format(
        path=CJK_SANS_PREFERENCE, face=leading_sans
    )
    consented = _consent_screen(
        screen,
        edited,
        context,
        "Font configuration",
        summary,
    )
    if not consented.chosen:
        return Answer(consented.outcome)
    return settle(screen, context, config, consented.unwrap())


def packages_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    translate = context.translate
    # A desktop, a driver and a display manager each decide something else as
    # well, so each has its own screen and none is a row to tick here.
    elsewhere = (
        set(desktop_profiles(context.groups, context.profile_paths))
        | {name for name, _ in GRAPHICS}
        | {name for name, _ in DISPLAY_MANAGERS}
        | set(input_method_groups(context.groups))
        | set(cjk_font_groups(context.groups))
    )
    names = sorted(name for name in context.groups if name not in elsewhere)
    have = {overlay.name for overlay in config.portage.overlays}
    items = [
        Item(
            label=name,
            value=name,
            detail=" ".join(context.groups[name].packages)
            + _adds(
                config,
                context,
                lambda packages, one: replace(
                    packages, applications=(*packages.applications, one)
                ),
                name,
            ),
            disabled_because=_needs_an_overlay(context.groups[name].repositories, have, translate),
        )
        for name in names
    ]
    chosen_already = set(config.packages.applications)
    while True:
        menu: MultipleChoiceMenu[str] = MultipleChoiceMenu(
            title=translate("Applications"),
            preamble=(translate("These packages supplement the desktop selection."),),
            items=items,
            selected={index for index, item in enumerate(items) if item.value in chosen_already},
            footer=footer(translate),
        )
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        chosen = answer.unwrap()
        kept = tuple(name for name in config.packages.applications if name in elsewhere)
        edited = replace(
            config,
            packages=replace(config.packages, applications=(*kept, *chosen)),
        )
        # Checked here rather than at the Install row: the conflict is between
        # two ticks on this screen and this is where either can be unticked.
        clash = framework_conflict(edited, context.groups)
        if not clash:
            return settle(screen, context, config, edited)
        chosen_already = set(chosen)
        say(screen, context, clash)


def extra_packages_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """Any atom the operator wants, typed in.

    Only the syntax is checked here. Whether it resolves is a question for the
    target's repositories, which `VerifyPackages` asks once the tree is synced;
    the live medium often carries no repository at all.
    """
    translate = context.translate
    while True:
        typed = TextField(
            title=translate("Packages to install, separated by spaces"),
            value=" ".join(config.packages.extra),
            footer=footer(translate),
        ).run(screen)
        if not typed.chosen:
            return Answer(typed.outcome)
        good, bad = atoms.split(typed.unwrap())
        if bad:
            informed = say(screen, context, f"{translate('Not a package name')}: {' '.join(bad)}")
            if not informed.chosen:
                return Answer(informed.outcome)
            continue
        return Answer(
            Outcome.CHOSE, replace(config, packages=replace(config.packages, extra=good))
        )


def _typed_beside_automatic(
    screen: Screen,
    context: Context,
    *,
    title: str,
    prompt: str,
    typed: tuple[str, ...],
    automatic: tuple[automatic_values.Added, ...],
    accepts: Callable[[str], tuple[tuple[str, ...], tuple[str, ...]]],
    rejected: str,
) -> Answer[tuple[str, ...]]:
    """One editable list, and under it what the installer adds by itself.

    The automatic rows are drawn disabled with their reason: an operator who
    can see that `root=` and `rd.luks.uuid=` are already handled stops adding a
    second copy of them, and one who cannot see it finds them in the installed
    system with nothing to attribute them to.
    """
    translate = context.translate
    while True:
        items: list[Item[str]] = [
            Item(
                label=prompt,
                value="edit",
                detail=" ".join(typed) or translate("none"),
            )
        ]
        items += [
            Item(
                label=one.value,
                value="",
                disabled_because=translate("added for you"),
                detail=(
                    f"{translate(one.because)} ({one.source})"
                    if one.source
                    else translate(one.because)
                ),
            )
            for one in automatic
        ]
        menu: Menu[str] = Menu(title=title, items=items, footer=footer(translate))
        answer = menu.run(screen)
        if not answer.chosen:
            return Answer(answer.outcome)
        entered = TextField(
            title=prompt,
            value=" ".join(typed),
            footer=footer(translate),
        ).run(screen)
        if not entered.chosen:
            return Answer(entered.outcome)
        good, bad = accepts(entered.unwrap())
        if bad:
            informed = say(screen, context, f"{rejected}: {' '.join(bad)}")
            if not informed.chosen:
                return Answer(informed.outcome)
            continue
        return Answer(Outcome.CHOSE, good)


def use_flags_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """`USE` in `make.conf`, beside what the chosen groups already ask for.

    A flag typed here is appended after the profile's own, so `-foo` turns off
    something the profile set. The groups' flags are listed rather than merged
    into the field: an operator who deletes `wayland` from a field that was
    pre-filled with it has silently overridden their desktop.
    """
    answer = _typed_beside_automatic(
        screen,
        context,
        title=context.translate("USE flags"),
        prompt=context.translate("Flags to add, separated by spaces"),
        typed=config.portage.use,
        automatic=automatic_values.use_flags(config, context.groups),
        accepts=atoms.split_use_flags,
        rejected=context.translate("Not a USE flag"),
    )
    if not answer.chosen:
        return Answer(answer.outcome)
    values = answer.unwrap()
    _record_operator(context, ValueKind.USE_FLAG, values)
    return Answer(
        Outcome.CHOSE, replace(config, portage=replace(config.portage, use=values))
    )


def video_cards_screen(
    screen: Screen, config: InstallConfig, context: Context
) -> Answer[InstallConfig]:
    """`VIDEO_CARDS` on top of what the driver choice contributes.

    A hybrid machine needs more than one: an AMD laptop with an NVIDIA card
    carries `amdgpu radeonsi nvidia`, and the driver row offers one group. The
    values are USE_EXPAND flags, so they are checked as flags.
    """
    answer = _typed_beside_automatic(
        screen,
        context,
        title=context.translate("VIDEO_CARDS"),
        prompt=context.translate("Values to add, separated by spaces"),
        typed=config.portage.video_cards,
        automatic=automatic_values.video_cards(config, context.groups),
        accepts=atoms.split_use_flags,
        rejected=context.translate("Not a VIDEO_CARDS value"),
    )
    if not answer.chosen:
        return Answer(answer.outcome)
    values = answer.unwrap()
    _record_operator(context, ValueKind.VIDEO_CARD, values)
    return Answer(
        Outcome.CHOSE,
        replace(config, portage=replace(config.portage, video_cards=values)),
    )
