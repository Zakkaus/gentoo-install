"""stage3, the chroot, and everything Portage needs before the first emerge.

The order here is not a preference. `getuto` has to build `/etc/portage/gnupg`
before a key can be imported, an imported key stays untrusted until `lsign`, and
`package.use/zz-autounmask` has to exist before the emerge that writes into it.
"""

from __future__ import annotations

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
                # A stage3 takes minutes to unpack and tar says nothing while
                # it does, so a watchdog reading the console ends the guest.
                f"--checkpoint={UNPACK_CHECKPOINT}",
                "--checkpoint-action=echo",
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

    @property
    def survives_a_reboot(self) -> bool:
        # The failure cleanup unmounts the target recursively, this included,
        # so a resumed run that skipped it ran every chroot command against
        # the live medium's own /proc, /sys and /dev.
        return False

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
    rewriting, so the file is edited rather than replaced: a key named here is
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


#: Where gemato refreshes the release key from, tried in this order. Gentoo's
#: own server is first because it is what Portage ships; a guest that cannot
#: reach it falls through rather than stopping at `No keyserver available`.
#: `keys.openpgp.org` is not here: it serves a key stripped of its user IDs
#: unless the address behind them was confirmed, gemato reads that as a failed
#: refresh, and the snapshot is then refused as unsigned.
KEY_SERVERS: Final[tuple[str, ...]] = (
    "hkps://keys.gentoo.org",
    "hkps://keyserver.ubuntu.com",
)


@dataclass(frozen=True, kw_only=True)
class WebrsyncRepository(Operation):
    """The first sync cannot be a git sync: a stage3 has no `dev-vcs/git`, and
    nothing can be merged until a tree exists. `emerge-webrsync` needs neither."""

    stage: Stage = Stage.PORTAGE

    def describe(self) -> str:
        return "fetch the first ebuild repository snapshot with emerge-webrsync"

    def apply(self, context: Context) -> None:
        # `emerge-webrsync` verifies the snapshot with gemato, which refreshes
        # the release key first, and a refresh that fails fails the sync. One
        # keyserver is one way for the whole install to stop, so each is tried
        # and only the last one's failure is raised.
        last: CommandFailed | None = None
        for server in KEY_SERVERS:
            try:
                context.run_in_target(
                    ["env", f"PORTAGE_GPG_KEY_SERVER={server}", "emerge-webrsync"]
                )
                return
            except CommandFailed as failed:
                last = failed
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
        self._sync(context)
        context.run_in_target(["chown", "--recursive", "portage:portage", str(self.location)])

    def _sync(self, context: Context) -> None:
        """`emerge --sync`, retried on a mirror that is mid-update.

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
        context.write(
            PurePosixPath(f"/etc/portage/package.accept_keywords/{self.repository}"),
            f"*/*::{self.repository} ~amd64\n",
        )


@dataclass(frozen=True, kw_only=True)
class Emerge(Operation):
    stage: Stage = Stage.PACKAGES
    packages: tuple[str, ...]
    summary: str
    requester: str = ""
    repository_bootstrap: bool = False
    oneshot: bool = False
    source: SourcePolicy = SourcePolicy.binaries_allowed()
    #: Install only what is absent. For a package an earlier operation already
    #: pulled in, a plain atom is `[ebuild R]` and portage rebuilds it: one
    #: run spent 132 seconds rebuilding `sys-apps/systemd` at the bootloader
    #: stage with the same flags it had been built with at the kernel stage.
    noreplace: bool = False

    def __post_init__(self) -> None:
        self.source.built_from(self.packages)
        if self.oneshot and self.noreplace:
            raise ValueError("oneshot and noreplace cannot be combined")

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
        argv = ["emerge", *EMERGE_OPTIONS]
        if self.oneshot:
            argv.append("--oneshot")
        if self.noreplace:
            argv.append("--noreplace")
        if context.degraded(BINARY_PACKAGES):
            # `FEATURES=getbinpkg` in make.conf keeps fetching remote binaries
            # under `--usepkg=n`, so both are needed to reach the source path
            # a degraded binhost has to fall back to.
            argv += ["--usepkg=n", "--getbinpkg=n"]
        elif built := self._built_here():
            # These packages only. Turning binaries off wholesale builds the
            # whole dependency tree here too: `sys-apps/systemd` pulled in
            # gtk+, cups and 21 more, and died on a circular dependency.
            # `--usepkg-exclude` also blocks the remote copy under `-g`.
            argv += [
                *BINPKG_OPTIONS[:-1],
                f"{BINPKG_EXCLUDED} {' '.join(_unversioned(one) for one in built)}",
            ]
        else:
            argv += BINPKG_OPTIONS
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
        output = context.run_in_target(
            ["emerge", "--pretend", "--quiet", "--", *atoms], check=False
        )
        if not isinstance(output, CommandOutput):
            raise ConfigError("emerge --pretend returned no exit status")
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
        InstallStage3(mirror=mirror, variant=variant_of(config)),
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
    ]
    if _uses_binhost(portage):
        # Before the first emerge, not after it. `make.conf` above already
        # carries `FEATURES=getbinpkg`, so `emerge dev-vcs/git` fetches binary
        # packages; without the keyring a profile with
        # `binpkg-request-signature` refuses the merge, and without it a
        # package is installed unverified three operations before the trust
        # setup that exists to prevent exactly that.
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
            SyncRepository(name=overlay.name, location=location),
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
