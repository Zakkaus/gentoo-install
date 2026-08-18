# SPDX-License-Identifier: GPL-2.0-or-later
"""The Mirrors screen: where every archive, overlay and binary host comes from.

One screen, one editable table, and the rows that build it. Nothing here is
reached from another screen, so `settings.py` is its only caller.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from ..i18n import Catalog
from ..model import compat, mirrors
from ..plan.portage import community_binhost
from ..model.config import (
    Binhost,
    BinhostChannel,
    GentooZhMirror,
    InstallConfig,
    MirrorConfig,
    MirrorRegion,
    Overlay,
    PortageConfig,
    Sync,
)
from .context import (
    Context,
    DONE,
    FieldDescriptor,
    current_menu,
    footer,
    pick,
    with_gentoo_zh,
)
from .widgets import Answer, Item, Menu, Outcome, Screen

_REGION: Final[str] = "region"
_SITE: Final[str] = "site"
_MEASURE: Final[str] = "measure"
_DISTFILES: Final[str] = "distfiles"
_SYNC: Final[str] = "sync"
_BINHOST: Final[str] = "binhost"
_ZH_BINHOST: Final[str] = "zh-binhost"
_ZH_SITE: Final[str] = "zh-site"
_ZH_DISTFILES: Final[str] = "zh-distfiles"


#: How the tree is kept up to date, and what each costs.
SYNC_METHODS: tuple[tuple[Sync, str], ...] = (
    (Sync.GIT, "carries the history a signed sync checks"),
    (Sync.RSYNC, "what emerge --sync has always done"),
    (Sync.WEBRSYNC, "no git, and no history"),
)

#: The gentoo-zh binary package channels.
GENTOOZH_CHANNELS: tuple[tuple[BinhostChannel, str], ...] = (
    (BinhostChannel.OFF, "not used"),
    (BinhostChannel.STABLE, "stable ::gentoo, ~amd64 for the overlay"),
    (BinhostChannel.UNSTABLE, "~amd64 throughout, so fewer packages match a binary host"),
)

#: Only x86-64 and x86-64-v3 carry a useful number of official binary packages;
#: the other subarchitectures are nearly empty.
BINHOSTS: tuple[tuple[tuple[bool, str], str], ...] = (
    ((False, "x86-64"), "hours rather than minutes"),
    ((True, "x86-64"), "works on every amd64 machine"),
    ((True, "x86-64-v3"), "this CPU runs it, and the packages are built for it"),
)

#: The overlays with no mirror of their own, so they are on or off and nothing
#: else. guru publishes from one place only.
PLAIN_OVERLAYS: tuple[tuple[str, str], ...] = (
    ("gig", "https://github.com/gentoo-zh/gig-overlay.git"),
    ("guru", "https://anongit.gentoo.org/git/repo/proj/guru.git"),
)


def _unreachable_here(site: mirrors.Site, context: Context) -> str:
    """Why this machine cannot fetch from a site, or empty.

    An IPv6-only machine reaches four of the mirrors on this list not at all:
    they publish no AAAA record, and one publishes an AAAA and does not answer
    on it. Saying so here is the difference between choosing another and
    finding out when the stage3 does not arrive.
    """
    # Both facts positive: this machine has IPv6 and has no IPv4. A machine
    # whose interface is still coming up reports neither, and refusing on that
    # would hide half the list from an operator who is about to configure it.
    if site.ipv6 or context.ipv4 or not context.ipv6:
        return ""
    return context.translate("this machine has no IPv4 and the mirror has no IPv6")


def mirror_screen(screen: Screen, config: InstallConfig, context: Context) -> Answer[InstallConfig]:
    """Where every repository comes from, and which of them are used at all.

    One screen because they are one decision: an overlay selected with no
    mirror behind it, or a mirror chosen for an overlay that is not selected,
    are both states the operator can reach by editing two rows apart.
    """
    translate = context.translate
    current = config
    cursor = 0
    while True:
        menu: Menu[str] = Menu(
            title=translate("Mirrors"),
            preamble=(translate("These sources provide the Gentoo tree, distfiles, overlays, and binary packages."),),
            items=_mirror_fields(current, translate),
            footer=footer(translate),
            cursor=cursor,
        )
        answer = menu.run(screen)
        cursor = menu.cursor
        if not answer.chosen:
            return Answer(answer.outcome)
        field = answer.unwrap()
        if field == DONE:
            return Answer(Outcome.CHOSE, _with_a_site(current))
        changed = _edit_mirror(screen, context, current, field)
        if changed is not None:
            current = changed


def _default_site(region: MirrorRegion, translate: Catalog) -> str:
    """What `_with_a_site` would adopt, drawn before it does."""
    offered = mirrors.gentoo_sites(region)
    return translate(offered[0].name) if offered else translate("none")


def _with_a_site(config: InstallConfig) -> InstallConfig:
    """The region's first mirror, for an operator who opened this screen and
    changed nothing.

    The row is required so that nobody installs from a mirror they never
    looked at, and opening the screen is looking at it. Leaving it unset
    instead made the row say `required` after it had been answered, which is
    the interface calling the operator wrong.
    """
    chosen = config.portage.mirrors
    if chosen.site:
        return config
    offered = mirrors.gentoo_sites(chosen.region)
    if not offered:
        return config
    return replace(
        config, portage=replace(config.portage, mirrors=replace(chosen, site=offered[0].key))
    )


def _mirror_fields(config: InstallConfig, translate: Catalog) -> list[Item[str]]:
    portage = config.portage
    chosen = portage.mirrors
    region = chosen.region
    site = chosen.site or mirrors.gentoo_sites(region)[0].key
    used = {overlay.name for overlay in portage.overlays}
    zh = mirrors.gentoozh(chosen.gentoo_zh)
    rows = [field.row(config, translate) for field in _MIRROR_FIELDS]
    rows += [
        # One row, not one per method: showing a git address under a webrsync
        # choice is the interface contradicting itself.
        Item(
            label=translate("Gentoo repository"),
            value="",
            detail=_tree_source(portage, region, site, translate),
        ),
        next(field.row(config, translate) for field in _ALL_MIRROR_FIELDS if field.key == _BINHOST),
        next(field.row(config, translate) for field in _ALL_MIRROR_FIELDS if field.key == _ZH_SITE),
    ]
    if "gentoo-zh" in used:
        rows += [field.row(config, translate) for field in _ZH_MIRROR_FIELDS]
    rows += [
        field.row(config, translate) for field in _OVERLAY_FIELDS
    ]
    rows.append(Item(label=translate("Done"), value=DONE))
    return rows


def _site_name(region: MirrorRegion, site: str, translate: Catalog) -> str:
    """The chosen site's name, or `not set` when the region does not carry it.

    The two are set together everywhere, and a bare `next` over a list that
    does not hold it raised `StopIteration` out of the menu instead.
    """
    named = next((one.name for one in mirrors.gentoo_sites(region) if one.key == site), "")
    return translate(named) if named else translate("not set")


def _tree_source(
    portage: PortageConfig, region: MirrorRegion, site: str, translate: Catalog
) -> str:
    """Where the chosen sync method will actually read the tree from."""
    if portage.sync is Sync.GIT:
        return mirrors.gentoo_sync_uri(region, site)
    if portage.sync is Sync.RSYNC:
        return mirrors.gentoo_rsync_uri(region, site)
    return translate("a snapshot from GENTOO_MIRRORS")


def _edit_mirror(
    screen: Screen, context: Context, config: InstallConfig, field: str
) -> InstallConfig | None:
    """The one screen behind a field, or None when nothing changed."""
    if not field:
        # A row that only reports where a service will come from, derived from
        # the two choices above it.
        return None
    descriptor = next((one for one in _ALL_MIRROR_FIELDS if one.key == field), None)
    return descriptor.edit(screen, context, config) if descriptor else None


def _mirror_region_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(label=translate("Region"), value=_REGION, detail=config.portage.mirrors.region.value)


def _mirror_site_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    chosen = config.portage.mirrors
    site = chosen.site or mirrors.gentoo_sites(chosen.region)[0].key
    detail = _site_name(chosen.region, site, translate) if chosen.site else (
        f"{_default_site(chosen.region, translate)} ({translate('default')})"
    )
    return Item(label=translate("Gentoo mirror"), value=_SITE, detail=detail)


def _mirror_measure_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(
        label=translate("Measure them"), value=_MEASURE,
        detail=translate("yes") if config.portage.mirrors.speed_test else translate("no"),
    )


def _mirror_sync_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(label=translate("Repository sync"), value=_SYNC, detail=config.portage.sync.value)


def _edit_mirror_region(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig | None:
    current = config.portage.mirrors
    picked = Menu(title=context.translate("Region"), items=[Item(label=one.value, value=one) for one in MirrorRegion], footer=footer(context.translate), current=current.region).run(screen)
    if not picked.chosen:
        return None
    return replace(config, portage=replace(config.portage, mirrors=replace(current, region=picked.unwrap(), site="")))


def _edit_mirror_site(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig | None:
    current = config.portage.mirrors
    offered = mirrors.gentoo_sites(current.region)
    chosen = Menu(title=context.translate("Gentoo mirror"), items=[Item(label=context.translate(one.name), value=one.key, detail=f"{context.translate(one.area)}  {one.distfiles}", disabled_because=_unreachable_here(one, context)) for one in offered], footer=footer(context.translate), current=current.site).run(screen)
    return replace(config, portage=replace(config.portage, mirrors=replace(current, site=chosen.unwrap()))) if chosen.chosen else None


def _toggle_mirror_distfiles(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig:
    current = config.portage.mirrors
    return replace(config, portage=replace(config.portage, mirrors=replace(current, gentoo_distfiles=not current.gentoo_distfiles)))


def _toggle_mirror_measure(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig:
    current = config.portage.mirrors
    return replace(config, portage=replace(config.portage, mirrors=replace(current, speed_test=not current.speed_test)))


def _edit_mirror_sync(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig | None:
    return pick(screen, context, config, "Repository sync", list(SYNC_METHODS), config.portage.sync, lambda chosen, value: replace(chosen, portage=replace(chosen.portage, sync=value)))


def _mirror_distfiles_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(
        label=translate("Gentoo distfiles"),
        value=_DISTFILES,
        detail=translate("in use") if config.portage.mirrors.gentoo_distfiles else translate("not used"),
    )


def _mirror_zh_distfiles_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    chosen = config.portage.mirrors
    return Item(label=translate("gentoo-zh distfiles"), value=_ZH_DISTFILES, detail=mirrors.gentoozh_distfiles(chosen.gentoo_zh)[0] if chosen.gentoo_zh_distfiles else translate("not used"))


def _mirror_zh_binhost_row(config: InstallConfig, translate: Catalog) -> Item[str]:
    return Item(label=translate("gentoo-zh binary packages"), value=_ZH_BINHOST, detail=community_binhost(config.portage) if config.portage.binhost.community is not BinhostChannel.OFF else translate("not used"))


def _mirror_overlay_row(config: InstallConfig, translate: Catalog, name: str) -> Item[str]:
    return Item(label=name, value=name, detail=translate("in use") if name in {one.name for one in config.portage.overlays} else translate("not used"))


def _edit_mirror_overlay(
    screen: Screen, context: Context, config: InstallConfig, name: str
) -> InstallConfig:
    return _toggle_overlay(config, name)


def _overlay_descriptor(name: str) -> FieldDescriptor[InstallConfig]:
    def row(config: InstallConfig, translate: Catalog) -> Item[str]:
        return _mirror_overlay_row(config, translate, name)

    def edit(screen: Screen, context: Context, config: InstallConfig) -> InstallConfig:
        return _edit_mirror_overlay(screen, context, config, name)

    return FieldDescriptor(name, row, edit)


_MIRROR_FIELDS: tuple[FieldDescriptor[InstallConfig], ...] = (
    FieldDescriptor(_REGION, _mirror_region_row, _edit_mirror_region),
    FieldDescriptor(_SITE, _mirror_site_row, _edit_mirror_site),
    FieldDescriptor(_DISTFILES, _mirror_distfiles_row, lambda s, c, x: _toggle_mirror_distfiles(s, c, x)),
    FieldDescriptor(_MEASURE, _mirror_measure_row, lambda s, c, x: _toggle_mirror_measure(s, c, x)),
    FieldDescriptor(_SYNC, _mirror_sync_row, _edit_mirror_sync),
)
_ZH_MIRROR_FIELDS: tuple[FieldDescriptor[InstallConfig], ...] = (
    FieldDescriptor(_ZH_DISTFILES, _mirror_zh_distfiles_row, lambda s, c, x: replace(x, portage=replace(x.portage, mirrors=replace(x.portage.mirrors, gentoo_zh_distfiles=not x.portage.mirrors.gentoo_zh_distfiles)))),
)
_OVERLAY_FIELDS = tuple(
    _overlay_descriptor(name)
    for name, _ in PLAIN_OVERLAYS
)
_ALL_MIRROR_FIELDS = _MIRROR_FIELDS + (
    FieldDescriptor(_BINHOST, lambda config, translate: Item(label=translate("Gentoo binary packages"), value=_BINHOST, detail=mirrors.gentoo_binhost(config.portage.mirrors.region, config.portage.mirrors.site, config.portage.binhost.subarch) if config.portage.binhost.official else translate("not used")), lambda s, c, x: _edit_binhost(s, c, x)),
    FieldDescriptor(_ZH_BINHOST, _mirror_zh_binhost_row, lambda s, c, x: pick(s, c, x, "gentoo-zh binary packages", list(GENTOOZH_CHANNELS), x.portage.binhost.community, lambda chosen, value: replace(chosen, portage=replace(chosen.portage, binhost=replace(chosen.portage.binhost, community=value))))),
    FieldDescriptor(_ZH_SITE, lambda config, translate: Item(label=translate("gentoo-zh"), value=_ZH_SITE, detail=translate(mirrors.gentoozh(config.portage.mirrors.gentoo_zh).name) if "gentoo-zh" in {one.name for one in config.portage.overlays} else translate("not used")), lambda s, c, x: _edit_gentoozh(s, c, x)),
    *_ZH_MIRROR_FIELDS,
    *_OVERLAY_FIELDS,
)


def _edit_binhost(
    screen: Screen, context: Context, config: InstallConfig
) -> InstallConfig | None:
    """Off, or which subarchitecture. `ld.so` lists what this CPU would search,
    so a machine that cannot run v3 is told rather than left to meet an illegal
    instruction in the first binary package."""
    translate = context.translate
    items: list[Item[tuple[bool, str]]] = [
        Item(
            label=subarch if official else translate("not used"),
            value=(official, subarch),
            detail=translate(reason) if subarch != "x86-64-v3" or context.supports_v3 else "",
            disabled_because=(
                translate("this CPU cannot run it")
                if subarch.endswith("v3") and not context.supports_v3
                else ""
            ),
        )
        for (official, subarch), reason in BINHOSTS
    ]
    menu: Menu[tuple[bool, str]] = Menu(
        title=translate("Gentoo binary packages"),
        preamble=(translate("Binary packages reduce compilation; source builds remain the fallback."),),
        items=items,
        footer=footer(translate),
        current=(config.portage.binhost.official, config.portage.binhost.subarch),
    )
    answer = menu.run(screen)
    if not answer.chosen:
        return None
    official, subarch = answer.unwrap()
    portage = config.portage
    return replace(
        config,
        portage=replace(
            portage, binhost=replace(portage.binhost, official=official, subarch=subarch)
        ),
    )


def _edit_gentoozh(
    screen: Screen, context: Context, config: InstallConfig
) -> InstallConfig | None:
    """Whether gentoo-zh is used and where from, which is one question: a
    mirror chosen for an overlay nobody selected changes nothing."""
    translate = context.translate
    items: list[Item[GentooZhMirror | None]] = [Item(label=translate("not used"), value=None)]
    items += [
        Item(
            label=translate(one.name),
            value=GentooZhMirror(one.key),
            # The git address, not the distfiles one: this choice writes
            # `sync-uri`, and upstream serves the two from different hosts.
            detail=f"{translate(one.area)}  {one.git}",
        )
        for one in mirrors.GENTOOZH_SITES
    ]
    used = any(one.name == "gentoo-zh" for one in config.portage.overlays)
    answer = current_menu(
        screen,
        context,
        translate("gentoo-zh"),
        items,
        config.portage.mirrors.gentoo_zh if used else None,
    )
    if not answer.chosen:
        return None
    picked = answer.unwrap()
    portage = config.portage
    if picked is None:
        # What required the overlay goes with it: leaving the community binhost
        # on refuses the install from a row the operator is not looking at.
        kept = tuple(one for one in portage.overlays if one.name != "gentoo-zh")
        return replace(
            config,
            portage=replace(
                portage,
                overlays=kept,
                binhost=replace(portage.binhost, community=BinhostChannel.OFF),
            ),
        )
    # The overlay is cloned from the chosen site, not from upstream: a mirror
    # picked here and ignored by the sync is the choice doing nothing.
    added = with_gentoo_zh(config)
    overlays = tuple(
        replace(one, sync_uri=mirrors.gentoozh(picked).git) if one.name == "gentoo-zh" else one
        for one in added.overlays
    )
    return replace(
        config,
        portage=replace(
            added, overlays=overlays, mirrors=replace(portage.mirrors, gentoo_zh=picked)
        ),
    )


def _toggle_overlay(config: InstallConfig, name: str) -> InstallConfig:
    """Flipped where it stands. A yes/no screen over a row that already reads
    `in use` or `not used` asks what the row has answered."""
    portage = config.portage
    kept = tuple(one for one in portage.overlays if one.name != name)
    if len(kept) == len(portage.overlays):
        uri = next(where for offered, where in PLAIN_OVERLAYS if offered == name)
        kept = (*kept, Overlay(name=name, sync_uri=uri))
    return replace(config, portage=replace(portage, overlays=kept))
