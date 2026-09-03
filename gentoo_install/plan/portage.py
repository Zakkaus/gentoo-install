# SPDX-License-Identifier: GPL-2.0-or-later
"""stage3, the chroot, and everything Portage needs before the first emerge.

The order here is not a preference. `getuto` has to build `/etc/portage/gnupg`
before a key can be imported, an imported key stays untrusted until `lsign`, and
`package.use/zz-autounmask` has to exist before the emerge that writes into it.
"""

from __future__ import annotations

import json
import random
import re
import signal
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Final, Iterator, Sequence

from ..errors import CommandFailed, ConfigError
from ..model import mirrors
from ..model.architecture import DEFAULT_ARCHITECTURE
from ..model.config import (
    BinhostChannel,
    InitSystem,
    InstallConfig,
    Keywords,
    PortageConfig,
    ProxyConfig,
    Sync,
)
from ..model.validate import parse_profile_list, validate_profile
from .operations import CommandOutput, Context, Operation, Stage, answered, worth_reading

#: The release engineering key, pinned. A fingerprint that does not match this
#: is a failed install, not a prompt to trust something new.
RELENG_FINGERPRINT: Final[str] = "13EBBDBEDE7A12775DFDB1BABB572E0E2D182910"

#: gentoo-zh signs its binary packages with this key.
GENTOOZH_FINGERPRINT: Final[str] = "6A0726AF1476A2F382C6AC6638A0234EC16AD42E"

#: `sec-keys/openpgp-keys-gentoozh` installs the key here.
GENTOOZH_KEY: Final[PurePosixPath] = PurePosixPath("/usr/share/openpgp-keys/gentoozh.asc")

#: A stage3 ships the release engineering key at this path.
RELEASE_KEY: Final[PurePosixPath] = PurePosixPath("/usr/share/openpgp-keys/gentoo-release.asc")

#: `getuto` records its local signing key here after creating the keyring.
PORTAGE_TRUST_KEY_ID: Final[PurePosixPath] = PurePosixPath("/etc/portage/gnupg/mykeyid")


def _repos_path(name: str) -> PurePosixPath:
    """Where one ebuild repository's `repos.conf` stanza goes."""
    return PurePosixPath("/etc/portage/repos.conf") / f"{name}.conf"


def _binrepos_path(name: str) -> PurePosixPath:
    filename = "gentoo.conf" if name == "gentoo" else f"{name}.conf"
    return PurePosixPath("/etc/portage/binrepos.conf") / filename


def _inherited_binrepos_paths(name: str) -> tuple[PurePosixPath, ...]:
    if name != "gentoo":
        return (_binrepos_path(name),)
    return (
        _binrepos_path(name),
        PurePosixPath("/etc/portage/binrepos.conf/gentoobinhost.conf"),
    )

#: Portage writes automatic keyword, USE and licence decisions into these. They
#: have to exist before the first emerge, or the writes land outside Portage.
AUTOUNMASK_FILES: Final[tuple[str, ...]] = (
    "/etc/portage/package.use/zz-autounmask",
    "/etc/portage/package.accept_keywords/zz-autounmask",
    "/etc/portage/package.license/zz-autounmask",
)

#: Where Gentoo publishes the keys gemato refreshes.
KEY_SERVER: Final[str] = "hkps://keys.gentoo.org"

#: Tried in order when the keyring refresh is refused. gemato is given the
#: release key by path (`-K /usr/share/openpgp-keys/gentoo-release.asc`, in
#: `emerge-webrsync`), so a keyserver only ever supplies updates to a key whose
#: fingerprint is already pinned: a second host is a mirror of the same key,
#: not a weaker check. Measured 2026-08-17: the cluster's guests got
#: `KEYSERVER_TCP refused` for 140.211.166.190 while this workstation received
#: the key from it, and `keyserver.ubuntu.com` answered for the same
#: fingerprint. Asking one host four times cannot leave a window that host is
#: refusing for.
KEY_SERVERS: Final[tuple[str, ...]] = (KEY_SERVER, "hkps://keyserver.ubuntu.com")

PROXY_BOOTSTRAP: Final[PurePosixPath] = PurePosixPath(
    "/etc/gentoo-install/proxy.toml"
)
CURL_PROXY_CONFIG: Final[PurePosixPath] = PurePosixPath(
    "/etc/gentoo-install/curl-proxy.conf"
)

#: `--retry-on-host-error` with the tries: without it `-t 3` resolves the
#: name once and gives up, which wget calls a fatal error and `vm-desktop`
#: met six times in one burst — `Resolving mirrors.nju.edu.cn... failed:
#: Temporary failure in name resolution.` and six binary packages lost after
#: fifty-six minutes of installing. Measured on wget 1.25.0: one `Resolving`
#: line without the flag, three with it. The curl commands below need
#: nothing: curl retries a resolution failure under plain `--retry`.
FETCHCOMMAND: Final[str] = (
    'wget -t 3 -T 60 --retry-on-host-error --passive-ftp -U "Portage (Gentoo, '
    'https://www.gentoo.org) distfile-fetch" -O "${DISTDIR}/${FILE}" "${URI}"'
)
RESUMECOMMAND: Final[str] = (
    'wget -c -t 3 -T 60 --retry-on-host-error --passive-ftp -U "Portage (Gentoo, '
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

#: The options that decide what emerge accepts, as against what it prints.
#: `VerifyPackages` asks whether the merge will succeed, so it has to carry
#: these: without them the check refused a package set the merge installs by
#: writing the USE change, and an install stopped at operation 26 of 60.
#: What the check adds so emerge computes the USE changes and prints them.
#: Not the whole set the merge carries: `--autounmask-write` never writes
#: under `--pretend` -- portage's `depgraph.py` reads
#: `write_to_file = autounmask_write and not pretend` -- so passing it and
#: `--autounmask-continue` to a pretend changes what is reported and nothing
#: else.
AUTOUNMASK: Final[tuple[str, ...]] = ("--autounmask-use=y",)

#: How emerge announces the one change the merge applies on its own. Only USE:
#: `--autounmask-license` and `--autounmask=y` are deliberately absent from
#: `EMERGE_OPTIONS`, so a keyword or licence change is a refusal the merge
#: would also make.
USE_CHANGES_NEEDED: Final[str] = "The following USE changes are necessary to proceed"


def merge_would_apply(output: str) -> bool:
    """Whether the merge accepts what this pretend refused.

    The merge runs with `--autounmask-write=y --autounmask-continue=y` and no
    `--pretend`, so it writes the USE change and carries on. A check that
    reported this as a refusal was stricter than the thing it guards, and an
    install stopped at operation 26 of 55 for one `wpa_supplicant dbus`.
    """
    return USE_CHANGES_NEEDED in output

#: How emerge says why it will not proceed, in the order the message is
#: looked for. Read off a real refusal rather than guessed: the first
#: non-empty line was `setlocale: unsupported locale setting`, which a chroot
#: whose locales are not generated yet always prints, and the three lines
#: beginning `!!!` were mirror download warnings from ten minutes earlier.
REFUSALS: Final[tuple[str, ...]] = (
    "Error fetching binhost package info",
    "necessary to proceed",
    "there are no ebuilds",
    "All ebuilds that could satisfy",
    "has unmet requirements",
    "Multiple package instances",
)


def why_emerge_refused(output: str) -> str:
    """The line that explains a refusal, not the first line printed."""
    lines = [one.strip() for one in output.splitlines() if one.strip()]
    for line in lines:
        if any(one in line for one in REFUSALS):
            return line.removeprefix("!!! ").strip()
    return lines[-1].removeprefix("!!! ").strip() if lines else "no output"


#: What a failed keyring degrades: every host at once, since none of them can
#: be verified without one.
BINARY_PACKAGES: Final[str] = "binary packages"



def _has_local_signature(signatures: str, signers: frozenset[str]) -> bool:
    """Whether `gpg --with-colons --list-sigs` names a local signing key."""
    for line in signatures.splitlines():
        fields = line.split(":")
        if len(fields) <= 12 or fields[0] != "sig":
            continue
        # The signature class ends in `l` for a local signature; field 13 is
        # the signing primary key's fingerprint.
        if fields[10].endswith("l") and fields[12].upper() in signers:
            return True
    return False

def binhost_trust(name: str) -> str:
    """What one host's own key degrades. The official host's key comes from
    `getuto`, so a community key that failed must not switch it off too."""
    return f"binary packages from {name}"


_BINHOST_NAMES: Final[tuple[str, ...]] = ("gentoo", "gentoo-zh")


def _known_binhosts(context: Context, hosts: tuple[str, ...]) -> tuple[str, ...]:
    """The plan's hosts, or the stanzas later operations actually received."""
    if hosts:
        return hosts
    return tuple(name for name in _BINHOST_NAMES if context.read(_binrepos_path(name)))


def _binary_host_available(
    context: Context, *, binary_host: bool, hosts: tuple[str, ...]
) -> bool:
    """Whether a configured binary host remains usable."""
    if not binary_host or context.degraded(BINARY_PACKAGES):
        return False
    if hosts:
        return any(not context.degraded(binhost_trust(name)) for name in hosts)
    if not any(context.degraded(binhost_trust(name)) for name in _BINHOST_NAMES):
        return True
    if known := _known_binhosts(context, ()):
        return any(not context.degraded(binhost_trust(name)) for name in known)
    # An operation without host metadata cannot assume an unrecorded peer exists.
    return False

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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        fingerprint = RELENG_FINGERPRINT[-16:]
        # `PROXY_BOOTSTRAP` is named because `apply()` writes it, and it holds
        # the proxy password: a file an operator meets by finding it.
        fallbacks = ", ".join(self.fallbacks)
        if self.proxy.enabled:
            if self.fallbacks:
                return (
                    "download the newest {} stage3 from {} via {}, or {} if it does not answer; verify it against {}, unpack it into the target and write {}",
                    (
                        self.variant,
                        self.mirror,
                        self.proxy.redacted_url,
                        fallbacks,
                        fingerprint,
                        str(PROXY_BOOTSTRAP),
                    ),
                )
            return (
                "download the newest {} stage3 from {} via {}, verify it against {}, unpack it into the target and write {}",
                (
                    self.variant,
                    self.mirror,
                    self.proxy.redacted_url,
                    fingerprint,
                    str(PROXY_BOOTSTRAP),
                ),
            )
        if self.fallbacks:
            return (
                "download the newest {} stage3 from {} directly, or {} if it does not answer; verify it against {}, unpack it into the target and write {}",
                (
                    self.variant,
                    self.mirror,
                    fallbacks,
                    fingerprint,
                    str(PROXY_BOOTSTRAP),
                ),
            )
        return (
            "download the newest {} stage3 from {} directly, verify it against {}, unpack it into the target and write {}",
            (self.variant, self.mirror, fingerprint, str(PROXY_BOOTSTRAP)),
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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        # Not gpg: its proxy is `dirmngr.conf`, which `PrepareBinhostTrust`
        # writes after `getuto` rebuilds that directory. Naming it here
        # described a file this operation never writes.
        if self.proxy.enabled:
            return (
                "configure wget, curl and git for {} in /etc/wgetrc, /etc/gitconfig, curl-proxy.conf and proxy.toml",
                (self.proxy.redacted_url,),
            )
        return "write no proxy configuration, so every client connects directly", ()

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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "mount transient proc, sys, dev and run filesystems into the target", ()

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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "seed the target's resolv.conf from the install medium", ()

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
    #: Whether `MAKEOPTS` is the installing machine's core count, resolved
    #: here because only `exec` may look at the machine. An empty `makeopts`
    #: means "follow this machine", and it was written as no `MAKEOPTS` at
    #: all: a stage3's `make.conf` carries none, `make.globals` carries none
    #: and no profile sets one, so the target built with a single job.
    jobs_from_machine: bool = False

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        keys = ", ".join(key for key, _ in self._named())
        mirrors = str(len(self.mirrors))
        if self.speed_test:
            if self.appended:
                return (
                    "write /etc/portage/make.conf with {}; {} mirrors fastest first, measured, {} appended",
                    (keys, mirrors, str(len(self.appended))),
                )
            return (
                "write /etc/portage/make.conf with {}; {} mirrors fastest first, measured",
                (keys, mirrors),
            )
        if self.appended:
            return (
                "write /etc/portage/make.conf with {}; {} mirrors in the configured order, {} appended",
                (keys, mirrors, str(len(self.appended))),
            )
        return (
            "write /etc/portage/make.conf with {}; {} mirrors in the configured order",
            (keys, mirrors),
        )


    def _named(self) -> tuple[tuple[str, str], ...]:
        """What the file will hold, for the description. `MAKEOPTS` is named
        even though its value is not known until apply."""
        if not self.jobs_from_machine:
            return self.settings
        return (*self.settings, ("MAKEOPTS", ""))

    def apply(self, context: Context) -> None:
        ranked = context.rank_mirrors(self.mirrors) if self.speed_test else self.mirrors
        wanted = list(self.settings)
        if self.jobs_from_machine:
            wanted.append(("MAKEOPTS", f"-j{context.jobs()}"))
        listed = (*ranked, *self.appended)
        # Not written at all when empty: an empty GENTOO_MIRRORS is a shorter
        # list than Portage's own, not the same thing as leaving it alone.
        if listed:
            wanted.append(("GENTOO_MIRRORS", " ".join(listed)))
        existing = context.read(PurePosixPath("/etc/portage/make.conf"))
        context.write(PurePosixPath("/etc/portage/make.conf"), merge(existing, wanted))


@dataclass(frozen=True, kw_only=True)
class SettleBinhostFeature(Operation):
    """Make `FEATURES` agree with whether a binary host was configured.

    `WriteMakeConf` writes `getbinpkg` because the configuration asks for a
    host, and `ConfigureBinhost` finds out six operations later whether there
    is one this machine can verify -- it returns without writing
    `binrepos.conf` when there is not. Left alone the installed system says it
    fetches binary packages and has nothing naming where from.

    A community key merge can reach here before its host is configured. Its
    host-scoped degradation removes that host only; another host stays usable.
    """

    stage: Stage = Stage.PORTAGE
    hosts: tuple[str, ...] = ()

    def destinations(self) -> tuple[PurePosixPath, ...]:
        return (MAKE_CONF,)

    def describe(self) -> str:
        return (
            "drop getbinpkg from FEATURES in /etc/portage/make.conf when no "
            "binary package host was written"
        )

    def apply(self, context: Context) -> None:
        existing = context.read(MAKE_CONF)
        usable = [
            one
            for one in self.hosts
            if not context.degraded(BINARY_PACKAGES)
            and not context.degraded(binhost_trust(one))
        ]
        wanted = existing if usable else _features_without(existing, "getbinpkg")
        context.write(MAKE_CONF, wanted)


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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "write {}", (str(self.path),)

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

    def destinations(self) -> tuple[PurePosixPath, ...]:
        return tuple(PurePosixPath(one) for one in AUTOUNMASK_FILES)

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "create {}, which emerge writes its decisions into", (
            ", ".join(str(one) for one in self.destinations()),
        )

    def apply(self, context: Context) -> None:
        for path in self.destinations():
            context.write(path, "")


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

    def destinations(self) -> tuple[PurePosixPath, ...]:
        return (_repos_path(self.name),)

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        where = str(_repos_path(self.name))
        if self.verify_commits:
            return "write {} pointing {} at {}, commit signatures verified", (
                where,
                self.name,
                self.sync_uri,
            )
        return "write {} pointing {} at {}", (where, self.name, self.sync_uri)

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
        (path,) = self.destinations()
        context.write(path, "\n".join(stanza) + "\n")


@dataclass(frozen=True, kw_only=True)
class ConfigureWebrsyncRepository(Operation):
    """Persist webrsync without a URI, which Portage's module does not consume."""

    stage: Stage = Stage.PORTAGE
    name: str
    location: PurePosixPath

    def destinations(self) -> tuple[PurePosixPath, ...]:
        return (_repos_path(self.name),)

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "write {} so repository {} syncs with emerge-webrsync", (
            str(_repos_path(self.name)),
            self.name,
        )

    def apply(self, context: Context) -> None:
        stanza = (
            f"[{self.name}]\n"
            f"location = {self.location}\n"
            "sync-type = webrsync\n"
            "auto-sync = yes\n"
            "sync-webrsync-verify-signature = true\n"
            f"sync-openpgp-key-path = {RELEASE_KEY}\n"
        )
        (path,) = self.destinations()
        context.write(path, stanza)


@dataclass(frozen=True, kw_only=True)
class WebrsyncRepository(Operation):
    """The first sync cannot be a git sync: a stage3 has no `dev-vcs/git`, and
    nothing can be merged until a tree exists. `emerge-webrsync` needs neither."""

    stage: Stage = Stage.PORTAGE

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "fetch the first ebuild repository snapshot with emerge-webrsync", ()

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
        servers = _key_servers(server)
        last: CommandFailed | None = None
        refused = 0
        for attempt in range(KEYRING_TRIES):
            try:
                context.run_in_target(
                    [
                        "env",
                        f"PORTAGE_GPG_KEY_SERVER={servers[refused % len(servers)]}",
                        "emerge-webrsync",
                    ]
                )
                return
            except CommandFailed as failed:
                last = failed
                if attempt + 1 >= KEYRING_TRIES:
                    break
                # A keyserver the whole round is asking at once needs longer
                # than a mirror rewriting a Manifest, and the two failures are
                # told apart before the wait rather than after it.
                keyring = _keyring_refused(str(failed))
                if keyring:
                    refused += 1
                pause = (KEYRING_PAUSE if keyring else SYNC_PAUSE) * (attempt + 1)
                if keyring:
                    pause *= random.uniform(*KEYRING_JITTER)
                context.run(["sleep", f"{pause:g}"])
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

#: What a keyserver under load answers. gemato refreshes the signing key to
#: check revocations, and ten cluster guests asking one host inside three
#: minutes got nine of these: run62 lost every fixture but `vm-f2fs`, which
#: reached the same step at 49.6 minutes and passed.
KEYRING_REFUSED: Final[tuple[str, ...]] = (
    "keyserver refresh failed",
    "openpgp keyring refresh failed",
    "no keyserver available",
)

#: Tries and the first pause for that case only. Geometric, so four attempts
#: span about twelve minutes rather than the ninety seconds a Manifest
#: mismatch needs.
KEYRING_TRIES: Final[int] = 4
KEYRING_PAUSE: Final[float] = 90.0

#: What the geometric pause is multiplied by, so a wave of machines that
#: reached this step together does not ask again together. Measured in run64:
#: eight guests dispatched in one wave all died 14 to 24 minutes in, while the
#: five that reached the same step 44 to 107 minutes in all passed, and both
#: keys answer from `keyserver.ubuntu.com` when one machine asks alone. Without
#: this the whole wave waits the same 90, 180 and 270 seconds and asks again in
#: lockstep, so four attempts are one attempt made four times. It only ever
#: extends: a factor below one would undercut the twelve minutes the geometric
#: pause exists to buy, and outlasting the round is the whole point.
KEYRING_JITTER: Final[tuple[float, float]] = (1.0, 2.5)


def _key_servers(first: str) -> tuple[str, ...]:
    """The rotation, with whatever the stage3's own policy named at the front.

    An operator or a stage3 that names a server has chosen it, so it is tried
    before the fallbacks and is not repeated later in the rotation.
    """
    return (first, *(one for one in KEY_SERVERS if one != first))


def _keyring_refused(output: str) -> bool:
    """Whether the failure was the keyring refresh rather than the snapshot."""
    lowered = output.lower()
    return any(marker in lowered for marker in KEYRING_REFUSED)


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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        if not self.alternates:
            return "sync repository {}", (self.name,)
        return "sync repository {}, {} other sites to fall back on", (
            self.name,
            str(len(self.alternates)),
        )

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
        # How many were tried, not only the last one's words: `vm-cjk-kernel`
        # reported one host refusing a connection where five had, which reads
        # as one unlucky mirror rather than a guest with no route to any of
        # them.
        raise CommandFailed(
            f"none of the {1 + len(self.alternates)} sites for {self.name} "
            f"could be synced from: {last}"
        ) from last

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
                    # What the failed attempt left, before sleeping on it. A
                    # clone cut off mid-transfer leaves a directory with no
                    # `metadata/layout.conf`, and the next two attempts then
                    # fail on `Repository 'gentoo-zh' is missing masters
                    # attribute` rather than on the network: `zbm-unlock`
                    # stopped at 46.8m with `curl 56 … unexpected eof` and
                    # that message, one after the other, on the same line.
                    context.run_in_target(["rm", "--recursive", "--force", str(self.location)])
                    context.run(["sleep", f"{SYNC_PAUSE * (attempt + 1):g}"])
        assert last is not None
        raise last


@dataclass(frozen=True, kw_only=True)
class SelectProfile(Operation):
    stage: Stage = Stage.PORTAGE
    profile: str

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "select profile {}", (self.profile,)

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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return (
            "accept {} for packages from {} only",
            (DEFAULT_ARCHITECTURE.testing_keyword, self.repository),
        )

    def apply(self, context: Context) -> None:
        WritePortageConfig(
            kind=PortageConfigKind.KEYWORDS,
            name=self.repository,
            lines=(
                f"*/*::{self.repository} {DEFAULT_ARCHITECTURE.testing_keyword}",
            ),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class EnableRepository(Operation):
    """Turn on an overlay the operator named, by name alone.

    `eselect repository` reads the current `repositories.xml` for the address,
    so the list is whatever the project publishes today rather than whatever
    this repository last copied. The exit status is checked because the tool
    prints one line and exits 0 for a name it does not know, and the install
    would carry on without the overlay.
    """

    stage: Stage = Stage.PORTAGE
    repository: str

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "enable the {} repository", (self.repository,)

    def apply(self, context: Context) -> None:
        said = context.run_in_target(
            ["eselect", "repository", "enable", self.repository], check=False
        )
        failed = isinstance(said, CommandOutput) and said.returncode != 0
        # The exit status is not enough: an unknown name prints one line and
        # exits 0, and the install would carry on without the overlay.
        if failed or "not found" in str(said):
            raise CommandFailed(
                f"{self.repository} was not enabled: {str(said).strip()[:200]}"
            )


@dataclass(frozen=True, kw_only=True)
class Emerge(Operation):
    stage: Stage = Stage.PACKAGES
    packages: tuple[str, ...]
    summary: str
    requester: str = ""
    repository_bootstrap: bool = False
    mode: InstallMode = InstallMode.NORMAL
    source: SourcePolicy = SourcePolicy.binaries_allowed()
    #: Whether this run has a binary host at all, which decides `--getbinpkg`.
    #: The stage3 ships an enabled `binrepos.conf` for the official one and
    #: `ext2` once compiled 48 packages while fetching 20 from a host it had
    #: not asked for; `build()` now schedules `DisableBinhost(name="gentoo")`
    #: before any emerge, so that host is gone rather than merely unasked for.
    binary_host: bool = True
    #: A host-scoped failure falls back to source only when no configured peer remains.
    hosts: tuple[str, ...] = ()
    #: What to degrade rather than fail, when this emerge is what enables an
    #: optional path. Empty means the install stops, which is right for
    #: everything the machine needs to boot.
    degrades: str = ""

    def __post_init__(self) -> None:
        self.source.built_from(self.packages)

    @property
    def package_requester(self) -> str:
        return self.requester or f"the `{self.summary}` operation"

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        packages = " ".join(self.packages)
        built = self._built_here()
        if built == self.packages:
            return "{}: emerge {}, from source", (self.summary, packages)
        # `binary_host` as well as `built`: they are different facts, and only
        # the second one was drawn, so a plan that stopped carrying a disabled
        # binhost read exactly like one that still had it.
        if built:
            if not self.binary_host:
                return "{}: emerge {}, building {} here, with no binhost", (
                    self.summary,
                    packages,
                    " ".join(built),
                )
            return "{}: emerge {}, building {} here", (
                self.summary,
                packages,
                " ".join(built),
            )
        if not self.binary_host:
            return "{}: emerge {}, with no binhost", (self.summary, packages)
        return "{}: emerge {}", (self.summary, packages)

    def _built_here(self) -> tuple[str, ...]:
        return self.source.built_from(self.packages)

    def apply(self, context: Context) -> None:
        command = self._argv(
            context,
            source_only=not _binary_host_available(
                context, binary_host=self.binary_host, hosts=self.hosts
            ),
        )
        try:
            result = context.run_in_target(command, check=False)
        except CommandFailed as error:
            marker = _binpkg_failure(str(error))
            if marker is None or context.degraded(BINARY_PACKAGES):
                if self._optional(context, str(error)):
                    return
                raise
            if (again := self._one_more_binary_try(context, command)) is not None:
                marker = again
            else:
                return
            if self._one_failing_binary(context, marker):
                return
            context.degrade(BINARY_PACKAGES, f"selected binary package failed: {marker}")
            self._from_source(context)
            return
        if not isinstance(result, CommandOutput) or result.returncode == 0:
            self._note_an_unreadable_index(context, result)
            return
        if (killed := _binhost_killed_emerge(result)) and not context.degraded(
            BINARY_PACKAGES
        ):
            second = context.run_in_target(command, check=False)
            if not isinstance(second, CommandOutput) or second.returncode == 0:
                self._note_an_unreadable_index(context, second)
                return
            context.degrade(BINARY_PACKAGES, killed)
            self._from_source(context)
            return
        if not _binpkg_failure(str(result)) or context.degraded(BINARY_PACKAGES):
            if self._optional(context, str(result)):
                return
            raise _emerge_failed("emerge", result)
        marker = _binpkg_failure(str(result))
        if (again := self._one_more_binary_try(context, command)) is not None:
            marker = again
        else:
            return
        if self._one_failing_binary(context, marker):
            return
        context.degrade(BINARY_PACKAGES, f"selected binary package failed: {marker}")
        self._from_source(context)

    def _optional(self, context: Context, said: str) -> bool:
        """Whether this failure degrades a path instead of ending the install.

        `sec-keys/openpgp-keys-gentoozh` is the whole of it today: its distfile
        is not on the Gentoo mirrors, so a machine whose only route to
        `distfiles.gentoozh.org` is down took the install down with it, exit 4
        with the disk already partitioned and the stage3 unpacked.
        """
        if not self.degrades:
            return False
        context.degrade(self.degrades, f"{' '.join(self.packages)} did not merge: {said.strip()[:200]}")
        return True

    def _without_one_binary(self, context: Context, package: str) -> bool:
        """Try again with that one package excluded from the binary host.

        Between the whole-group retry and giving the group up: `vm-gnome` lost
        eight hours compiling 227 packages because one `.gpkg.tar` download
        answered `Unable to establish SSL connection.` twice.
        """
        argv = self._argv(context, source_only=False)
        for index, item in enumerate(argv):
            if item.startswith(BINPKG_EXCLUDED):
                argv[index] = f"{item} {package}"
                break
        else:
            return False
        answer = context.run_in_target(argv, check=False)
        return isinstance(answer, CommandOutput) and answer.returncode == 0

    def _one_failing_binary(self, context: Context, marker: str) -> bool:
        """Whether excluding the package this marker names finished the merge."""
        package = _failed_package(marker)
        if not package or not self._without_one_binary(context, package):
            return False
        # The package, not `BINARY_PACKAGES`: giving up the path would send
        # every later merge to source as well, which is the eight hours this
        # step exists to avoid. Nothing reads a package name back.
        context.degrade(package, f"its binary package failed, so it is compiled: {marker}")
        return True

    def _from_source(self, context: Context) -> None:
        retry_result = context.run_in_target(self._argv(context, source_only=True), check=False)
        if isinstance(retry_result, CommandOutput) and retry_result.returncode != 0:
            raise _emerge_failed("source retry", retry_result)

    def _note_an_unreadable_index(self, context: Context, result: object) -> None:
        """A host that answers no index does not fail the emerge: Portage says
        so, compiles everything and exits 0. Recorded here or nowhere, and
        recorded once for that host, because it says so on every emerge.

        Every reader of a zero exit, not the first one: a retry that succeeded
        left the run saying it fetches binary packages from a host whose index
        it had failed to read.
        """
        if context.degraded(BINARY_PACKAGES):
            return
        for unreadable in _unreadable_indexes(str(result)):
            trust = binhost_trust(unreadable.host)
            if not context.degraded(trust):
                context.degrade(trust, unreadable.reason)

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
            self._note_an_unreadable_index(context, again)
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


#: What Portage prints when it cannot read a binary host's index, taken from
#: what stopped `vm-gnome`: `!!! [gentoo] Error fetching binhost package info
#: from 'https://mirrors.nju.edu.cn/gentoo/releases/amd64/binpackages/23.0/
#: x86-64'`, followed by `!!! [gentoo] <urlopen error timed out>`.
BINHOST_INDEX_FAILURE: Final[str] = "Error fetching binhost package info"


#: The same line, with the host and the url it names. Built from the marker
#: rather than spelling it a second time: the two detectors read one Portage
#: diagnostic, and a reworded one would have left this half of them silent.
#: Portage compiles everything and exits 0 after printing it, so nothing else
#: in this file sees it: a run against a host whose index answered 404
#: compiled all 69 of its packages in 84 minutes and recorded no reason.
#: Portage prints its own diagnosis on the line after the one above, as
#: `!!! [gentoo] <urlopen error [Errno -3] Temporary failure in name
#: resolution>`. Captured, because without it the recorded reason says the
#: host has no index when the guest could not resolve its name at all.
#: What the kernel and the compiler say when a build is killed for memory.
#: `vm-gnome` died compiling `net-libs/webkit-gtk` with four jobs in eight
#: gibibytes and the installer reported `emerge ended with exit 1`, leaving
#: the operator to find `Out of memory: Killed ... task=cc1plus` in the log.
#: Both spellings, because the kernel's line is not always in the emerge
#: output the installer captured while gcc's always is.
_OUT_OF_MEMORY: Final[re.Pattern[str]] = re.compile(
    r"Out of memory: Killed|fatal error: Killed|virtual memory exhausted|"
    r"cc1plus: out of memory|g\+\+: fatal error: Killed"
)


def _emerge_failed(what: str, result: CommandOutput) -> CommandFailed:
    """The failure a merge deserves, with the one cause an operator can act on.

    `g++: fatal error: Killed` is the machine running out of memory rather than
    a broken package, and the source retry raised the generic text for it.
    """
    if _ran_out_of_memory(str(result)):
        return CommandFailed(
            "the compiler was killed because the machine ran out of memory, "
            "not because the package is broken: lower the job count in "
            "MAKEOPTS or give the machine swap, then resume. "
            f"{what} ended with {result.ending}: {worth_reading(str(result))}"
        )
    return CommandFailed(f"{what} ended with {result.ending}: {worth_reading(str(result))}")


def _ran_out_of_memory(said: str) -> bool:
    """Whether this emerge failed because the machine had no memory left."""
    return _OUT_OF_MEMORY.search(said) is not None


_INDEX_UNREADABLE: Final[re.Pattern[str]] = re.compile(
    r"!!! \[(?P<host>[^\]]+)\] "
    + re.escape(BINHOST_INDEX_FAILURE)
    + r" from '(?P<url>[^']+)'"
    + r"(?:\s*\n\s*!!! \[(?P=host)\] (?P<detail>[^\n]+))?"
)


@dataclass(frozen=True)
class _UnreadableBinhostIndex:
    host: str
    reason: str


def _names_an_unreadable_index(output: str) -> str:
    """The line proving the failure was the binary host and not a package.

    Looser than `_INDEX_UNREADABLE` on purpose: that pattern reads the host
    out of Portage's usual two-line shape, and a line in any other shape still
    says the index could not be read.
    """
    for raw in output.splitlines():
        line = raw.strip()
        if BINHOST_INDEX_FAILURE in line:
            return line.removeprefix("!!! ")
    return ""


def _unreadable_indexes(said: str) -> Iterator[_UnreadableBinhostIndex]:
    """The hosts and reasons to record when their indexes could not be read."""
    for found in _INDEX_UNREADABLE.finditer(said):
        detail = (found.group("detail") or "").strip()
        host = found.group("host")
        where = f"{host} could not read its package index at {found.group('url')}"
        yield _UnreadableBinhostIndex(
            host=host, reason=f"{where}: {detail}" if detail else where
        )


def _binhost_killed_emerge(result: CommandOutput) -> str | None:
    """Whether emerge died on a signal before printing a single byte.

    Its stdout is a pipe and therefore block-buffered, so a signal loses
    everything it had written, and the only network it touches before its
    first line is the binary host. `vm-xfs` died with `SIGPIPE (13)` and no
    output on the install's first emerge, seventy-seven lines after
    `mirrors.nju.edu.cn` dropped a TLS handshake mid-fetch.
    """
    if result.returncode != -signal.SIGPIPE or str(result).strip():
        return None
    return "the binary host killed emerge with SIGPIPE before it printed anything"


def _failed_package(marker: str) -> str:
    """The package a binary failure names, in the shape `--usepkg-exclude`
    takes, or empty when the marker names none."""
    found = re.search(r">>> Failed to emerge (\S+?)(?:,|$)", marker)
    return _unversioned(found.group(1)) if found else ""


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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        # `dirmngr.conf` only where it is written: `apply` guards it on
        # `over_http`, and every plan without a proxy promised a file the run
        # never produced.
        if self.proxy.over_http:
            return (
                "run getuto so Portage has a keyring to verify binary packages against, and write dirmngr.conf",
                (),
            )
        return (
            "run getuto so Portage has a keyring to verify binary packages against",
            (),
        )

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
    """An imported key stays untrusted until `lsign`, and its local signature
    is the postcondition that permits the host to serve binary packages."""

    stage: Stage = Stage.PORTAGE
    binhost: str
    fingerprint: str
    key_path: PurePosixPath

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "import {} from {}, locally sign it for {}, and verify the local signature", (
            self.fingerprint[-16:],
            str(self.key_path),
            self.binhost,
        )

    def apply(self, context: Context) -> None:
        if context.degraded(BINARY_PACKAGES) or context.degraded(binhost_trust(self.binhost)):
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
                "gpg",
                "--homedir",
                "/etc/portage/gnupg",
                "--batch",
                "--yes",
                "--pinentry-mode",
                "loopback",
                "--passphrase-file",
                "/etc/portage/gnupg/pass",
                "--lsign-key",
                self.fingerprint,
            ]
        )
        context.run_in_target(["gpg", "--homedir", "/etc/portage/gnupg", "--check-trustdb"])
        signers = frozenset(
            fingerprint.strip().upper()
            for fingerprint in context.read(PORTAGE_TRUST_KEY_ID).splitlines()
            if fingerprint.strip()
        )
        if not signers:
            raise CommandFailed("Portage's local signing key is absent")
        signatures = answered(
            context.run_in_target(
                [
                    "gpg",
                    "--homedir",
                    "/etc/portage/gnupg",
                    "--with-colons",
                    "--list-sigs",
                    self.fingerprint,
                ],
                check=False,
            ),
            f"could not read the local signature for {self.fingerprint[-16:]}",
        )
        if not _has_local_signature(signatures, signers):
            raise CommandFailed(
                f"{self.fingerprint[-16:]} has no local signature from Portage's trust key"
            )


@dataclass(frozen=True, kw_only=True)
class DisableBinhost(Operation):
    """Remove a binhost inherited from the stage3."""

    stage: Stage = Stage.PORTAGE
    name: str

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "disable inherited binary package host {}", (self.name,)

    def apply(self, context: Context) -> None:
        for path in _inherited_binrepos_paths(self.name):
            context.run_in_target(["rm", "--force", "--", str(path)])


@dataclass(frozen=True, kw_only=True)
class ConfigureBinhost(Operation):
    stage: Stage = Stage.PORTAGE
    name: str
    sync_uri: str
    verify: bool

    def destinations(self) -> tuple[PurePosixPath, ...]:
        return (_binrepos_path(self.name),)

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        where = str(_binrepos_path(self.name))
        if self.verify:
            return "write {} adding verified binary package host {} at {}", (
                where,
                self.name,
                self.sync_uri,
            )
        return "write {} adding unverified binary package host {} at {}", (
            where,
            self.name,
            self.sync_uri,
        )

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
        context.write(_binrepos_path(self.name), stanza)


@dataclass(frozen=True, kw_only=True)
class PackageRequest:
    atom: str
    requesters: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class VerifyPackages(Operation):
    """Repository bootstrap merges make repositories reachable, so they precede this check."""

    stage: Stage = Stage.PORTAGE
    requests: tuple[PackageRequest, ...]
    #: As `Emerge.binary_host`: the pretend has to resolve against the same
    #: package set the real merge will use, or it accepts what emerge refuses.
    binary_host: bool = True
    #: A host-scoped failure leaves binaries on when this tuple has a usable peer.
    hosts: tuple[str, ...] = ()

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return "resolve {} together before installing the requested packages", (
            " ".join(request.atom for request in self.requests),
        )

    def apply(self, context: Context) -> None:
        atoms = tuple(request.atom for request in self.requests)
        output = self._resolve(
            context,
            atoms,
            source_only=not _binary_host_available(
                context, binary_host=self.binary_host, hosts=self.hosts
            ),
        )
        if output.returncode == 0:
            return
        output = self._without_the_binary_host(context, atoms, output)
        if output.returncode == 0:
            return
        if merge_would_apply(str(output)):
            # The merge writes this one and carries on, so refusing here would
            # stop an install that works.
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

        detail = why_emerge_refused(output)
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
            ["emerge", "--pretend", "--quiet", *AUTOUNMASK, *without, "--", *atoms],
            check=False,
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
        said = str(failed)
        unreadable = next(_unreadable_indexes(said), None)
        # The marker without the host, as the deleted `_binhost_unreadable`
        # matched it: a line Portage prints in another shape still names a
        # host that cannot be read, and failing the install there is worse
        # than degrading the whole feature.
        loose = _names_an_unreadable_index(said)
        if not loose or context.degraded(BINARY_PACKAGES):
            return failed
        # The same retry `Emerge` makes before giving up on binaries: one
        # dropped connection would otherwise compile the whole install.
        again = self._resolve(context, atoms, source_only=False)
        if again.returncode == 0 or not _names_an_unreadable_index(str(again)):
            return again
        if unreadable is None or not _known_binhosts(context, self.hosts):
            context.degrade(BINARY_PACKAGES, f"binary host index unreadable: {loose}")
        else:
            context.degrade(binhost_trust(unreadable.host), unreadable.reason)
        return self._resolve(context, atoms, source_only=True)


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

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return (
            "accept {} for {} and nothing else",
            (DEFAULT_ARCHITECTURE.testing_keyword, " ".join(self.packages)),
        )

    def apply(self, context: Context) -> None:
        WritePortageConfig(
            kind=PortageConfigKind.KEYWORDS,
            name="user",
            lines=tuple(
                f"{atom} {DEFAULT_ARCHITECTURE.testing_keyword}"
                for atom in self.packages
            ),
        ).apply(context)


@dataclass(frozen=True, kw_only=True)
class AcceptTestingGlobally(Operation):
    """Last, never earlier. Opening `~amd64` before the system is installed
    drags the whole install into an unmask chain."""

    stage: Stage = Stage.FINISH

    def describe_parts(self) -> tuple[str, tuple[str, ...]]:
        return (
            'append ACCEPT_KEYWORDS="{}" to make.conf, after everything is installed',
            (DEFAULT_ARCHITECTURE.testing_keyword,),
        )

    def apply(self, context: Context) -> None:
        context.append(
            PurePosixPath("/etc/portage/make.conf"),
            f'ACCEPT_KEYWORDS="{DEFAULT_ARCHITECTURE.testing_keyword}"\n',
        )


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
) -> list[Operation]:
    portage = config.portage
    hosts = _binhost_names(portage)
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
            settings=make_conf(config, use, video_cards),
            mirrors=_distfiles(portage),
            speed_test=portage.mirrors.speed_test,
            appended=_appended_distfiles(portage),
            jobs_from_machine=not portage.makeopts,
        ),
        WriteProxyClients(proxy=config.proxy),
        CreateAutounmaskFiles(),
    ]
    operations.append(DisableBinhost(name="gentoo"))
    if uses_binhost(portage):
        # Before the first emerge, not after it. `make.conf` above already
        # carries `FEATURES=getbinpkg`, so `emerge dev-vcs/git` fetches binary
        # packages; without the keyring a profile with
        # `binpkg-request-signature` refuses the merge, and without it a
        # package is installed unverified three operations before the trust
        # setup that exists to prevent exactly that.
        operations.append(PrepareBinhostTrust(proxy=config.proxy))
    if portage.binhost.official:
        # Written rather than left to the stage3's default: entry removal keeps
        # untrusted hosts source-only; profile baseline and subarch are choices here.
        operations += [
            TrustBinhostKey(
                binhost="gentoo",
                fingerprint=RELENG_FINGERPRINT,
                key_path=RELEASE_KEY,
            ),
            ConfigureBinhost(
                name="gentoo",
                sync_uri=portage.binhost.url
                or mirrors.gentoo_binhost(
                    portage.mirrors.region, portage.mirrors.site, portage.binhost.subarch
                ),
                verify=True,
            ),
        ]
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
                hosts=hosts,
            ),
            ConfigureRepository(
                name="gentoo",
                location=gentoo,
                sync_uri=_repo_sync_uri(portage),
                verify_commits=True,
            ),
            SyncRepository(
                name="gentoo",
                location=gentoo,
                # The rest of the region, the way an overlay already gets it.
                # Without them one unreachable host ended the install on
                # `none of the 1 sites for gentoo could be synced from`, an
                # hour in, with four more mirrors carrying the same tree.
                alternates=tuple(
                    ConfigureRepository(
                        name="gentoo",
                        location=gentoo,
                        sync_uri=uri,
                        verify_commits=True,
                    )
                    for uri in mirrors.gentoo_sync_uris(portage.mirrors.region)
                    if uri != _repo_sync_uri(portage)
                ),
            ),
        ]
    elif portage.sync is Sync.RSYNC:
        # No `dev-vcs/git`: rsync needs none, and the stage3 already has the
        # rsync binary. Signatures are verified per snapshot, not per commit.
        #
        # No sync here either. `emerge-webrsync` has already placed a signed
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
                hosts=hosts,
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
    if portage.repositories:
        operations.append(
            Emerge(
                stage=Stage.PORTAGE,
                packages=("app-eselect/eselect-repository",),
                summary="install eselect-repository, which the named overlays are added with",
                repository_bootstrap=True,
                hosts=hosts,
            )
        )
        operations += [
            EnableRepository(repository=name) for name in portage.repositories
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
                degrades=binhost_trust("gentoo-zh"),
                hosts=hosts,
            ),
            TrustBinhostKey(
                binhost="gentoo-zh",
                fingerprint=GENTOOZH_FINGERPRINT,
                key_path=GENTOOZH_KEY,
            ),
            ConfigureBinhost(name="gentoo-zh", sync_uri=community_binhost(portage), verify=True),
        ]
    if hosts:
        # After every `ConfigureBinhost`, because it is their outcome this
        # reads: `WriteMakeConf` wrote `getbinpkg` before any of them ran.
        operations.append(SettleBinhostFeature(hosts=hosts))
    return operations


def finish(config: InstallConfig) -> list[Operation]:
    if config.portage.keywords is Keywords.TESTING:
        return [AcceptTestingGlobally()]
    return []


def make_conf(
    config: InstallConfig,
    use: tuple[str, ...] = (),
    video_cards: tuple[str, ...] = (),
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
        settings.append(
            (DEFAULT_ARCHITECTURE.cpu_flags_variable, " ".join(portage.cpu_flags))
        )
    wanted_cards = video_cards or portage.video_cards
    if wanted_cards:
        settings.append(("VIDEO_CARDS", " ".join(wanted_cards)))
    if portage.input_devices:
        settings.append(("INPUT_DEVICES", " ".join(portage.input_devices)))
    settings += [
        ("ACCEPT_LICENSE", " ".join(portage.accept_license)),
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
    if uses_binhost(config.portage):
        wanted.append("getbinpkg")
    if config.proxy.enabled and (config.proxy.over_socks or config.proxy.username):
        wanted.append("-userfetch")
    return tuple(wanted)


#: The one file `WriteMakeConf` and `ConfigureBinhost` both touch.
MAKE_CONF: Final[PurePosixPath] = PurePosixPath("/etc/portage/make.conf")

_FEATURES_LINE: Final[re.Pattern[str]] = re.compile(
    r'^(?P<lead>FEATURES=")(?P<value>[^"]*)(?P<tail>")\n', re.MULTILINE
)


def _features_without(text: str, feature: str) -> str:
    """`make.conf` with one feature removed from `FEATURES`, and the whole
    assignment dropped when it was the only one."""

    def rewritten(found: "re.Match[str]") -> str:
        kept = [one for one in found.group("value").split() if one != feature]
        if not kept:
            return ""
        return f"{found.group('lead')}{' '.join(kept)}{found.group('tail')}\n"

    return _FEATURES_LINE.sub(rewritten, text)


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


def _binhost_names(portage: PortageConfig) -> tuple[str, ...]:
    """The names this configuration asks the plan to make usable."""
    if portage.binhost.official:
        return (
            ("gentoo", "gentoo-zh")
            if portage.binhost.community is not BinhostChannel.OFF
            else ("gentoo",)
        )
    return ("gentoo-zh",) if portage.binhost.community is not BinhostChannel.OFF else ()


def uses_binhost(portage: PortageConfig) -> bool:
    """Whether any binary host is configured at all."""
    return bool(_binhost_names(portage))


def _distfiles(portage: PortageConfig) -> tuple[str, ...]:
    if not portage.mirrors.gentoo_distfiles:
        return ()
    if portage.mirrors.distfiles:
        return portage.mirrors.distfiles
    return mirrors.gentoo_distfiles(portage.mirrors.region, portage.mirrors.site)


def _appended_distfiles(portage: PortageConfig) -> tuple[str, ...]:
    """gentoo-zh's own distfiles, when they were asked for. They hold the
    sources of that overlay's packages and no main mirror carries them.

    The overlay decides as well as the flag: without it nothing on the machine
    can want those sources, and the flag's default is on, so a minimal
    configuration wrote five hosts into the installed `make.conf` that its
    operator never chose and nothing would ever fetch from.
    """
    if not portage.mirrors.gentoo_zh_distfiles:
        return ()
    if not any(
        overlay.name == mirrors.GENTOO_ZH_OVERLAY for overlay in portage.overlays
    ):
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
