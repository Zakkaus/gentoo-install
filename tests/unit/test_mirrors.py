"""The mirror tables. Every address here was taken from the project's own list
rather than derived from a pattern, because the paths do not follow one."""

from __future__ import annotations

from urllib.parse import urlsplit

from gentoo_install.model.config import GentooZhMirror, MirrorRegion
from gentoo_install.model import mirrors


def test_every_site_of_a_region_is_a_site_that_exists() -> None:
    for region, keys in mirrors.GENTOO_REGIONS.items():
        assert keys, region
        for key in keys:
            assert any(site.key == key for site in mirrors.GENTOO_SITES), key


def test_every_address_is_absolute_and_uses_a_scheme_portage_understands() -> None:
    for site in (*mirrors.GENTOO_SITES, *mirrors.GENTOOZH_SITES):
        assert urlsplit(site.distfiles).scheme in ("http", "https"), site.key
        assert not site.distfiles.endswith("/"), site.key
        if site.git:
            assert site.git.endswith(".git"), site.key
        if site.rsync:
            assert site.rsync.startswith("rsync://"), site.key


def test_a_region_always_has_somewhere_to_sync_the_tree_from() -> None:
    """Most mirrors carry the files and not the tree, so a region whose chosen
    site has no git still has to name one."""
    for region in MirrorRegion:
        assert mirrors.gentoo_sync_uri(region).endswith(".git")
        assert mirrors.gentoo_sync_uri(region, "sustech").endswith(".git")


def test_the_chosen_site_comes_first_and_the_rest_stay() -> None:
    """Portage walks the list, so a mirror that is behind on one file still
    has the rest."""
    ordered = mirrors.gentoo_distfiles(MirrorRegion.CN, "ustc")
    assert ordered[0] == "https://mirrors.ustc.edu.cn/gentoo/"
    assert len(ordered) == len(mirrors.GENTOO_REGIONS[MirrorRegion.CN])


def test_a_site_that_serves_no_rsync_falls_back_within_its_region() -> None:
    assert mirrors.gentoo_rsync_uri(MirrorRegion.CN, "sustech").startswith("rsync://")
    # No global mirror in the list serves rsync, and saying so beats inventing
    # an address that would fail at the first sync.
    assert mirrors.gentoo_rsync_uri(MirrorRegion.GLOBAL) == ""


def test_gentoozh_binhost_and_distfiles_come_from_the_same_site() -> None:
    for chosen in GentooZhMirror:
        site = mirrors.gentoozh(chosen)
        assert mirrors.gentoozh_binhost(chosen) == f"{site.distfiles}/binpkgs/x86-64"
        assert mirrors.gentoozh_distfiles(chosen)[0] == site.distfiles


def test_upstream_is_last_in_the_gentoozh_list() -> None:
    """The order the project documents: the chosen mirror first, the origin
    last, so a mirror that is behind still falls through to a complete set."""
    ordered = mirrors.gentoozh_distfiles(GentooZhMirror.NJU)
    assert ordered[-1] == "https://distfiles.gentoozh.org"
    assert mirrors.gentoozh_distfiles(GentooZhMirror.UPSTREAM) == (
        "https://distfiles.gentoozh.org",
    )
