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


def test_rsync_always_names_somewhere() -> None:
    """Most mirrors carry the files and not an rsync module, and the official
    pool is what the handbook gives, so there is always an answer."""
    assert mirrors.gentoo_rsync_uri(MirrorRegion.CN, "sustech").startswith("rsync://")
    assert (
        mirrors.gentoo_rsync_uri(MirrorRegion.GLOBAL)
        == "rsync://rsync.gentoo.org/gentoo-portage"
    )
    assert mirrors.gentoo_rsync_uri(MirrorRegion.CN, "tuna").endswith("tsinghua.edu.cn/gentoo-portage")


def test_gentoozh_binhost_and_distfiles_come_from_the_same_site() -> None:
    for chosen in GentooZhMirror:
        site = mirrors.gentoozh(chosen)
        assert mirrors.gentoozh_binhost(chosen) == f"{site.distfiles}/binpkgs/x86-64"
        assert mirrors.gentoozh_distfiles(chosen)[0] == site.distfiles


def test_every_gentoozh_site_is_appended_with_the_chosen_one_first() -> None:
    """`GENTOO_MIRRORS` is a fallback chain: a mirror that is behind on one
    file still has the rest, so the choice is an order and not a filter."""
    ordered = mirrors.gentoozh_distfiles(GentooZhMirror.NJU)
    assert ordered[0] == "https://mirror.nju.edu.cn/gentoo-zh"
    assert set(ordered) == {site.distfiles for site in mirrors.GENTOOZH_SITES}
    assert mirrors.gentoozh_distfiles(GentooZhMirror.UPSTREAM)[0] == (
        "https://distfiles.gentoozh.org"
    )


def test_the_chinese_default_is_ustc_and_not_tuna() -> None:
    """On 2026-08-10 the rsync tree at tuna served a snapshot its own signed
    Manifest did not match — `metadata/Manifest.gz expected 23353, have
    23355` — and stayed that way across three attempts an hour apart, so three
    cluster guests stopped an hour into their installs with
    `emerge --sync gentoo exited 1`.

    The order here is what an operator gets without choosing, so it names the
    two that were serving a consistent tree.
    """
    from gentoo_install.model.mirrors import GENTOO_SITES

    names = [one.key for one in GENTOO_SITES]
    assert names[:2] == ["ustc", "nju"], names[:4]
    assert names.index("tuna") > names.index("ustc"), names
