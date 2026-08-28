# SPDX-License-Identifier: GPL-2.0-or-later
"""Where each repository is fetched from, as one table per repository.

The main tree and gentoo-zh are separate choices and separate tables: they hold
different files, a machine is not equally close to both, and the two do not
offer the same set of sites. A site serves up to three things and most serve
only one, so `git` and `rsync` are empty rather than derived from `distfiles`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .architecture import DEFAULT_ARCHITECTURE
from .config import PROFILE_RELEASE, GentooZhMirror, MirrorRegion


@dataclass(frozen=True)
class Site:
    """One mirror and what it actually serves.

    `distfiles` is the base Portage appends `distfiles/` to. `git` and `rsync`
    are empty for a site that carries the files and not the tree, and a sync
    method falls back to a site that has it.
    """

    key: str
    name: str
    #: Where it is, so a long list can be read by anyone choosing from it.
    area: str
    distfiles: str
    git: str = ""
    rsync: str = ""
    #: Whether the site answers over IPv6. False for a site with no AAAA
    #: record, and for one that publishes an AAAA and does not answer on it:
    #: an IPv6-only machine cannot install from either, and the menu says so
    #: rather than letting the operator find out when the stage3 does not
    #: arrive. Measured with `python3 -m tests.mirrors_probe`.
    ipv6: bool = True
    #: Whether the site serves `releases/`, which is where the stage3 comes
    #: from. False for a site that carries the files and not the archives:
    #: `mirror.xtom.com.hk` answers 200 for `releases/amd64/autobuilds/` and
    #: 404 for the pointer file inside it, and `ftp.twaren.net` answers 403 to
    #: this installer's own downloader while serving a browser. An install told
    #: to use either one stopped with no stage3.
    releases: bool = True


#: The main tree. Taken from the project's own mirror list; the git and rsync
#: columns are the addresses reported as tested rather than a pattern applied
#: to every host, because the paths do not follow one: USTC serves git at
#: `/gentoo.git` and rsync from another hostname entirely.
GENTOO_SITES: Final[tuple[Site, ...]] = (
    # USTC first, then NJU. On 2026-08-10 the rsync tree at tuna served a
    # snapshot its own signed Manifest did not match — `metadata/Manifest.gz
    # expected 23353, have 23355` — and it stayed that way across three
    # attempts an hour apart, so three cluster guests stopped an hour into
    # their installs. The order is what an operator gets by default.
    Site(
        "ustc", "USTC", "Hefei",
        "https://mirrors.ustc.edu.cn/gentoo",
        "https://mirrors.ustc.edu.cn/gentoo.git",
        "rsync://rsync.mirrors.ustc.edu.cn/gentoo-portage",
    ),
    Site(
        "nju", "Nanjing University", "Nanjing",
        "https://mirrors.nju.edu.cn/gentoo",
        "https://mirrors.nju.edu.cn/git/gentoo-portage.git",
    ),
    Site(
        "bfsu", "BFSU", "Beijing",
        "https://mirrors.bfsu.edu.cn/gentoo",
        "https://mirrors.bfsu.edu.cn/git/gentoo-portage.git",
        "rsync://mirrors.bfsu.edu.cn/gentoo-portage",
    ),
    # No git column: `/git/gentoo-portage.git`, `/git/gentoo.git` and
    # `/gentoo-portage.git` were each asked and none of them answered. A sync
    # method falls back to a site that has it rather than to a dead address.
    Site(
        "tuna", "Tsinghua TUNA", "Beijing",
        "https://mirrors.tuna.tsinghua.edu.cn/gentoo",
        rsync="rsync://mirrors.tuna.tsinghua.edu.cn/gentoo-portage",
    ),
    Site(
        "zju", "Zhejiang University", "Hangzhou",
        "https://mirrors.zju.edu.cn/gentoo",
    ),
    Site(
        "sdu", "Shandong University", "Qingdao",
        "https://mirrors.sdu.edu.cn/gentoo",
    ),
    Site(
        "hust", "HUST", "Wuhan",
        "https://mirrors.hust.edu.cn/gentoo",
        "https://mirrors.hust.edu.cn/git/gentoo-portage.git",
    ),
    Site("sustech", "SUSTech", "Shenzhen", "https://mirrors.sustech.edu.cn/gentoo"),
    Site("hit", "HIT", "Harbin", "https://mirrors.hit.edu.cn/gentoo"),
    Site("lzu", "Lanzhou University", "Lanzhou", "https://mirror.lzu.edu.cn/gentoo"),
    Site("aliyun", "Aliyun", "China, CDN", "https://mirrors.aliyun.com/gentoo", ipv6=False),  # no AAAA record
    Site("netease", "NetEase 163", "China, CDN", "https://mirrors.163.com/gentoo", ipv6=False),  # no AAAA record
    # Federated: it answers with a 302 to whichever member is nearest, so one
    # address covers all three services and needs no measurement of its own.
    Site(
        "cernet", "CERNET", "China, nearest",
        "https://mirrors.cernet.edu.cn/gentoo",
        "https://mirrors.cernet.edu.cn/gentoo-portage.git",
    ),
    Site("cicku-hk", "CICKU", "Hong Kong", "https://hk.mirrors.cicku.me/gentoo"),
    Site(
        "planetunix-hk", "PlanetUnix", "Hong Kong",
        "https://hippocamp.cn.ext.planetunix.net/pub/gentoo",
        rsync="rsync://hippocamp.cn.ext.planetunix.net/gentoo-portage",
    ),
    Site(
        "xtom-hk", "xTom", "Hong Kong", "https://mirror.xtom.com.hk/gentoo",
        releases=False,
    ),
    Site("rackspace-hk", "Rackspace", "Hong Kong", "https://mirror.rackspace.com/gentoo", ipv6=False),  # no AAAA record
    # http, not https: the only address this one answers on.
    Site("aditsu-hk", "aditsu", "Hong Kong", "http://gentoo.aditsu.net:8000", ipv6=False),  # no AAAA record
    Site(
        "nchc-tw", "NCHC", "Taiwan",
        "http://ftp.twaren.net/Linux/Gentoo",
        rsync="rsync://ftp.twaren.net/gentoo-portage",
        releases=False,
        # It publishes an AAAA record and does not answer on it.
        ipv6=False,
    ),
    Site("cicku-tw", "CICKU", "Taiwan", "https://tw.mirrors.cicku.me/gentoo"),
    Site("freedif-sg", "Freedif", "Singapore", "https://mirror.freedif.org/gentoo"),
    Site("cicku-sg", "CICKU", "Singapore", "https://sg.mirrors.cicku.me/gentoo"),
    Site(
        "planetunix-sg", "PlanetUnix", "Singapore",
        "https://enceladus.sg.ext.planetunix.net/pub/gentoo",
    ),
    Site(
        "gentoo", "gentoo.org", "worldwide",
        "https://distfiles.gentoo.org",
        "https://github.com/gentoo-mirror/gentoo.git",
        # The address the handbook gives, which rotates over the official
        # rsync pool.
        "rsync://rsync.gentoo.org/gentoo-portage",
    ),
    Site("osuosl", "OSU Open Source Lab", "worldwide", "https://gentoo.osuosl.org"),
)

#: Which sites a region offers, in the order the menu lists them.
GENTOO_REGIONS: Final[dict[MirrorRegion, tuple[str, ...]]] = {
    # USTC and NJU first. This list is what picks the sync address, not the
    # order of `GENTOO_SITES`: reordering that alone left `gentoo_rsync_uri`
    # answering tuna, whose rsync tree served a snapshot its own signed
    # Manifest did not match on three separate rounds.
    MirrorRegion.CN: (
        "ustc", "nju", "bfsu", "tuna", "zju", "sdu", "hust", "sustech", "hit",
        "lzu", "aliyun", "netease", "cernet", "cicku-hk", "planetunix-hk",
        "xtom-hk", "rackspace-hk", "aditsu-hk", "nchc-tw", "cicku-tw",
        "freedif-sg", "cicku-sg", "planetunix-sg",
    ),
    MirrorRegion.GLOBAL: ("gentoo", "osuosl"),
}

_GENTOO: Final[dict[str, Site]] = {site.key: site for site in GENTOO_SITES}

#: gentoo-zh. Every one answers `binpkgs/x86-64/Packages` and every one answers
#: a git request, though cernet keeps the tree beside the files rather than
#: under `/git/`.
GENTOOZH_SITES: Final[tuple[Site, ...]] = (
    Site(
        "upstream", "upstream", "worldwide",
        "https://distfiles.gentoozh.org",
        "https://github.com/gentoo-zh/overlay.git",
    ),
    Site(
        "cernet", "CERNET", "China, nearest",
        "https://mirrors.cernet.edu.cn/gentoo-zh",
        "https://mirrors.cernet.edu.cn/gentoo-zh.git",
    ),
    Site(
        "nju", "Nanjing University", "Nanjing",
        "https://mirror.nju.edu.cn/gentoo-zh",
        "https://mirror.nju.edu.cn/git/gentoo-zh.git",
    ),
    Site(
        "nyist", "Nanyang Institute of Technology", "Nanyang",
        "https://mirror.nyist.edu.cn/gentoo-zh",
        "https://mirror.nyist.edu.cn/git/gentoo-zh.git",
    ),
    Site(
        "ha", "Henan Education Network", "Henan",
        "https://mirrors.ha.edu.cn/gentoo-zh",
        "https://mirrors.ha.edu.cn/git/gentoo-zh.git",
    ),
)

_GENTOOZH: Final[dict[GentooZhMirror, Site]] = {
    GentooZhMirror(site.key): site for site in GENTOOZH_SITES
}


def gentoo_sites(region: MirrorRegion) -> tuple[Site, ...]:
    return tuple(_GENTOO[key] for key in GENTOO_REGIONS[region])


def gentoo_distfiles(region: MirrorRegion, preferred: str = "") -> tuple[str, ...]:
    """Every site of that region, the chosen one first.

    All of them, not only the chosen one: Portage walks the list, and a mirror
    that is behind on one file still has the rest.
    """
    sites = gentoo_sites(region)
    ordered = sorted(sites, key=lambda site: site.key != preferred)
    return tuple(f"{site.distfiles}/" for site in ordered)


def gentoo_sync_uri(region: MirrorRegion, preferred: str = "") -> str:
    """The chosen site's tree, or the first in the region that carries one."""
    chosen = _GENTOO.get(preferred)
    if chosen is not None and chosen.git:
        return chosen.git
    return next(site.git for site in gentoo_sites(region) if site.git)


def gentoo_sync_uris(region: MirrorRegion) -> tuple[str, ...]:
    """Every git tree the region carries, in the order the menu lists them.

    The chosen site is one of these; a sync that cannot reach it has the rest
    to try, and each one is a mirror of the same signed history.
    """
    return tuple(site.git for site in gentoo_sites(region) if site.git)


def gentoo_rsync_uri(region: MirrorRegion, preferred: str = "") -> str:
    """The chosen site's rsync module, then any in the region, then the official
    pool. Never empty: most mirrors carry the files and not an rsync module, and
    `rsync.gentoo.org` is what the handbook gives."""
    chosen = _GENTOO.get(preferred)
    if chosen is not None and chosen.rsync:
        return chosen.rsync
    within = next((site.rsync for site in gentoo_sites(region) if site.rsync), "")
    return within or _GENTOO["gentoo"].rsync


#: Where a mirror keeps the official binary packages, relative to the
#: distfiles base. Every site that carries the releases tree carries these.
#: Where a mirror keeps the official binary packages, relative to the
#: distfiles base. The architecture appears twice in one URL and both come
#: from the same row: `releases/<gentoo_name>/binpackages/<release>/<subarch>`.
BINPACKAGES: Final[str] = "releases/{arch}/binpackages/" + PROFILE_RELEASE


def gentoo_binhost(region: MirrorRegion, preferred: str = "", subarch: str = "") -> str:
    """The official binary packages, from the same site as the distfiles."""
    sites = gentoo_sites(region)
    chosen = next((site for site in sites if site.key == preferred), sites[0])
    within = BINPACKAGES.format(arch=DEFAULT_ARCHITECTURE.gentoo_name)
    return f"{chosen.distfiles}/{within}/{subarch or DEFAULT_ARCHITECTURE.binhost_subarch}"


def gentoozh(chosen: GentooZhMirror) -> Site:
    return _GENTOOZH[chosen]


#: Which gentoo-zh site each Gentoo mirror region reaches fastest. Separate
#: tables because the two repositories do not share a mirror set, and a
#: machine choosing Chinese Gentoo mirrors cloning the overlay from GitHub is
#: what this pairing exists to prevent.
_GENTOOZH_BY_REGION: Final[dict[MirrorRegion, GentooZhMirror]] = {
    MirrorRegion.CN: GentooZhMirror.CERNET,
    MirrorRegion.GLOBAL: GentooZhMirror.UPSTREAM,
}


def gentoozh_for(region: MirrorRegion) -> GentooZhMirror:
    """The gentoo-zh site that suits a Gentoo mirror region."""
    return _GENTOOZH_BY_REGION[region]


def gentoozh_binhost(
    chosen: GentooZhMirror, subarch: str = "x86-64", unstable: bool = False
) -> str:
    """The channel is a different path, not a different keyword file: stable
    builds against the main tree's `amd64`, unstable against `~amd64`
    throughout, and Portage refuses a binpkg whose dependencies do not match."""
    where = "unstable/binpkgs" if unstable else "binpkgs"
    return f"{gentoozh(chosen).distfiles}/{where}/{subarch}"


def overlay_sync_uris(name: str, chosen: GentooZhMirror) -> tuple[str, ...]:
    """Every site carrying that overlay, the chosen one first.

    Only gentoo-zh has a mirror set. `gig` is published from one host, so an
    install that cannot reach it has nowhere else to look and stops there.
    """
    if name != "gentoo-zh":
        return ()
    ordered = sorted(GENTOOZH_SITES, key=lambda site: site.key != chosen.value)
    return tuple(site.git for site in ordered if site.git)


def gentoozh_distfiles(chosen: GentooZhMirror) -> tuple[str, ...]:
    """Every site, the chosen one first, the same as `gentoo_distfiles`: a
    mirror behind on one file still has the rest. Portage appends `distfiles/`,
    so the base is what goes in."""
    ordered = sorted(GENTOOZH_SITES, key=lambda site: site.key != chosen.value)
    return tuple(site.distfiles for site in ordered)
