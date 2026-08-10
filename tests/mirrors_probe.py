"""Ask every mirror in the tables whether it serves what the table claims.

Run by hand, not in the suite: it is a network measurement, and a mirror that
is down for an hour is not a defect in this repository. What it finds is used
to correct `model/mirrors.py`, which is the only place those addresses live.

    python3 -m tests.mirrors_probe

`layout.conf` is not the marker for `distfiles`: `mirror.xtom.com.hk` serves
the directory and not that file, and asking for it reported a working mirror
as broken. A `Range` header is not used either, because `ftp.twaren.net`
answers `403` to one and `200` to the same request without it.
"""

from __future__ import annotations

import concurrent.futures as futures
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Final

from gentoo_install.model import mirrors

TIMEOUT: Final[float] = 25.0

#: What each column has to answer for the table to be telling the truth.
DISTFILES_MARKER: Final[str] = "distfiles/"
RELEASES_MARKER: Final[str] = "releases/amd64/autobuilds/latest-stage3-amd64-systemd.txt"
BINPKG_MARKER: Final[str] = "binpkgs/x86-64/Packages"


@dataclass(frozen=True)
class Answer:
    table: str
    site: str
    column: str
    said: str

    @property
    def good(self) -> bool:
        return self.said in ("200", "206", "ok")


def _http(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as answer:
            answer.read(64)
            return str(answer.status)
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return type(error).__name__


def _rsync(uri: str) -> str:
    said = subprocess.run(
        ["rsync", "--contimeout=15", "--timeout=15", "--list-only", f"{uri}/"],
        capture_output=True,
        text=True,
        timeout=90,
    )
    return "ok" if said.returncode == 0 else f"rsync {said.returncode}"


def _git(uri: str) -> str:
    said = subprocess.run(
        ["git", "ls-remote", "--heads", uri],
        capture_output=True,
        text=True,
        timeout=120,
        env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin"},
    )
    return "ok" if said.returncode == 0 else f"git {said.returncode}"


def ask(table: str, site: mirrors.Site) -> list[Answer]:
    base = site.distfiles.rstrip("/")
    marker = RELEASES_MARKER if table == "gentoo" else BINPKG_MARKER
    found = [
        Answer(table, site.key, "distfiles", _http(f"{base}/{DISTFILES_MARKER}")),
        Answer(table, site.key, marker.split("/")[0], _http(f"{base}/{marker}")),
    ]
    if site.git:
        found.append(Answer(table, site.key, "git", _git(site.git)))
    if site.rsync:
        found.append(Answer(table, site.key, "rsync", _rsync(site.rsync)))
    return found


def main() -> int:
    work = [("gentoo", site) for site in mirrors.GENTOO_SITES]
    work += [("gentoo-zh", site) for site in mirrors.GENTOOZH_SITES]
    answers: list[Answer] = []
    with futures.ThreadPoolExecutor(max_workers=12) as pool:
        for got in pool.map(lambda one: ask(*one), work):
            answers.extend(got)
    for answer in sorted(answers, key=lambda one: (one.good, one.table, one.site)):
        print(f"{' ' if answer.good else '!'} {answer.table:9} "
              f"{answer.site:16} {answer.column:9} {answer.said}")
    broken = [one for one in answers if not one.good]
    print(f"\n{len(answers) - len(broken)}/{len(answers)} answered")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
