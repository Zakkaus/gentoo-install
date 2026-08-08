"""stage3, the chroot, and everything Portage needs before the first emerge.

The order here is not a preference. `getuto` has to build `/etc/portage/gnupg`
before a key can be imported, an imported key stays untrusted until `lsign`, and
`package.use/zz-autounmask` has to exist before the emerge that writes into it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Sequence

from ..errors import CommandFailed, ConfigError
from ..model import mirrors
from ..model.config import (
    BinhostChannel,
    InitSystem,
    InstallConfig,
    Keywords,
    MirrorRegion,
    PortageConfig,
    Sync,
)
from .operations import Context, Operation, Stage

#: Only `x86-64` and `x86-64-v3` carry a useful number of official binary
#: packages; the other subarchitectures are nearly empty, so the interface
#: offers those two. Which mirror serves them is `model/mirrors.py`.

#: The release engineering key, pinned. A fingerprint that does not match this
#: is a failed install, not a prompt to trust something new.
RELENG_FINGERPRINT: Final[str] = "13EBBDBEDE7A12775DFDB1BABB572E0E2D182910"

#: gentoo-zh signs its binary packages with this key.
GENTOOZH_FINGERPRINT: Final[str] = "6A0726AF1476A2F382C6AC6638A0234EC16AD42E"

#: `sec-keys/openpgp-keys-gentoozh` installs the key here.
GENTOOZH_KEY: Final[PurePosixPath] = PurePosixPath("/usr/share/openpgp-keys/gentoozh.asc")

#: A stage3 ships the release engineering key at this path.
RELEASE_KEY: Final[PurePosixPath] = PurePosixPath("/usr/share/openpgp-keys/gentoo-release.asc")

#: Portage writes automatic keyword, USE and licence decisions into these. They
#: have to exist before the first emerge, or the writes land outside Portage.
AUTOUNMASK_FILES: Final[tuple[str, ...]] = (
    "/etc/portage/package.use/zz-autounmask",
    "/etc/portage/package.accept_keywords/zz-autounmask",
    "/etc/portage/package.license/zz-autounmask",
)

#: Never `--binpkg-respect-use=y` with these: it overrides `--autounmask-use=y`
#: and the two together leave autounmask doing nothing.
EMERGE_OPTIONS: Final[tuple[str, ...]] = (
    "--verbose",
    "--autounmask-license=y",
    "--autounmask-use=y",
    "--autounmask-write=y",
    "--autounmask-continue=y",
)

#: What a failed keyring degrades: every host at once, since none of them can
#: be verified without one.
BINARY_PACKAGES: Final[str] = "binary packages"


def binhost_trust(name: str) -> str:
    """What one host's own key degrades. The official host's key comes from
    `getuto`, so a community key that failed must not switch it off too."""
    return f"binary packages from {name}"

#: `*/*-bin` is deliberately absent: it would exclude `gentoo-kernel-bin`, the
#: one package this installer asks a binary host for.
BINPKG_OPTIONS: Final[tuple[str, ...]] = (
    "--getbinpkg=y",
    "--binpkg-changed-deps=y",
    "--usepkg-exclude",
    "acct-*/* virtual/*",
)


@dataclass(frozen=True, kw_only=True)
class InstallStage3(Operation):
    """Download, verify and unpack in one step, because the archive's name is
    only known once the mirror's directory index has been read.

    GNU tar, not Python's tarfile: stage3 carries xattrs and file capabilities
    and tarfile restores neither.
    """

    stage: Stage = Stage.STAGE3
    mirror: str
    variant: str

    def describe(self) -> str:
        return (
            f"download the newest {self.variant} stage3 from {self.mirror}, verify it "
            f"against {RELENG_FINGERPRINT[-16:]} and unpack it into the target"
        )

    def apply(self, context: Context) -> None:
        archive = context.fetch_stage3(self.mirror, self.variant, RELENG_FINGERPRINT)
        context.run(
            [
                "tar", "--extract",
                "--file", str(archive),
                "--directory", str(context.target),
                "--preserve-permissions",
                "--xattrs-include=*.*",
                "--numeric-owner",
            ]
        )


@dataclass(frozen=True, kw_only=True)
class PrepareChroot(Operation):
    """`/run` is bound and made slave rather than mounted as a fresh tmpfs: that
    is the Handbook's form and it keeps the installing system's udev reachable.

    `/sys` is recursive, so the target gets efivarfs with it and `efibootmgr`
    inside the chroot can write a boot entry.
    """

    stage: Stage = Stage.CHROOT

    def describe(self) -> str:
        return "mount proc, sys, dev and run into the target and copy resolv.conf"

    def apply(self, context: Context) -> None:
        target = context.target
        context.run(["mount", "--types", "proc", "/proc", str(target / "proc")])
        for source, propagation in (("/sys", "rslave"), ("/dev", "rslave"), ("/run", "slave")):
            where = str(target / source.lstrip("/"))
            context.run(["mount", "--rbind" if propagation == "rslave" else "--bind", source, where])
            context.run(["mount", f"--make-{propagation}", where])
        context.run(["install", "--mode=0644", "/etc/resolv.conf", str(target / "etc/resolv.conf")])


@dataclass(frozen=True, kw_only=True)
class WriteMakeConf(Operation):
    """The mirror order is settled here rather than in the plan, because the
    only useful way to order mirrors is to measure them, and measuring is
    something only `exec` may do."""

    stage: Stage = Stage.PORTAGE
    settings: tuple[tuple[str, str], ...]
    mirrors: tuple[str, ...]
    speed_test: bool
    #: Appended after the measurement, never ranked with the rest: these hold
    #: the overlay's own distfiles, so ranking them together would order one
    #: repository by how fast the other answers.
    appended: tuple[str, ...] = ()

    def describe(self) -> str:
        keys = [key for key, _ in self.settings]
        order = "fastest first, measured" if self.speed_test else "in the configured order"
        extra = f", {len(self.appended)} appended" if self.appended else ""
        return (
            f"write /etc/portage/make.conf with {', '.join(keys)}; "
            f"{len(self.mirrors)} mirrors {order}{extra}"
        )

    def apply(self, context: Context) -> None:
        ranked = context.rank_mirrors(self.mirrors) if self.speed_test else self.mirrors
        wanted = list(self.settings)
        listed = (*ranked, *self.appended)
        # Not written at all when empty: an empty GENTOO_MIRRORS is a shorter
        # list than Portage's own, not the same thing as leaving it alone.
        if listed:
            wanted.append(("GENTOO_MIRRORS", " ".join(listed)))
        existing = context.read(PurePosixPath("/etc/portage/make.conf"))
        context.write(PurePosixPath("/etc/portage/make.conf"), merge(existing, wanted))


def merge(existing: str, wanted: Sequence[tuple[str, str]]) -> str:
    """Replace the keys this installer sets and keep the rest of the file.

    The stage3 ships a make.conf with comments and a CHOST nobody should be
    rewriting, so the file is edited rather than replaced: a key we set is
    substituted in place, and one the file does not mention is appended.
    """
    replacing = dict(wanted)
    kept: list[str] = []
    seen: set[str] = set()
    for line in existing.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in replacing:
            kept.append(f'{key}="{replacing[key]}"')
            seen.add(key)
            continue
        kept.append(line)
    added = [f'{key}="{value}"' for key, value in wanted if key not in seen]
    if added:
        if kept and kept[-1].strip():
            kept.append("")
        kept.append("# Added by gentoo-install.")
        kept += added
    return "\n".join(kept).rstrip("\n") + "\n"


@dataclass(frozen=True, kw_only=True)
class CreateAutounmaskFiles(Operation):
    stage: Stage = Stage.PORTAGE

    def describe(self) -> str:
        return "create the three autounmask files emerge writes its decisions into"

    def apply(self, context: Context) -> None:
        for path in AUTOUNMASK_FILES:
            context.write(PurePosixPath(path), "")


@dataclass(frozen=True, kw_only=True)
class ConfigureRepository(Operation):
    stage: Stage = Stage.PORTAGE
    name: str
    location: PurePosixPath
    sync_uri: str
    verify_commits: bool
    #: `git` or `rsync`, as `sync-type` spells them. `sync-depth` and the
    #: signature keys mean nothing to rsync, so they are written only for git.
    sync_type: str = "git"

    def describe(self) -> str:
        verified = ", commit signatures verified" if self.verify_commits else ""
        return f"point repository {self.name} at {self.sync_uri}{verified}"

    def apply(self, context: Context) -> None:
        stanza = [
            f"[{self.name}]",
            f"location = {self.location}",
            f"sync-type = {self.sync_type}",
            f"sync-uri = {self.sync_uri}",
        ]
        if self.sync_type == "git":
            stanza.append("sync-depth = 1")
        stanza.append("auto-sync = yes")
        if self.verify_commits:
            stanza.append("sync-git-verify-commit-signature = true")
            # Without a key path there is nothing to verify against, and Portage
            # treats the whole sync as unverified rather than failing loudly.
            stanza.append(f"sync-openpgp-key-path = {RELEASE_KEY}")
        context.write(
            PurePosixPath(f"/etc/portage/repos.conf/{self.name}.conf"), "\n".join(stanza) + "\n"
        )


@dataclass(frozen=True, kw_only=True)
class WebrsyncRepository(Operation):
    """The first sync cannot be a git sync: a stage3 has no `dev-vcs/git`, and
    nothing can be merged until a tree exists. `emerge-webrsync` needs neither."""

    stage: Stage = Stage.PORTAGE

    def describe(self) -> str:
        return "fetch the first ebuild repository snapshot with emerge-webrsync"

    def apply(self, context: Context) -> None:
        context.run_in_target(["emerge-webrsync"])


@dataclass(frozen=True, kw_only=True)
class SyncRepository(Operation):
    """A directory holding a copy that git did not create makes `emerge --sync`
    refuse, so the local copy goes first."""

    stage: Stage = Stage.PORTAGE
    name: str
    location: PurePosixPath

    def describe(self) -> str:
        return f"sync repository {self.name}"

    def apply(self, context: Context) -> None:
        context.run_in_target(["rm", "--recursive", "--force", str(self.location)])
        context.run_in_target(["emerge", "--sync", self.name])
        context.run_in_target(["chown", "--recursive", "portage:portage", str(self.location)])


@dataclass(frozen=True, kw_only=True)
class SelectProfile(Operation):
    stage: Stage = Stage.PORTAGE
    profile: str

    def describe(self) -> str:
        return f"select profile {self.profile}"

    def apply(self, context: Context) -> None:
        context.run_in_target(["eselect", "profile", "set", self.profile])


@dataclass(frozen=True, kw_only=True)
class AcceptOverlayKeywords(Operation):
    """gentoo-zh only ever carries `~amd64`, so the keyword is accepted for that
    repository alone. The main tree stays stable."""

    stage: Stage = Stage.PORTAGE
    repository: str

    def describe(self) -> str:
        return f"accept ~amd64 for packages from {self.repository} only"

    def apply(self, context: Context) -> None:
        context.write(
            PurePosixPath(f"/etc/portage/package.accept_keywords/{self.repository}"),
            f"*/*::{self.repository} ~amd64\n",
        )


@dataclass(frozen=True, kw_only=True)
class Emerge(Operation):
    stage: Stage = Stage.PACKAGES
    packages: tuple[str, ...]
    summary: str
    oneshot: bool = False
    binary_packages: bool = True

    def describe(self) -> str:
        how = "" if self.binary_packages else ", from source"
        return f"{self.summary}: emerge {' '.join(self.packages)}{how}"

    def apply(self, context: Context) -> None:
        argv = ["emerge", *EMERGE_OPTIONS]
        if self.oneshot:
            argv.append("--oneshot")
        if self.binary_packages and not context.degraded(BINARY_PACKAGES):
            argv += BINPKG_OPTIONS
        else:
            argv.append("--usepkg=n")
        context.run_in_target([*argv, "--", *self.packages])


@dataclass(frozen=True, kw_only=True)
class PrepareBinhostTrust(Operation):
    """`getuto` creates `/etc/portage/gnupg` and its trustdb. Without it even
    the official host's signatures have nothing to verify against."""

    stage: Stage = Stage.PORTAGE

    def describe(self) -> str:
        return "run getuto so Portage has a keyring to verify binary packages against"

    def apply(self, context: Context) -> None:
        try:
            context.run_in_target(["getuto"])
        except CommandFailed as error:
            # Not fatal by design: the disks are already written by now, and
            # compiling is the guaranteed path a binary host only shortens.
            context.degrade(BINARY_PACKAGES, f"getuto left no keyring to verify against: {error}")


@dataclass(frozen=True, kw_only=True)
class TrustBinhostKey(Operation):
    """A key imported into Portage's keyring stays untrusted until `lsign`, and
    verification then fails the same way it fails with no key at all."""

    stage: Stage = Stage.PORTAGE
    binhost: str
    fingerprint: str
    key_path: PurePosixPath

    def describe(self) -> str:
        return (
            f"import {self.fingerprint[-16:]} from {self.key_path} and locally sign it "
            f"for {self.binhost}"
        )

    def apply(self, context: Context) -> None:
        if context.degraded(BINARY_PACKAGES):
            return
        try:
            self._trust(context)
        except CommandFailed as error:
            # An imported but unsigned key fails verification exactly as a
            # missing key does, so a half-done import degrades the same way.
            context.degrade(
                binhost_trust(self.binhost), f"{self.fingerprint[-16:]} is not trusted: {error}"
            )

    def _trust(self, context: Context) -> None:
        context.run_in_target(
            ["gpg", "--homedir", "/etc/portage/gnupg", "--import", str(self.key_path)]
        )
        context.run_in_target(
            [
                "gpg", "--homedir", "/etc/portage/gnupg",
                "--batch", "--yes",
                "--pinentry-mode", "loopback",
                "--passphrase-file", "/etc/portage/gnupg/pass",
                "--lsign-key", self.fingerprint,
            ]
        )
        context.run_in_target(["gpg", "--homedir", "/etc/portage/gnupg", "--check-trustdb"])


@dataclass(frozen=True, kw_only=True)
class ConfigureBinhost(Operation):
    stage: Stage = Stage.PORTAGE
    name: str
    sync_uri: str
    verify: bool

    def describe(self) -> str:
        signed = "verified" if self.verify else "unverified"
        return f"add {signed} binary package host {self.name} at {self.sync_uri}"

    def apply(self, context: Context) -> None:
        if context.degraded(BINARY_PACKAGES) or context.degraded(binhost_trust(self.name)):
            # Writing the host anyway would leave the installed system pulling
            # binaries it cannot verify.
            return
        stanza = (
            f"[{self.name}]\n"
            f"sync-uri = {self.sync_uri}\n"
            "priority = 10\n"
            f"verify-signature = {'true' if self.verify else 'false'}\n"
            f"location = /var/cache/binhost/{self.name}\n"
        )
        context.write(PurePosixPath(f"/etc/portage/binrepos.conf/{self.name}.conf"), stanza)


@dataclass(frozen=True, kw_only=True)
class VerifyPackages(Operation):
    """Every atom the operator typed has to resolve to an ebuild.

    Asked here, right after the tree is synced and before anything is built: a
    name that matches nothing otherwise stops the run at the packages stage,
    hours in and with the disks already written.
    """

    stage: Stage = Stage.PORTAGE
    packages: tuple[str, ...]

    def describe(self) -> str:
        return f"check that {' '.join(self.packages)} name real packages"

    def apply(self, context: Context) -> None:
        missing: list[str] = []
        for atom in self.packages:
            try:
                context.run_in_target(["emerge", "--pretend", "--quiet", "--nodeps", "--", atom])
            except CommandFailed:
                missing.append(atom)
        if missing:
            raise ConfigError(
                f"no ebuild matches {', '.join(missing)}; check the name, or add the "
                "overlay that carries it"
            )


@dataclass(frozen=True, kw_only=True)
class AcceptTestingPackages(Operation):
    """A third scope beside stable and global testing: named atoms only, so the
    rest of the system keeps the guarantee stable carries."""

    stage: Stage = Stage.PORTAGE
    packages: tuple[str, ...]

    def describe(self) -> str:
        return f"accept ~amd64 for {' '.join(self.packages)} and nothing else"

    def apply(self, context: Context) -> None:
        lines = "".join(f"{atom} ~amd64\n" for atom in self.packages)
        context.write(PurePosixPath("/etc/portage/package.accept_keywords/user"), lines)


@dataclass(frozen=True, kw_only=True)
class AcceptTestingGlobally(Operation):
    """Last, never earlier. Opening `~amd64` before the system is installed
    drags the whole install into an unmask chain."""

    stage: Stage = Stage.FINISH

    def describe(self) -> str:
        return 'append ACCEPT_KEYWORDS="~amd64" to make.conf, after everything is installed'

    def apply(self, context: Context) -> None:
        context.append(PurePosixPath("/etc/portage/make.conf"), 'ACCEPT_KEYWORDS="~amd64"\n')


def build(
    config: InstallConfig,
    mirror: str,
    use: tuple[str, ...] = (),
    video_cards: tuple[str, ...] = (),
    licenses: tuple[str, ...] = (),
) -> list[Operation]:
    portage = config.portage
    gentoo = PurePosixPath("/var/db/repos/gentoo")
    operations: list[Operation] = [
        InstallStage3(mirror=mirror, variant=_variant(config)),
        PrepareChroot(),
    ]
    operations += [
        WriteMakeConf(
            settings=make_conf(config, use, video_cards, licenses),
            mirrors=_distfiles(portage),
            speed_test=portage.mirrors.speed_test,
            appended=_appended_distfiles(portage),
        ),
        CreateAutounmaskFiles(),
        WebrsyncRepository(),
        SelectProfile(profile=portage.profile),
    ]
    if portage.sync is Sync.GIT:
        operations += [
            Emerge(
                stage=Stage.PORTAGE,
                packages=("dev-vcs/git",),
                summary="install git, which every later repository sync needs",
            ),
            ConfigureRepository(
                name="gentoo",
                location=gentoo,
                sync_uri=_repo_sync_uri(portage),
                verify_commits=True,
            ),
            SyncRepository(name="gentoo", location=gentoo),
        ]
    elif portage.sync is Sync.RSYNC:
        # No `dev-vcs/git`: rsync needs none, and the stage3 already has the
        # rsync binary. Signatures are verified per snapshot, not per commit.
        operations += [
            ConfigureRepository(
                name="gentoo",
                location=gentoo,
                sync_uri=_rsync_uri(portage),
                verify_commits=False,
                sync_type="rsync",
            ),
            SyncRepository(name="gentoo", location=gentoo),
        ]
    for overlay in portage.overlays:
        location = PurePosixPath(f"/var/db/repos/{overlay.name}")
        operations += [
            ConfigureRepository(
                name=overlay.name,
                location=location,
                sync_uri=overlay.sync_uri,
                verify_commits=False,
            ),
            SyncRepository(name=overlay.name, location=location),
            AcceptOverlayKeywords(repository=overlay.name),
        ]
    if config.packages.extra:
        operations.append(VerifyPackages(packages=config.packages.extra))
    if portage.testing_packages:
        operations.append(AcceptTestingPackages(packages=portage.testing_packages))
    if _uses_binhost(portage):
        operations.append(PrepareBinhostTrust())
    if portage.binhost.official:
        # Written rather than left to the stage3's default, because that names
        # the profile's baseline and the subarchitecture is a choice here.
        operations.append(
            ConfigureBinhost(
                name="gentoo",
                sync_uri=mirrors.gentoo_binhost(
                    portage.mirrors.region, portage.mirrors.site, portage.binhost.subarch
                ),
                verify=True,
            )
        )
    if portage.binhost.community is not BinhostChannel.OFF:
        operations += [
            Emerge(
                stage=Stage.PORTAGE,
                packages=("sec-keys/openpgp-keys-gentoozh",),
                summary="install the key the community binary packages are signed with",
                binary_packages=False,
            ),
            TrustBinhostKey(
                binhost="gentoo-zh",
                fingerprint=GENTOOZH_FINGERPRINT,
                key_path=GENTOOZH_KEY,
            ),
            ConfigureBinhost(name="gentoo-zh", sync_uri=_binhost_uri(portage), verify=True),
        ]
    return operations


def finish(config: InstallConfig) -> list[Operation]:
    if config.portage.keywords is Keywords.TESTING:
        return [AcceptTestingGlobally()]
    return []


def make_conf(
    config: InstallConfig,
    use: tuple[str, ...] = (),
    video_cards: tuple[str, ...] = (),
    licenses: tuple[str, ...] = (),
) -> tuple[tuple[str, str], ...]:
    """Everything but `GENTOO_MIRRORS`, which is settled when the operation runs.

    `use` carries the flags the selected package groups declare, so a desktop
    profile that needs `wayland` gets it without a second place to edit.
    """
    portage = config.portage
    settings: list[tuple[str, str]] = []
    if portage.common_flags != PortageConfig().common_flags:
        # Left alone at the default: the stage3 already sets these, and its
        # value is the one Gentoo built the binary packages against.
        settings += [
            ("COMMON_FLAGS", portage.common_flags),
            ("CFLAGS", "${COMMON_FLAGS}"),
            ("CXXFLAGS", "${COMMON_FLAGS}"),
            ("FCFLAGS", "${COMMON_FLAGS}"),
            ("FFLAGS", "${COMMON_FLAGS}"),
        ]
    if portage.makeopts:
        settings.append(("MAKEOPTS", portage.makeopts))
    wanted = [*portage.use, *(flag for flag in use if flag not in portage.use)]
    if wanted:
        settings.append(("USE", " ".join(wanted)))
    if portage.cpu_flags:
        settings.append(("CPU_FLAGS_X86", " ".join(portage.cpu_flags)))
    wanted_cards = video_cards or portage.video_cards
    if wanted_cards:
        settings.append(("VIDEO_CARDS", " ".join(wanted_cards)))
    settings += [
        ("ACCEPT_LICENSE", " ".join(licenses or portage.accept_license)),
        ("L10N", " ".join(_l10n(config))),
    ]
    if _uses_binhost(portage):
        settings.append(("FEATURES", "getbinpkg"))
    return tuple(settings)


def _uses_binhost(portage: PortageConfig) -> bool:
    return portage.binhost.official or portage.binhost.community is not BinhostChannel.OFF


def _distfiles(portage: PortageConfig) -> tuple[str, ...]:
    if not portage.mirrors.gentoo_distfiles:
        return ()
    if portage.mirrors.distfiles:
        return portage.mirrors.distfiles
    return mirrors.gentoo_distfiles(portage.mirrors.region, portage.mirrors.site)


def _rsync_uri(portage: PortageConfig) -> str:
    return mirrors.gentoo_rsync_uri(portage.mirrors.region, portage.mirrors.site)


def _appended_distfiles(portage: PortageConfig) -> tuple[str, ...]:
    """gentoo-zh's own distfiles, when they were asked for. They hold the
    sources of that overlay's packages and no main mirror carries them."""
    if not portage.mirrors.gentoo_zh_distfiles:
        return ()
    return mirrors.gentoozh_distfiles(portage.mirrors.gentoo_zh)


def _repo_sync_uri(portage: PortageConfig) -> str:
    return portage.mirrors.repo_sync_uri or mirrors.gentoo_sync_uri(
        portage.mirrors.region, portage.mirrors.site
    )


def _binhost_uri(portage: PortageConfig) -> str:
    return mirrors.gentoozh_binhost(portage.mirrors.gentoo_zh)


def _l10n(config: InstallConfig) -> tuple[str, ...]:
    """`L10N` uses a hyphen and no encoding, so `zh_CN.UTF-8` becomes `zh-CN`."""
    tags: list[str] = []
    for locale in config.system.locales:
        tag = locale.split(".", 1)[0].replace("_", "-")
        if tag not in tags:
            tags.append(tag)
    return tuple(tags)


def _variant(config: InstallConfig) -> str:
    return "systemd" if config.system.init is InitSystem.SYSTEMD else "openrc"
