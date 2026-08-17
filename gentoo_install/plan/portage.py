# SPDX-License-Identifier: GPL-2.0-or-later
"""stage3, the chroot, and everything Portage needs before the first emerge.

The order here is not a preference. `getuto` has to build `/etc/portage/gnupg`
before a key can be imported, an imported key stays untrusted until `lsign`, and
`package.use/zz-autounmask` has to exist before the emerge that writes into it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
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
    ProxyConfig,
    Sync,
)
from ..model.validate import parse_profile_list, validate_profile
from .operations import CommandOutput, Context, Operation, Stage

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

#: Where Gentoo publishes the keys gemato refreshes.
KEY_SERVER: Final[str] = "hkps://keys.gentoo.org"

PROXY_BOOTSTRAP: Final[PurePosixPath] = PurePosixPath(
    "/etc/gentoo-install/proxy.toml"
)
CURL_PROXY_CONFIG: Final[PurePosixPath] = PurePosixPath(
    "/etc/gentoo-install/curl-proxy.conf"
)

FETCHCOMMAND: Final[str] = (
    'wget -t 3 -T 60 --passive-ftp -U "Portage (Gentoo, '
    'https://www.gentoo.org) distfile-fetch" -O "${DISTDIR}/${FILE}" "${URI}"'
)
RESUMECOMMAND: Final[str] = (
    'wget -c -t 3 -T 60 --passive-ftp -U "Portage (Gentoo, '
    'https://www.gentoo.org) distfile-fetch" -O "${DISTDIR}/${FILE}" "${URI}"'
)
#: `-K` rather than the command line: `/proc/<pid>/cmdline` is world readable
#: and the file this names carries the proxy's password. curl treats a missing
#: file as an error, and `WriteProxyClients` writes it before the first fetch.
CURL_FETCHCOMMAND: Final[str] = (
    f'curl -K {CURL_PROXY_CONFIG} --fail --location --retry 3 '
    '--connect-timeout 60 --output '
    '"${DISTDIR}/${FILE}" "${URI}"'
)
CURL_RESUMECOMMAND: Final[str] = (
    f'curl -K {CURL_PROXY_CONFIG} --fail --location --retry 3 '
    '--connect-timeout 60 --continue-at - --output '
    '"${DISTDIR}/${FILE}" "${URI}"'
)

#: What is absent matters as much as what is here, and the reason for each
#: absence is the fixed-emerge-options section of docs/design.md.
EMERGE_OPTIONS: Final[tuple[str, ...]] = (
    "--verbose",
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
BINPKG_EXCLUDED: Final[str] = "acct-*/* virtual/*"
BINPKG_OPTIONS: Final[tuple[str, ...]] = (
    "--getbinpkg=y",
    "--binpkg-changed-deps=y",
    "--usepkg-exclude",
    BINPKG_EXCLUDED,
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
    proxy: ProxyConfig = ProxyConfig()
    #: The rest of the region's sites, in order. A mirror that cannot be
    #: reached at all ends that mirror, not the install: the stage3 fetch was
    #: the one step in the whole plan with no second address to try.
    fallbacks: tuple[str, ...] = ()

    def describe(self) -> str:
        route = f" via {self.proxy.redacted_url}" if self.proxy.enabled else " directly"
        return (
            f"download the newest {self.variant} stage3 from {self.mirror}{route}, verify it "
            f"against {RELENG_FINGERPRINT[-16:]} and unpack it into the target"
        )

    def apply(self, context: Context) -> None:
        context.write(
            PROXY_BOOTSTRAP,
            _proxy_toml(self.proxy),
            mode=0o600,
        )
        archive = context.fetch_stage3(
            self.mirror, self.variant, RELENG_FINGERPRINT, self.fallbacks
        )
        context.run(
            [
                "tar", "--extract",
                "--file", str(archive),
                "--directory", str(context.target),
                "--preserve-permissions",
                "--xattrs-include=*.*",
                "--numeric-owner",
                # A stage3 takes minutes to unpack and tar says nothing while
                # it does, so a watchdog reading the console ends the guest.
                f"--checkpoint={UNPACK_CHECKPOINT}",
                "--checkpoint-action=echo",
            ]
        )


@dataclass(frozen=True, kw_only=True)
class WriteProxyClients(Operation):
    """Configure clients that Portage invokes without putting credentials in argv."""

    stage: Stage = Stage.PORTAGE
    proxy: ProxyConfig

    def describe(self) -> str:
        route = self.proxy.redacted_url if self.proxy.enabled else "direct connection"
        return f"configure Portage, wget, curl, git and gpg for {route}"

    def apply(self, context: Context) -> None:
        proxy = self.proxy
        # A machine with no proxy keeps the files its distribution shipped.
        if not proxy.enabled:
            return
        endpoint = _proxy_endpoint(proxy)
        bypass = ",".join(proxy.bypass)
        context.write(
            PurePosixPath("/etc/wgetrc"),
            (
                f"use_proxy = on\nhttp_proxy = {endpoint}\nhttps_proxy = {endpoint}\n"
                f"no_proxy = {bypass}\nproxy_user = {proxy.username}\n"
                f"proxy_password = {proxy.password}\n"
                if proxy.over_http
                else "use_proxy = off\n"
            ),
            mode=0o600,
        )
        context.write(
            CURL_PROXY_CONFIG,
            f'proxy = "{endpoint}"\n'
            f'noproxy = "{bypass}"\n'
            f'proxy-user = "{proxy.username}:{proxy.password}"\n',
            mode=0o600,
        )
        context.write(
            PurePosixPath("/etc/gitconfig"),
            "[http]\n"
            f"\tproxy = {proxy.url}\n"
            f"\tnoProxy = {bypass}\n",
            mode=0o600,
        )
        context.write(
            PROXY_BOOTSTRAP,
            _proxy_toml(proxy),
            mode=0o600,
        )


@dataclass(frozen=True, kw_only=True)
class MountChrootFilesystems(Operation):
    """`/run` is bound and made slave rather than mounted as a fresh tmpfs: that
    is the Handbook's form and it keeps the installing system's udev reachable.

    `/sys` is recursive, so the target gets efivarfs with it and `efibootmgr`
    inside the chroot can write a boot entry.
    """

    stage: Stage = Stage.CHROOT

    @property
    def survives_a_reboot(self) -> bool:
        # The failure cleanup unmounts the target recursively, this included,
        # so a resumed run that skipped it ran every chroot command against
        # the live medium's own /proc, /sys and /dev.
        return False

    def describe(self) -> str:
        return "mount transient proc, sys, dev and run filesystems into the target"

    def apply(self, context: Context) -> None:
        target = context.target
        context.run(["mount", "--types", "proc", "/proc", str(target / "proc")])
        for source, propagation in (("/sys", "rslave"), ("/dev", "rslave"), ("/run", "slave")):
            where = str(target / source.lstrip("/"))
            context.run(["mount", "--rbind" if propagation == "rslave" else "--bind", source, where])
            context.run(["mount", f"--make-{propagation}", where])


@dataclass(frozen=True, kw_only=True)
class SeedResolver(Operation):
    """Give chrooted commands working DNS until the target's own resolver is
    configured at the end of the system stage."""

    stage: Stage = Stage.CHROOT

    @property
    def survives_a_reboot(self) -> bool:
        # This is an ordinary file in the target and remains there until
        # LinkResolvConf replaces it after the last emerge.
        return True

    def describe(self) -> str:
        return "seed the target's resolv.conf from the install medium"

    def apply(self, context: Context) -> None:
        target = context.target
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


class PortageConfigKind(Enum):
    USE = "package.use"
    KEYWORDS = "package.accept_keywords"
    LICENSE = "package.license"
    UNMASK = "package.unmask"


@dataclass(frozen=True, kw_only=True)
class WritePortageConfig(Operation):
    """Write one named Portage configuration fragment."""

    stage: Stage = Stage.PORTAGE
    kind: PortageConfigKind
    name: str
    lines: tuple[str, ...]

    @property
    def path(self) -> PurePosixPath:
        return PurePosixPath(f"/etc/portage/{self.kind.value}/{self.name}")

    def describe(self) -> str:
        return f"write {self.path}"

    def apply(self, context: Context) -> None:
        context.write(self.path, "".join(f"{line}\n" for line in self.lines))


#: Values bash has to expand when it sources make.conf rather than read back as
#: text. `CFLAGS="${COMMON_FLAGS}"` is the stage3's own idiom, and escaping it
#: handed gcc the literal `${COMMON_FLAGS}` as a filename: every source build
#: stopped at `linker input file not found`.
EXPANDED: Final[frozenset[str]] = frozenset({"CFLAGS", "CXXFLAGS", "FCFLAGS", "FFLAGS"})


def quoted(key: str, value: str) -> str:
    """One make.conf value, as bash reads it back.

    Portage sources the file, so an unescaped `"` ends the value early and an
    unescaped `$` expands there instead of when the command runs. `FETCHCOMMAND`
    holds both and needs the escaping make.globals gives it; make.globals leaves
    the four flag variables unescaped for the same reason this does.
    """
    if key in EXPANDED:
        return f'"{value}"'
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def merge(existing: str, wanted: Sequence[tuple[str, str]]) -> str:
    """Replace the keys this installer sets and keep the rest of the file.

    The stage3 ships a make.conf with comments and a CHOST nobody should be
    rewriting, so the file is edited rather than replaced: a key named here is
    substituted in place, and one the file does not mention is appended.
    """
    replacing = dict(wanted)
    kept: list[str] = []
    seen: set[str] = set()
    for line in existing.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in replacing:
            kept.append(f"{key}={quoted(key, replacing[key])}")
            seen.add(key)
            continue
        kept.append(line)
    added = [f"{key}={quoted(key, value)}" for key, value in wanted if key not in seen]
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
class ConfigureWebrsyncRepository(Operation):
    """Persist webrsync without a URI, which Portage's module does not consume."""

    stage: Stage = Stage.PORTAGE
    name: str
    location: PurePosixPath

    def describe(self) -> str:
        return f"configure repository {self.name} to sync with emerge-webrsync"

    def apply(self, context: Context) -> None:
        stanza = (
            f"[{self.name}]\n"
            f"location = {self.location}\n"
            "sync-type = webrsync\n"
            "auto-sync = yes\n"
            "sync-webrsync-verify-signature = true\n"
            f"sync-openpgp-key-path = {RELEASE_KEY}\n"
        )
        context.write(PurePosixPath(f"/etc/portage/repos.conf/{self.name}.conf"), stanza)


@dataclass(frozen=True, kw_only=True)
class WebrsyncRepository(Operation):
    """The first sync cannot be a git sync: a stage3 has no `dev-vcs/git`, and
    nothing can be merged until a tree exists. `emerge-webrsync` needs neither."""

    stage: Stage = Stage.PORTAGE

    def describe(self) -> str:
        return "fetch the first ebuild repository snapshot with emerge-webrsync"

    def apply(self, context: Context) -> None:
        # Portage owns this policy in the stage3; overriding it can select a
        # server incompatible with the installed gemato configuration.
        # `portageq` cannot answer before this operation runs, because
        # `repos.conf` names `/var/db/repos/gentoo` and no snapshot has created
        # it yet, so an unreadable policy means no override rather than a fault.
        policy = context.run_in_target(
            ["portageq", "envvar", "PORTAGE_GPG_KEY_SERVER"], check=False
        )
        readable = isinstance(policy, CommandOutput) and policy.returncode == 0
        # Gentoo's own when the stage3 names none: gemato tries WKD first and
        # falls back to a keyserver, and with none configured a WKD that does
        # not answer ends the install at `No keyserver available`.
        server = (policy.strip() if readable else "") or KEY_SERVER
        # The same patience the later syncs get, and this one needs it more: it
        # downloads the whole snapshot, looks the signing key up over WKD and
        # refreshes it from a keyserver, and gemato lets a `ReadTimeout` in the
        # WKD lookup out rather than falling back. `openrc-sdboot` ended a
        # cluster round there with the tree never fetched.
        last: CommandFailed | None = None
        for attempt in range(SYNC_TRIES):
            try:
                context.run_in_target(
                    ["env", f"PORTAGE_GPG_KEY_SERVER={server}", "emerge-webrsync"]
                )
                return
            except CommandFailed as failed:
                last = failed
                if attempt + 1 < SYNC_TRIES:
                    context.run(["sleep", f"{SYNC_PAUSE * (attempt + 1):g}"])
        assert last is not None
        raise last


#: Records between the lines tar prints while unpacking. One every few
#: seconds on a slow disk, which is often enough for a watchdog and rare
#: enough not to fill the log.
UNPACK_CHECKPOINT: Final[int] = 20000

#: How many times a repository sync is attempted, and how long between them.
#: A mirror rewriting its Manifests is the transient case; two more attempts a
#: minute apart cover it without walking around a mismatch that is real.
SYNC_TRIES: Final[int] = 3
SYNC_PAUSE: Final[float] = 30.0

#: Portage's own default is 180, and a mirror that queues rather than refuses
#: spends longer than that before it sends a byte: USTC answers `Upstream
#: mirrors4 has reached the maximum number of 60 connections. Your request is
#: being queued.` and rsync was killed at position 3 of 4.
RSYNC_TIMEOUT: Final[int] = 900


def _unversioned(atom: str) -> str:
    """The package name inside an atom, for an option that takes no version.

    `--usepkg-exclude` accepts package names and slot atoms only, and a pinned
    kernel reaches it as `=sys-kernel/gentoo-cjk-kernel-bin-7.1.7`: emerge then
    answers `Invalid Atom(s)` and the install stops with the disks written.
    """
    name = atom.lstrip("=<>~!")
    category, _, rest = name.partition("/")
    if not rest:
        return name
    # `-7.1.7` and `-7.1.7-r2` alike: the version starts at the last hyphen
    # followed by a digit, which is what `package-1.2-r3` gives portage too.
    trimmed = re.sub(r"-\d[^-]*(-r\d+)?$", "", rest)
    return f"{category}/{trimmed}"


class _SourceMode(Enum):
    BINARIES_ALLOWED = "binaries allowed"
    BUILD_ALL = "build all"
    BUILD_SUBSET = "build subset"


class InstallMode(Enum):
    NORMAL = ""
    ONESHOT = "--oneshot"
    NOREPLACE = "--noreplace"


@dataclass(frozen=True, kw_only=True)
class SourcePolicy:
    """Which requested packages may use binaries.

    Dependencies may still use the binhost when requested packages are built.
    """

    mode: _SourceMode
    subset: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode is _SourceMode.BUILD_SUBSET:
            if not self.subset:
                raise ValueError("a source subset cannot be empty")
        elif self.subset:
            raise ValueError(f"{self.mode.value} cannot carry a source subset")

    @classmethod
    def binaries_allowed(cls) -> SourcePolicy:
        return cls(mode=_SourceMode.BINARIES_ALLOWED)

    @classmethod
    def build_all(cls) -> SourcePolicy:
        return cls(mode=_SourceMode.BUILD_ALL)

    @classmethod
    def build_subset(cls, packages: tuple[str, ...]) -> SourcePolicy:
        return cls(mode=_SourceMode.BUILD_SUBSET, subset=packages)

    def built_from(self, packages: tuple[str, ...]) -> tuple[str, ...]:
        if self.mode is _SourceMode.BINARIES_ALLOWED:
            return ()
        if self.mode is _SourceMode.BUILD_ALL:
            return packages
        outside = tuple(atom for atom in self.subset if atom not in packages)
        if outside:
            raise ValueError(
                f"source subset contains atoms outside packages: {' '.join(outside)}"
            )
        return self.subset


#: What a sync failure says when the site was never reached, so retrying the
#: same one is pointless and another site is worth trying. Taken from what git
#: printed on a cluster guest that could not reach the overlay mirror: `fatal:
#: unable to access 'https://mirror.nju.edu.cn/git/gentoo-zh.git/': Failed to
#: connect to mirror.nju.edu.cn:443 after 2052 ms: Could not connect to
#: server`. Every marker names the transport; none of them names content, so a
#: mirror mid-update never matches.
UNREACHABLE_MARKERS: Final[tuple[str, ...]] = (
    "unable to access",
    "could not connect to server",
    "failed to connect",
    "could not resolve host",
    "name or service not known",
    "connection refused",
    "connection timed out",
    "network is unreachable",
)


def site_unreachable(message: str) -> bool:
    """Whether a sync failure was the site rather than what it served."""
    lowered = message.lower()
    return any(marker in lowered for marker in UNREACHABLE_MARKERS)


@dataclass(frozen=True, kw_only=True)
class SyncRepository(Operation):
    """A directory holding a copy that git did not create makes `emerge --sync`
    refuse, so the local copy goes first."""

    stage: Stage = Stage.PORTAGE
    name: str
    location: PurePosixPath
    #: Other sites carrying the same repository, tried in order when the
    #: configured one cannot be reached. Each one is a whole stanza rather than
    #: a bare address, so `ConfigureRepository` stays the only writer of that
    #: file.
    alternates: tuple[ConfigureRepository, ...] = ()

    def describe(self) -> str:
        if not self.alternates:
            return f"sync repository {self.name}"
        return f"sync repository {self.name}, {len(self.alternates)} other sites to fall back on"

    def apply(self, context: Context) -> None:
        context.run_in_target(["rm", "--recursive", "--force", str(self.location)])
        self._sync(context)
        context.run_in_target(["chown", "--recursive", "portage:portage", str(self.location)])

    def _sync(self, context: Context) -> None:
        """The configured site, then each alternate that a reachability failure
        makes worth trying.

        A site that was not reached will not be reached by waiting, and a
        cluster guest spent fifteen minutes on three attempts at one host
        before the install stopped. A mirror that answers and serves an
        incomplete snapshot is the opposite case and is retried where it is.
        """
        try:
            self._attempt(context)
            return
        except CommandFailed as failed:
            last = failed
        for alternate in self.alternates:
            if not site_unreachable(str(last)):
                raise last
            alternate.apply(context)
            # git refuses to clone into a directory it did not create, and the
            # failed attempt may have left one.
            context.run_in_target(["rm", "--recursive", "--force", str(self.location)])
            try:
                self._attempt(context)
                return
            except CommandFailed as failed:
                last = failed
        raise last

    def _attempt(self, context: Context) -> None:
        """One site's `emerge --sync`, retried while it is mid-update.

        Three of eight guests stopped in the same minute with `Manifest
        mismatch for gui-apps/Manifest.gz __size__: expected: 5745, have:
        5746`. Portage quarantined the download and refused, which is correct:
        the snapshot it fetched was incomplete, not the tree corrupted. The
        same command a minute later reads a whole one.

        The same mirror, and a bounded number of times: a mismatch that is not
        transient has to stop the install rather than be walked around.
        """
        last: CommandFailed | None = None
        for attempt in range(SYNC_TRIES):
            try:
                context.run_in_target(
                    ["env", f"RSYNC_TIMEOUT={RSYNC_TIMEOUT}", "emerge", "--sync", self.name]
                )
                return
            except CommandFailed as failed:
                last = failed
                if site_unreachable(str(failed)):
                    raise
                if attempt + 1 < SYNC_TRIES:
                    context.run(["sleep", f"{SYNC_PAUSE * (attempt + 1):g}"])
        assert last is not None
        raise last


@dataclass(frozen=True, kw_only=True)
class SelectProfile(Operation):
    stage: Stage = Stage.PORTAGE
    profile: str

    def describe(self) -> str:
        return f"select profile {self.profile}"

    def apply(self, context: Context) -> None:
        source = "`eselect profile list` in the target"
        listed = context.run_in_target(["eselect", "profile", "list"], check=False)
        if isinstance(listed, CommandOutput) and listed.returncode != 0:
            reason = listed.strip() or f"exit {listed.returncode} with no output"
            raise CommandFailed(f"{source} could not be read: {reason}")
        profiles = parse_profile_list(listed, source=source)
        validate_profile(self.profile, tuple(profile.path for profile in profiles))
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
        WritePortageConfig(
            kind=PortageConfigKind.KEYWORDS,
            name=self.repository,
            lines=(f"*/*::{self.repository} ~amd64",),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class Emerge(Operation):
    stage: Stage = Stage.PACKAGES
    packages: tuple[str, ...]
    summary: str
    requester: str = ""
    repository_bootstrap: bool = False
    mode: InstallMode = InstallMode.NORMAL
    source: SourcePolicy = SourcePolicy.binaries_allowed()

    def __post_init__(self) -> None:
        self.source.built_from(self.packages)

    @property
    def package_requester(self) -> str:
        return self.requester or f"the `{self.summary}` operation"

    def describe(self) -> str:
        built = self._built_here()
        how = f", building {' '.join(built)} here" if built else ""
        if built == self.packages:
            how = ", from source"
        return f"{self.summary}: emerge {' '.join(self.packages)}{how}"

    def _built_here(self) -> tuple[str, ...]:
        return self.source.built_from(self.packages)

    def apply(self, context: Context) -> None:
        command = self._argv(context, source_only=context.degraded(BINARY_PACKAGES))
        try:
            result = context.run_in_target(command, check=False)
        except CommandFailed as error:
            marker = _binpkg_failure(str(error))
            if marker is None or context.degraded(BINARY_PACKAGES):
                raise
            if (again := self._one_more_binary_try(context, command)) is not None:
                marker = again
            else:
                return
            context.degrade(BINARY_PACKAGES, f"selected binary package failed: {marker}")
            retry_result = context.run_in_target(self._argv(context, source_only=True), check=False)
            if isinstance(retry_result, CommandOutput) and retry_result.returncode != 0:
                raise CommandFailed(
                    f"source retry ended with {retry_result.ending}: "
                    f"{str(retry_result).strip()}"
                )
            return
        if not isinstance(result, CommandOutput) or result.returncode == 0:
            return
        if not _binpkg_failure(str(result)) or context.degraded(BINARY_PACKAGES):
            raise CommandFailed(f"emerge ended with {result.ending}: {str(result).strip()}")
        marker = _binpkg_failure(str(result))
        if (again := self._one_more_binary_try(context, command)) is not None:
            marker = again
        else:
            return
        reason = f"selected binary package failed: {marker}"
        context.degrade(BINARY_PACKAGES, reason)
        retry = self._argv(context, source_only=True)
        retry_result = context.run_in_target(retry, check=False)
        if isinstance(retry_result, CommandOutput) and retry_result.returncode != 0:
            raise CommandFailed(
                f"source retry ended with {retry_result.ending}: {str(retry_result).strip()}"
            )

    def _one_more_binary_try(self, context: Context, command: list[str]) -> str | None:
        """Run the same emerge again, and answer what still names a binary
        failure, or `None` when the second run succeeded.

        One dropped TLS handshake would otherwise compile a whole group from
        source: `libtommath` failed that way inside the plasma group, and
        rebuilding all of it costs hours against a package that downloads in
        seconds.
        """
        try:
            again = context.run_in_target(command, check=False)
        except CommandFailed as error:
            return _binpkg_failure(str(error)) or str(error).strip()
        if not isinstance(again, CommandOutput) or again.returncode == 0:
            return None
        return _binpkg_failure(str(again)) or str(again).strip()

    def _argv(self, context: Context, *, source_only: bool) -> list[str]:
        argv = ["emerge", *EMERGE_OPTIONS]
        if self.mode.value:
            argv.append(self.mode.value)
        if source_only:
            argv += ["--usepkg=n", "--getbinpkg=n"]
        elif built := self._built_here():
            argv += [
                *BINPKG_OPTIONS[:-1],
                f"{BINPKG_EXCLUDED} {' '.join(_unversioned(one) for one in built)}",
            ]
        else:
            argv += BINPKG_OPTIONS
        return [*argv, "--", *self.packages]


#: Portage's own binary package extension, which is what proves a fetch was a
#: binary one when the path is the only line naming it.
GPKG_SUFFIX: Final[str] = ".gpkg.tar"


def _binpkg_failure(output: str) -> str | None:
    """Return a Portage marker that proves the failure was a binary fetch."""
    binaries: set[str] = set()
    pending = ""
    for raw in output.splitlines():
        line = raw.strip()
        if any(
            marker in line
            for marker in (
                "Fetching Binary failed",
                "Binary package is not usable",
                "Fetching Binary",
            )
        ):
            return line
        # Portage says none of those when the download itself breaks: it
        # prints wget's `Unable to establish SSL connection.` and fails the
        # package. What names it a binary is the line that started it, so the
        # two are read together. `btrfs-luks` lost an hour of install there.
        if started := re.match(r">>> Emerging binary \(\d+ of \d+\) (\S+)", line):
            binaries.add(started.group(1).split("::")[0])
        # A package whose binary is fetched in the background never reaches
        # `Emerging binary`, and its failure line carries no comma: `vm-desktop`
        # stopped at `>>> Failed to emerge net-wireless/wireless-regdb-20251007`
        # with `.gpkg.tar.partial` as the only proof it was a binary.
        if checking := re.match(r">>> Running pre-merge checks for (\S+)", line):
            pending = checking.group(1).split("::")[0]
        elif GPKG_SUFFIX in line and pending:
            binaries.add(pending)
        if failed := re.match(r">>> Failed to emerge (\S+?)(?:,|$)", line):
            if failed.group(1) in binaries:
                return line
    return None


@dataclass(frozen=True, kw_only=True)
class PrepareBinhostTrust(Operation):
    """`getuto` creates `/etc/portage/gnupg` and its trustdb. Without it even
    the official host's signatures have nothing to verify against."""

    stage: Stage = Stage.PORTAGE
    proxy: ProxyConfig = ProxyConfig()

    def describe(self) -> str:
        return "run getuto so Portage has a keyring to verify binary packages against"

    def apply(self, context: Context) -> None:
        try:
            context.run_in_target(["getuto"])
        except CommandFailed as error:
            # Not fatal by design: the disks are already written by now, and
            # compiling is the guaranteed path a binary host only shortens.
            context.degrade(BINARY_PACKAGES, f"getuto left no keyring to verify against: {error}")
        # After getuto, never before: it rebuilds the whole directory from a
        # staging copy, so anything written there earlier is discarded. Its own
        # file carries `honor-http-proxy`, and the endpoint in the environment
        # has no credentials, so the tree's signature check answered
        # `keyserver refresh failed: Not authenticated`.
        if self.proxy.over_http:
            context.write(
                PurePosixPath("/etc/portage/gnupg/dirmngr.conf"),
                f"honor-http-proxy\nhttp-proxy {self.proxy.url}\n",
                mode=0o600,
            )


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
        filename = "gentoobinhost.conf" if self.name == "gentoo" else f"{self.name}.conf"
        context.write(PurePosixPath(f"/etc/portage/binrepos.conf/{filename}"), stanza)


@dataclass(frozen=True, kw_only=True)
class PackageRequest:
    atom: str
    requesters: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class VerifyPackages(Operation):
    """Repository bootstrap merges make repositories reachable, so they precede this check."""

    stage: Stage = Stage.PORTAGE
    requests: tuple[PackageRequest, ...]

    def describe(self) -> str:
        return (
            f"resolve {' '.join(request.atom for request in self.requests)} together "
            "before installing the requested packages"
        )

    def apply(self, context: Context) -> None:
        atoms = tuple(request.atom for request in self.requests)
        output = self._resolve(context, atoms, source_only=context.degraded(BINARY_PACKAGES))
        if output.returncode == 0:
            return
        output = self._without_the_binary_host(context, atoms, output)
        if output.returncode == 0:
            return

        problems: list[str] = []
        for request in self.requests:
            available = context.run_in_target(
                [
                    "portageq",
                    "pquery",
                    "--no-version",
                    "--no-filters",
                    request.atom,
                ]
            )
            if not available.strip():
                problems.append(
                    f"{_requesters_ask(request)} for `{request.atom}`, which the target's "
                    "repositories do not carry"
                )
                continue
            visible = context.run_in_target(
                ["portageq", "best_visible", "/", request.atom], check=False
            )
            if not isinstance(visible, CommandOutput):
                raise ConfigError("portageq best_visible returned no exit status")
            if visible.returncode == 1:
                problems.append(
                    f"{_requesters_ask(request)} for `{request.atom}`, which the target's "
                    "configuration masks"
                )
            elif visible.returncode != 0:
                raise ConfigError(
                    f"portageq best_visible failed for `{request.atom}` with exit "
                    f"{visible.returncode}"
                )
        if problems:
            raise ConfigError("; ".join(problems))

        detail = next(
            (line.strip().removeprefix("!!! ") for line in output.splitlines() if line.strip()),
            "no output",
        )
        requesters = tuple(
            dict.fromkeys(
                requester for request in self.requests for requester in request.requesters
            )
        )
        raise ConfigError(
            f"emerge --pretend rejected packages requested by {', '.join(requesters)}: {detail}"
        )

    def _resolve(
        self, context: Context, atoms: tuple[str, ...], *, source_only: bool
    ) -> CommandOutput:
        without = ("--usepkg=n", "--getbinpkg=n") if source_only else ()
        output = context.run_in_target(
            ["emerge", "--pretend", "--quiet", *without, "--", *atoms], check=False
        )
        if not isinstance(output, CommandOutput):
            raise ConfigError("emerge --pretend returned no exit status")
        return output

    def _without_the_binary_host(
        self, context: Context, atoms: tuple[str, ...], failed: CommandOutput
    ) -> CommandOutput:
        """Resolve again with binaries off when the host's index was the only
        thing that could not be read.

        `--pretend` fails on an unreadable index, so `vm-gnome` stopped six
        minutes in with every requested package present and visible. Source is
        the guaranteed path, so an index that cannot be read degrades to it
        rather than ending the install.
        """
        unreadable = _binhost_unreadable(str(failed))
        if unreadable is None or context.degraded(BINARY_PACKAGES):
            return failed
        # The same retry `Emerge` makes before giving up on binaries: one
        # dropped connection would otherwise compile the whole install.
        again = self._resolve(context, atoms, source_only=False)
        if again.returncode == 0 or _binhost_unreadable(str(again)) is None:
            return again
        context.degrade(BINARY_PACKAGES, f"binary host index unreadable: {unreadable}")
        return self._resolve(context, atoms, source_only=True)


#: What Portage prints when it cannot read a binary host's index, taken from
#: what stopped `vm-gnome`: `!!! [gentoo] Error fetching binhost package info
#: from 'https://mirrors.nju.edu.cn/gentoo/releases/amd64/binpackages/23.0/
#: x86-64'`, followed by `!!! [gentoo] <urlopen error timed out>`.
BINHOST_INDEX_FAILURE: Final[str] = "Error fetching binhost package info"


def _binhost_unreadable(output: str) -> str | None:
    """The line proving the failure was the binary host and not a package."""
    for raw in output.splitlines():
        line = raw.strip()
        if BINHOST_INDEX_FAILURE in line:
            return line.removeprefix("!!! ")
    return None


def _requesters_ask(request: PackageRequest) -> str:
    if len(request.requesters) == 1:
        return f"{request.requesters[0]} asks"
    return f"{', '.join(request.requesters[:-1])} and {request.requesters[-1]} ask"


@dataclass(frozen=True, kw_only=True)
class AcceptTestingPackages(Operation):
    """A third scope beside stable and global testing: named atoms only, so the
    rest of the system keeps the guarantee stable carries."""

    stage: Stage = Stage.PORTAGE
    packages: tuple[str, ...]

    def describe(self) -> str:
        return f"accept ~amd64 for {' '.join(self.packages)} and nothing else"

    def apply(self, context: Context) -> None:
        WritePortageConfig(
            kind=PortageConfigKind.KEYWORDS,
            name="user",
            lines=tuple(f"{atom} ~amd64" for atom in self.packages),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class AcceptTestingGlobally(Operation):
    """Last, never earlier. Opening `~amd64` before the system is installed
    drags the whole install into an unmask chain."""

    stage: Stage = Stage.FINISH

    def describe(self) -> str:
        return 'append ACCEPT_KEYWORDS="~amd64" to make.conf, after everything is installed'

    def apply(self, context: Context) -> None:
        context.append(PurePosixPath("/etc/portage/make.conf"), 'ACCEPT_KEYWORDS="~amd64"\n')


def _other_mirrors(config: InstallConfig, chosen: str) -> tuple[str, ...]:
    """Every other site of the configured region, in its order.

    The stage3 fetch was the one step with a single address: a mirror whose
    name did not resolve ended the install three minutes in, with the disks
    already partitioned and mounted.
    """
    sites = mirrors.gentoo_sites(config.portage.mirrors.region)
    return tuple(
        site.distfiles
        for site in sites
        if site.distfiles and site.distfiles.rstrip("/") != chosen.rstrip("/")
    )


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
        InstallStage3(
            mirror=mirror,
            variant=variant_of(config),
            proxy=config.proxy,
            fallbacks=_other_mirrors(config, mirror),
        ),
        MountChrootFilesystems(),
        SeedResolver(),
    ]
    operations += [
        WriteMakeConf(
            settings=make_conf(config, use, video_cards, licenses),
            mirrors=_distfiles(portage),
            speed_test=portage.mirrors.speed_test,
            appended=_appended_distfiles(portage),
        ),
        WriteProxyClients(proxy=config.proxy),
        CreateAutounmaskFiles(),
    ]
    if _uses_binhost(portage):
        # Before the first emerge, not after it. `make.conf` above already
        # carries `FEATURES=getbinpkg`, so `emerge dev-vcs/git` fetches binary
        # packages; without the keyring a profile with
        # `binpkg-request-signature` refuses the merge, and without it a
        # package is installed unverified three operations before the trust
        # setup that exists to prevent exactly that.
        operations.append(PrepareBinhostTrust(proxy=config.proxy))
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
    operations += [
        WebrsyncRepository(),
        SelectProfile(profile=portage.profile),
    ]
    if portage.sync is Sync.GIT:
        operations += [
            Emerge(
                stage=Stage.PORTAGE,
                packages=("dev-vcs/git",),
                summary="install git, which every later repository sync needs",
                repository_bootstrap=True,
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
        #
        # No sync here either. `emerge-webrsync` has just placed a signed
        # snapshot, and `SyncRepository` deletes it and fetches the same tree
        # again over rsync. Twelve guests behind one address did that at once
        # and rsync.gentoo.org answered `access denied ... from UNKNOWN`, which
        # its own MOTD warns about. The repository is configured so the
        # installed system syncs, and the snapshot is what this install uses.
        operations.append(
            ConfigureRepository(
                name="gentoo",
                location=gentoo,
                sync_uri=_repo_sync_uri(portage),
                verify_commits=False,
                sync_type="rsync",
            )
        )
    elif portage.sync is Sync.WEBRSYNC:
        operations.append(
            ConfigureWebrsyncRepository(
                name="gentoo",
                location=gentoo,
            )
        )
    if portage.overlays and portage.sync is not Sync.GIT:
        # Every overlay is a git repository whichever way the main tree syncs,
        # and a stage3 has no git: without this the first overlay sync fails.
        operations.append(
            Emerge(
                stage=Stage.PORTAGE,
                packages=("dev-vcs/git",),
                summary="install git, which the overlays are cloned with",
                repository_bootstrap=True,
            )
        )
    for overlay in portage.overlays:
        location = PurePosixPath(f"/var/db/repos/{overlay.name}")
        operations += [
            ConfigureRepository(
                name=overlay.name,
                location=location,
                sync_uri=overlay.sync_uri,
                verify_commits=False,
            ),
            SyncRepository(
                name=overlay.name,
                location=location,
                alternates=tuple(
                    ConfigureRepository(
                        name=overlay.name,
                        location=location,
                        sync_uri=uri,
                        verify_commits=False,
                    )
                    for uri in mirrors.overlay_sync_uris(
                        overlay.name, portage.mirrors.gentoo_zh
                    )
                    if uri != overlay.sync_uri
                ),
            ),
            AcceptOverlayKeywords(repository=overlay.name),
        ]
    if portage.testing_packages:
        # Before the check below: an atom accepted as testing is one the
        # verifier would otherwise report as having no ebuild at all.
        operations.append(AcceptTestingPackages(packages=portage.testing_packages))
    if portage.binhost.community is not BinhostChannel.OFF:
        operations += [
            Emerge(
                stage=Stage.PORTAGE,
                packages=("sec-keys/openpgp-keys-gentoozh",),
                summary="install the key the community binary packages are signed with",
                source=SourcePolicy.build_all(),
                repository_bootstrap=True,
            ),
            TrustBinhostKey(
                binhost="gentoo-zh",
                fingerprint=GENTOOZH_FINGERPRINT,
                key_path=GENTOOZH_KEY,
            ),
            ConfigureBinhost(name="gentoo-zh", sync_uri=community_binhost(portage), verify=True),
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
    if portage.input_devices:
        settings.append(("INPUT_DEVICES", " ".join(portage.input_devices)))
    settings += [
        ("ACCEPT_LICENSE", " ".join(licenses or portage.accept_license)),
        ("L10N", " ".join(_l10n(config))),
    ]
    # Only when a proxy is configured: `emerge-webrsync` runs `FETCHCOMMAND`
    # for a snapshot whose URL it supplies itself, and the wget line written
    # here has none, so it answered `missing URL` and no tree arrived.
    proxy = config.proxy
    if proxy.enabled:
        endpoint = _proxy_endpoint(proxy)
        fetchcommand = CURL_FETCHCOMMAND if proxy.over_socks else FETCHCOMMAND
        resumecommand = CURL_RESUMECOMMAND if proxy.over_socks else RESUMECOMMAND
        # `http_proxy` names an HTTP proxy and nothing else. `getuto` writes
        # `honor-http-proxy` into its own dirmngr.conf, so a SOCKS URL there
        # made the tree's signature check answer `keyserver refresh failed:
        # Invalid URI`. dirmngr cannot use SOCKS at all; `all_proxy` is the
        # one curl reads and dirmngr ignores.
        named = ("all_proxy",) if proxy.over_socks else (
            "http_proxy", "https_proxy", "ftp_proxy", "all_proxy"
        )
        settings += [(name, endpoint) for name in named]
        settings += [
            ("no_proxy", ",".join(proxy.bypass)),
            ("FETCHCOMMAND", fetchcommand),
            ("RESUMECOMMAND", resumecommand),
        ]
        if proxy.over_http:
            settings.append(("RSYNC_PROXY", _rsync_proxy(proxy)))
    features = _features(config)
    if features:
        settings.append(("FEATURES", " ".join(features)))
    return tuple(settings)


def _features(config: InstallConfig) -> tuple[str, ...]:
    """One `FEATURES` value, because two would leave two lines and bash keeps
    the last.

    `userfetch` is Portage's own default and it drops the fetch to the
    `portage` user, which cannot read the `0600` files holding the proxy's
    password: curl answered `cannot read config from` for every distfile.
    """
    wanted: list[str] = []
    if _uses_binhost(config.portage):
        wanted.append("getbinpkg")
    if config.proxy.enabled and (config.proxy.over_socks or config.proxy.username):
        wanted.append("-userfetch")
    return tuple(wanted)


def _proxy_endpoint(proxy: ProxyConfig) -> str:
    """The proxy URL without user information, for process environments."""
    return proxy.redacted_url


def _rsync_proxy(proxy: ProxyConfig) -> str:
    """The host:port form rsync reads from `RSYNC_PROXY`."""
    host = proxy.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{proxy.port or 8080}"


def _proxy_toml(proxy: ProxyConfig) -> str:
    """The credential-bearing bootstrap file read only by the installer."""
    return (
        f"kind = {json.dumps(proxy.kind.value)}\n"
        f"host = {json.dumps(proxy.host)}\n"
        f"port = {proxy.port}\n"
        f"username = {json.dumps(proxy.username)}\n"
        f"password = {json.dumps(proxy.password)}\n"
        f"bypass = {json.dumps(list(proxy.bypass))}\n"
    )


def _uses_binhost(portage: PortageConfig) -> bool:
    return portage.binhost.official or portage.binhost.community is not BinhostChannel.OFF


def _distfiles(portage: PortageConfig) -> tuple[str, ...]:
    if not portage.mirrors.gentoo_distfiles:
        return ()
    if portage.mirrors.distfiles:
        return portage.mirrors.distfiles
    return mirrors.gentoo_distfiles(portage.mirrors.region, portage.mirrors.site)


def _appended_distfiles(portage: PortageConfig) -> tuple[str, ...]:
    """gentoo-zh's own distfiles, when they were asked for. They hold the
    sources of that overlay's packages and no main mirror carries them."""
    if not portage.mirrors.gentoo_zh_distfiles:
        return ()
    return mirrors.gentoozh_distfiles(portage.mirrors.gentoo_zh)


def _repo_sync_uri(portage: PortageConfig) -> str:
    if portage.mirrors.repo_sync_uri:
        return portage.mirrors.repo_sync_uri
    if portage.sync is Sync.RSYNC:
        return mirrors.gentoo_rsync_uri(portage.mirrors.region, portage.mirrors.site)
    return mirrors.gentoo_sync_uri(
        portage.mirrors.region, portage.mirrors.site
    )


def community_binhost(portage: PortageConfig) -> str:
    """Where the gentoo-zh binary packages come from.

    Global `~amd64` forces the unstable path whatever the channel row says: the
    stable set is built against the main tree's `amd64`, and Portage refuses a
    binary package whose dependencies do not match the keywords in force.
    """
    return mirrors.gentoozh_binhost(
        portage.mirrors.gentoo_zh,
        unstable=portage.binhost.community is BinhostChannel.UNSTABLE
        or portage.keywords is Keywords.TESTING,
    )


def _l10n(config: InstallConfig) -> tuple[str, ...]:
    """`L10N` uses a hyphen and no encoding, so `zh_CN.UTF-8` becomes `zh-CN`."""
    if config.portage.l10n:
        return config.portage.l10n
    tags: list[str] = []
    for locale in config.system.locales:
        tag = locale.split(".", 1)[0].replace("_", "-")
        if tag not in tags:
            tags.append(tag)
    return tuple(tags)


#: The stage3 Gentoo publishes for each profile target, by the profile path
#: segment that names it. Order matters: `no-multilib` is checked before the
#: plain base, and `desktop` covers `desktop/plasma` and `desktop/gnome`, for
#: which Gentoo publishes no stage3 of their own.
STAGE3_BY_PROFILE: tuple[tuple[str, str], ...] = (
    ("/no-multilib", "nomultilib"),
    ("/desktop", "desktop"),
)


def variant_of(config: InstallConfig) -> str:
    """The stage3 that matches the chosen profile and init system.

    `eselect profile set` is all the installer runs, and a profile switch
    removes nothing: a no-multilib profile on a multilib stage3 keeps every
    32-bit ABI and package the tarball came with, which is not the complete
    64-bit environment the option offers. Gentoo publishes a tarball built
    for each of these targets, so the right one is fetched instead.
    """
    init = "systemd" if config.system.init is InitSystem.SYSTEMD else "openrc"
    profile = config.portage.profile
    for segment, target in STAGE3_BY_PROFILE:
        if segment in profile:
            return f"{target}-{init}"
    return init
