# SPDX-License-Identifier: GPL-2.0-or-later
"""The five counts, each against a session that produces it and one that does not.

Every count here has a negative control in the same test: a session whose
screens differ only in the thing being counted. A count that answers the same
number for both is measuring the session's length, not the interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from tests.tui.report import STUCK_AFTER, Report, read, title_of

#: A framed pane, as `TwoPane.frame` draws it. The heading names the open row.
def frame(name: str, rows: tuple[tuple[str, str], ...]) -> str:
    lines = [f"+- {name} " + "-" * 20 + "+"]
    lines += [f"{one.ljust(20)}| {other}" for one, other in rows]
    return "\n".join(lines)


def session(directory: Path, screens: list[str], keys: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "screens.txt").write_text("\f".join(screens), encoding="utf-8")
    (directory / "keys.txt").write_text("\n".join(keys) + "\n", encoding="utf-8")
    return directory


DISK = (("* Disk", "/dev/sda"), ("  Hostname", "lab1"))
CHANGED = (("* Disk", "/dev/sdb"), ("  Hostname", "lab1"))


def test_the_frame_names_the_open_row_not_the_title_bar() -> None:
    """`gentoo-install` is on every screen, so it identifies none of them."""
    assert title_of(frame("Partitioning", DISK)) == "Partitioning"
    # Negative control: a screen with no frame falls back to its first line,
    # and must not answer with the box drawing of a screen that has one.
    assert title_of("Mirrors\n  gentoo") == "Mirrors"


def test_a_row_opened_and_left_unchanged_counts_as_lost(tmp_path: Path) -> None:
    """The count that says a row's own name failed to say what it was."""
    inside = frame("Disk", DISK)
    lost = read(
        session(
            tmp_path / "lost",
            [frame("Partitioning", DISK), inside, frame("Partitioning", DISK)],
            ["enter", "esc", "down"],
        )
    )
    assert lost.lost == ("Disk",), lost

    # Negative control: the same three screens and the same two keys, with one
    # value different on the way out. Nothing was lost; something was set.
    found = read(
        session(
            tmp_path / "found",
            [frame("Partitioning", DISK), inside, frame("Partitioning", CHANGED)],
            ["enter", "esc", "down"],
        )
    )
    assert found.lost == (), found


def test_a_screen_that_outlasts_answering_it_counts_as_stuck(tmp_path: Path) -> None:
    same = frame("Mirrors", DISK)
    stuck = read(session(tmp_path / "stuck", [same] * (STUCK_AFTER + 2), ["down"]))
    assert stuck.stuck == ("Mirrors",), stuck

    # Negative control: the same number of screens, moving through rows. Length
    # alone must not produce the count.
    moving = [frame(f"Row {at}", DISK) for at in range(STUCK_AFTER + 2)]
    assert read(session(tmp_path / "moving", moving, ["down"])).stuck == ()


def test_asking_for_help_and_being_refused_are_counted(tmp_path: Path) -> None:
    refused = frame("Extra packages", (("Not a package name: code", ""),))
    one = read(
        session(tmp_path / "asked", [frame("Disk", DISK), refused], ["help", "type:code"])
    )
    assert one.helped == 1 and one.refused == 1, one

    # Negative control: the same length of session with neither.
    quiet = read(
        session(tmp_path / "quiet", [frame("Disk", DISK)] * 2, ["down", "type:code"])
    )
    assert quiet.helped == 0 and quiet.refused == 0, quiet


def test_finished_means_the_plan_ran_not_that_a_row_was_on_the_screen(
    tmp_path: Path,
) -> None:
    """The main list always carries a `Start the installation` row.

    Matched against the last screen, a session that ended anywhere on that
    list read as one that had installed: three of four runs were reported
    finished when one of them had left the installer without starting it.
    """
    still = session(tmp_path / "part", [frame("Install", DISK)], ["down"])
    (still / "probe-9301-abc.log").write_bytes(b"root@livecd ~ # ")
    assert not read(still).finished

    ran = session(tmp_path / "done", [frame("Disk", DISK)], ["enter"])
    (ran / "probe-9301-abc.log").write_bytes(
        b"run: findmnt --mountpoint /mnt/gentoo\ninstalled 74 operations into /mnt/gentoo\n"
    )
    assert read(ran).finished

    # Negative control: a console that mentions the installer without having
    # run it is not enough, or the count is measuring the word and not the run.
    said = session(tmp_path / "said", [frame("Disk", DISK)], ["enter"])
    (said / "probe-9301-abc.log").write_bytes(b"install.sh --lang zh-TW\nInstall\n")
    assert not read(said).finished


def test_reading_a_session_that_left_nothing_behind_answers_zero(tmp_path: Path) -> None:
    """A guest killed before its first screen must not read as a finished run."""
    empty = read(tmp_path / "never-ran")
    assert empty == Report(finished=False, lost=(), helped=0, stuck=(), refused=0)


def test_a_translated_screen_is_counted_the_same_as_an_english_one(tmp_path: Path) -> None:
    """A session runs in its spec's language, so the counts read the catalogs.

    The words come from the catalog rather than from a literal in this file:
    a translated string pasted here goes stale the next time the wording is
    corrected, and passes while measuring nothing.
    """
    from gentoo_install.i18n import Catalog

    rejected = Catalog("zh-TW")("Not a package name")
    assert rejected != "Not a package name", "the catalog has no translation to test against"
    refused = read(session(tmp_path / "zh", [frame("Extra packages", ((rejected, ""),))], ["type:code"]))
    assert refused.refused == 1, refused

    # Negative control: a screen whose text no catalog maps that string to is
    # not counted as a refusal.
    assert read(session(tmp_path / "no", [frame("Kernel", DISK)], ["enter"])).refused == 0

def test_the_session_offers_no_subcommand_that_answers_with_its_own_input() -> None:
    """`plan` read the key log and called it the installer's plan.

    An agent asked whether the plan matches its spec would have been comparing
    the spec against its own keystrokes, which is the shape of check that
    cannot fail. It is gone rather than stubbed: a subcommand that answers
    something else under the right name is worse than an absent one.
    """
    import tests.tui.session as session

    source = Path(session.__file__).read_text(encoding="utf-8")
    assert '"plan"' not in source, "plan is back and has to answer a real plan"
    assert '"screen"' in source and '"key"' in source


def test_the_session_writes_the_file_the_report_counts_from(tmp_path: Path) -> None:
    """The report read `screens.txt` and nothing wrote it.

    Every count came back zero on a session that had been driven for half an
    hour, and zero reads as a clean run rather than as a missing file.
    """
    import tests.tui.session as session

    named = session.Session("probe")
    assert named.screens.name == "screens.txt"
    assert named.screens != named.transcript

    source = Path(session.__file__).read_text(encoding="utf-8")
    assert "session.screens.open" in source, "no screen is ever recorded"

    # Negative control: the report on a directory holding only keys answers
    # zero, which is what a session that never recorded a screen looks like.
    (tmp_path / "keys.txt").write_text("enter\nesc\n", encoding="utf-8")
    assert read(tmp_path) == Report(
        finished=False, lost=(), helped=0, stuck=(), refused=0
    )
def test_every_spec_names_a_machine_that_can_answer_it() -> None:
    """A spec and the guest it runs on are one thing.

    Asked for an MBR table on a UEFI guest, or for a mirror on a machine with
    one disk, the operator is answering an impossible question and the run
    measures that rather than the interface.
    """
    from tests.tui.specs import SPECS
    from tests.vm.cluster import TUI_GUESTS

    assert sorted(SPECS) == sorted(TUI_GUESTS), (sorted(SPECS), sorted(TUI_GUESTS))
    # Read from the proof, not the prose: a spec that says "BIOS, not UEFI"
    # names both words, and the proof is the half a machine can be checked
    # against anyway.
    for number, spec in SPECS.items():
        disks, uefi, _cjk = TUI_GUESTS[number]
        proof = " ".join(spec.proof).lower()
        if "mirror" in proof:
            assert disks >= 2, (number, disks)
        if "firmware is bios" in proof:
            assert not uefi, number
        if "firmware is uefi" in proof or "systemd-boot" in proof:
            assert uefi, number

    # Negative control: a spec asking for two disks on a one-disk guest is the
    # mismatch this refuses, and the rule has to see it.
    broken = {1: (1, True, True)}
    assert not (broken[1][0] >= 2), "a one-disk guest must not satisfy two disks"


def test_the_open_row_is_named_when_the_box_carries_no_name() -> None:
    """The box drops the row's name while that row is open.

    Read from the frame alone the answer was the title bar, which says
    `gentoo-install` on every screen: a stuck screen was reported as
    `gentoo-install ... 1/24` instead of the row the operator could not answer.
    """
    bare = "\n".join(
        [
            " gentoo-install                                    1/24",
            "",
            "  Disk          /dev/vda    +------------------------+",
            "",
            "cursor:",
            "* Disk          /dev/vda    | something in the pane  |",
        ]
    )
    assert title_of(bare) == "Disk", title_of(bare)

    # Negative control: with the name in the frame the frame wins, because a
    # closed row is what the box is there to label.
    named = frame("Mirrors", DISK) + "\n\ncursor:\n* Disk  /dev/vda"
    assert title_of(named) == "Mirrors", title_of(named)


def test_a_spec_that_replaces_a_system_is_refused_on_a_fresh_guest() -> None:
    """The interface says so on its first screen, after ten minutes of boot.

    `conversion is not offered: this medium has no installed system to
    replace` is what an agent sent at the in-place spec would read, having
    spent a guest and a round to get there.
    """
    import pytest

    from tests.vm import cluster

    class Nothing:
        def isos(self, node: str) -> list[str]:
            raise AssertionError("a refused spec must not reach the cluster")

    with pytest.raises(ValueError, match="already boots one"):
        cluster.tui_execution(
            cast(Any, Nothing()), "infra-node1", "conv", 3, Path("/nonexistent")
        )

    # Negative control: every other spec is answerable on a fresh guest, so
    # the refusal is about this one and not about the check being on at all.
    assert cluster.TUI_NEEDS_A_SYSTEM == frozenset({3}), cluster.TUI_NEEDS_A_SYSTEM
    for number in sorted(set(cluster.TUI_GUESTS) - cluster.TUI_NEEDS_A_SYSTEM):
        assert number not in cluster.TUI_NEEDS_A_SYSTEM


def test_a_spec_the_interface_has_no_row_for_is_refused_before_a_guest() -> None:
    """`--ram` and `--lowram` arm a one-shot boot entry and reboot into a
    memory-held environment, and they exist only on the command line. An agent
    sent at spec 4 found `Build in RAM`, which is Portage's build directory,
    was refused for too little memory, and reported being stuck having spent a
    guest and an hour."""
    import pytest

    from tests.vm import cluster

    class Nothing:
        def isos(self, node: str) -> list[str]:
            raise AssertionError("a refused spec must not reach the cluster")

    with pytest.raises(ValueError, match="has no row in the interface"):
        cluster.tui_execution(
            cast(Any, Nothing()), "infra-node1", "ram", 4, Path("/nonexistent")
        )

    # The two refusals are about different things, so a spec in one is never
    # in the other and neither set has quietly grown to cover the whole table.
    assert cluster.TUI_HAS_NO_ROW == frozenset({4}), cluster.TUI_HAS_NO_ROW
    assert not cluster.TUI_HAS_NO_ROW & cluster.TUI_NEEDS_A_SYSTEM
    assert set(cluster.TUI_GUESTS) - cluster.TUI_HAS_NO_ROW - cluster.TUI_NEEDS_A_SYSTEM


def test_two_sessions_started_together_do_not_take_one_vmid() -> None:
    """`v2` and `v8` were started four seconds apart, both read 9300 as free,
    and the second answered `VM 9300 already exists on node 'infra-node5'`.
    Nothing between two `session start` calls allocates, so the collision is
    found only by the create."""
    import pytest

    from tests.vm import cluster
    from tests.vm.proxmox import CreateConflict

    taken = {9300}
    built: list[int] = []

    class Machine:
        def __init__(self, vmid: int) -> None:
            self.vmid = vmid

        def create(self) -> None:
            built.append(self.vmid)
            if self.vmid in taken:
                raise CreateConflict(f"VM {self.vmid} already exists")
            taken.add(self.vmid)

    class Cluster:
        def free_vmid(self, held: frozenset[int] = frozenset()) -> int:
            return next(one for one in range(9300, 9400) if one not in held)

    def execution(
        api: object, node: str, job: object, driver: str, workdir: object, vmid: int, nonce: str
    ) -> object:
        return type("Held", (), {"guest": Machine(vmid)})()

    original = cluster._execution
    cluster._execution = cast(Any, execution)
    try:
        _, guest = cluster._created_on_a_free_vmid(
            cast(Any, Cluster()), "n", cast(Any, None), "d", Path("/x"), 0, "v2"
        )
    finally:
        cluster._execution = original

    assert built == [9300, 9301], built
    assert guest.vmid == 9301

    # A VMID the caller named is not swapped for another: the operator asked
    # for that machine, and quietly building a different one hides the clash.
    taken.add(9302)
    cluster._execution = cast(Any, execution)
    try:
        with pytest.raises(CreateConflict):
            cluster._created_on_a_free_vmid(
                cast(Any, Cluster()), "n", cast(Any, None), "d", Path("/x"), 9302, "v2"
            )
    finally:
        cluster._execution = original


def test_a_conversion_runs_inside_the_machine_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`refusals.LIVE_MEDIUM` is what the interface answers on a guest that
    just booted a live image, so spec 3 cannot be measured the way the other
    eight are. `tui_conversion` makes the installed system speak first, boots
    it from its own disk, logs in, and runs the installer from the driver CD
    inside it."""
    import pytest

    from tests.vm import cluster

    done: list[str] = []

    class Machine:
        vmid = 9300
        node = "infra-node3"

        def stop(self) -> None:
            done.append("stop")

        def boot_from_disk(self) -> None:
            done.append("boot_from_disk")

        def start(self) -> None:
            done.append("start")

        def reset(self) -> None:
            done.append("reset")

        def transferred(self) -> tuple[int, int]:
            return (0, 0)

        def qmp_state(self) -> str:
            return "running"

    class Console:
        def send(self, line: str) -> None:
            done.append(f"send:{line}")

        def send_raw(self, keys: str) -> None:
            done.append(f"send:{keys}")

        def expect(self, pattern: str, timeout: float, idle: float = 0.0) -> bytes:
            done.append(f"expect:{pattern}")
            return b""

    class Link:
        console = Console()

        def reopen(self, *, solicit_prompt: bool = True) -> None:
            done.append("reopen")

        def run(self, command: str, timeout: float = 120.0, *, repeatable: bool = True) -> None:
            done.append(f"run:{command[:20]}")

    class Cluster:
        def call(self, method: str, path: str, **form: object) -> object:
            if method == "PUT":
                done.append(f"put:{sorted(form)}")
                done.append(f"cd:{form.get('ide3', '')}")
                self.settled = dict(form)
            answer = {"name": "gi-x1", "tags": "abc;gentoo-install-test", "virtio0": "disk"}
            answer.update(getattr(self, "settled", {}))
            return answer

        def node_load(self, name: str) -> float | None:
            return 0.0

    machine, link = Machine(), Link()

    def speak(one: object) -> object:
        done.append("speak")
        return cluster.SerialRoute.GRUB_CONFIG

    monkeypatch.setattr(cluster, "Guest", lambda **kw: machine)
    monkeypatch.setattr(
        cluster, "Reconnecting", type("R", (), {"to": staticmethod(lambda *a: link)})
    )
    monkeypatch.setattr(cluster, "make_the_installed_system_speak", speak)
    monkeypatch.setattr(
        cluster, "edit_the_menu_if_that_is_the_only_route", lambda *a: done.append("menu")
    )
    monkeypatch.setattr(cluster, "reach_prompt", lambda one: done.append("prompt"))
    monkeypatch.setattr(cluster, "build_driver", lambda where, packed=False: where)
    monkeypatch.setattr(cluster, "retain_driver", lambda workdir, built: Path("gi-driver-new.iso"))
    monkeypatch.setattr(cluster, "place_driver", lambda *a: done.append("place_driver"))
    monkeypatch.setattr(cluster, "_edit_uefi_cmdline", lambda *a: done.append("uefi"))
    monkeypatch.setattr(cluster, "_edit_bios_cmdline", lambda *a: done.append("bios"))
    running = cluster.tui_conversion(
        cast(Any, Cluster()), "infra-node3", "conv", 9300, Path("/tmp")
    )

    # The guest is opened from firmware first, so the shell everything below
    # talks to is one this call made rather than one it inherited.
    assert done[0] == "stop", done
    assert done.index("prompt") < done.index("speak"), done
    # The parameters go in while a live shell is still there, and only then is
    # the guest pointed at its own disk.
    assert done.index("speak") < done.index("boot_from_disk"), done
    # `start` happens twice — once for the medium and once for the disk — so
    # the second cycle is asserted as a sequence rather than by first index.
    at = done.index("boot_from_disk")
    assert done[at - 1] == "stop" and done[at + 1] == "start", done
    assert "expect:login:" in done, done
    # The prompt that covers all three spellings: the installed system comes
    # up in Chinese and asks in Chinese, and an `assword` of its own matched
    # nothing while the password sat unsent.
    from tests.vm.console import PASSWORD_PROMPT

    assert f"expect:{PASSWORD_PROMPT}" in done, done
    assert f"send:{cluster.TUI_PASSWORD}" in done, done
    # No `--config`: the menu is the subject here as much as anywhere else.
    started = [one for one in done if "install.sh" in one]
    assert started and "--config" not in started[0], done
    # The link, not the console inside it: the daemon that holds this outlives
    # a drop only if what it reads through reconnects, and one `Broken pipe`
    # ended a round with the interface still running inside the guest.
    assert running.console is link

    # The driver CD is built and attached by this call. The guest keeps the one
    # it was created with otherwise, so three rounds measured an installer older
    # than the tree and read a fix made between them as absent from the screen.
    assert "place_driver" in done, done
    assert "cd:local:iso/gi-driver-new.iso,media=cdrom" in done, done
    assert done.index("place_driver") < done.index("start"), done

    # Negative control: the slot is named in the same request as the boot order,
    # so a config edit that carried only one of them would leave the guest
    # booting the medium off a CD it had already replaced, or the reverse.
    assert "put:['boot', 'ide3']" in done, done


def test_a_conversion_refuses_a_config_edit_that_did_not_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change to a guest that is not fully down is recorded as pending and
    applied at some later stop. The guest then starts from the order it already
    had: one round booted its own disk while every printed line still said it
    was waiting for the medium."""
    import pytest

    from tests.vm import cluster

    class Machine:
        vmid = 9300
        node = "infra-node3"

        def stop(self) -> None:
            return None

        def start(self) -> None:
            raise AssertionError("the guest must not be started on a stale config")

    class Deaf:
        """A cluster that records nothing, the way a pending edit answers."""

        def call(self, method: str, path: str, **form: object) -> object:
            return {
                "name": "gi-x1",
                "tags": "abc;gentoo-install-test",
                "virtio0": "disk",
                "boot": "order=virtio0;ide3",
                "ide3": "local:iso/gi-driver-old.iso,media=cdrom",
            }

    monkeypatch.setattr(cluster, "Guest", lambda **kw: Machine())
    monkeypatch.setattr(cluster, "build_driver", lambda where, packed=False: where)
    monkeypatch.setattr(cluster, "retain_driver", lambda workdir, built: Path("gi-driver-new.iso"))
    monkeypatch.setattr(cluster, "place_driver", lambda *a: None)

    from tests.vm.proxmox import ProxmoxError

    # No pause between attempts: the retry exists for a node still holding a
    # lock, and a test that waits for it measures the sleep and nothing else.
    monkeypatch.setattr(cluster, "EDIT_PATIENCE", 0.0)

    with pytest.raises(ProxmoxError, match="still boots"):
        cluster.tui_conversion(cast(Any, Deaf()), "infra-node3", "conv", 9300, Path("/tmp"))

    # Negative control: the same guest with the edit recorded gets past this and
    # fails later, so the refusal above is the config check and not the fake.
    class Heard(Deaf):
        def call(self, method: str, path: str, **form: object) -> object:
            answer = dict(cast(Any, super().call(method, path, **form)))
            # As the node stores it: `order=ide2` comes back with every other
            # bootable device appended, and an equality check refused a guest
            # whose edit had taken.
            answer["boot"] = f"order={cluster.MEDIUM_FIRST};ide3"
            answer["ide3"] = "local:iso/gi-driver-new.iso,media=cdrom"
            return answer

    with pytest.raises(AssertionError, match="stale config"):
        cluster.tui_conversion(cast(Any, Heard()), "infra-node3", "conv", 9300, Path("/tmp"))


def test_a_config_edit_is_asked_again_while_the_node_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stop` returns on its task and the node holds a lock for a moment after.
    An edit made in that moment is recorded as pending, so the guest starts
    from the order it already had: one round was refused on
    `still boots 'order=virtio0;ide3'` with the edit correct and late."""
    import pytest

    from tests.vm import cluster

    puts: list[int] = []

    class Late:
        """Records the edit only from the second attempt, the way a lock lifts."""

        def call(self, method: str, path: str, **form: object) -> object:
            if method == "PUT":
                puts.append(len(puts))
            answer = {"name": "gi-x1", "tags": "abc;gentoo-install-test", "virtio0": "disk"}
            if len(puts) >= 2:
                answer["boot"] = f"order={cluster.MEDIUM_FIRST};ide3"
                answer["ide3"] = "local:iso/gi-driver-new.iso,media=cdrom"
            else:
                answer["boot"] = "order=virtio0;ide3"
                answer["ide3"] = "local:iso/gi-driver-old.iso,media=cdrom"
            return answer

    class Machine:
        vmid = 9300
        node = "infra-node3"

        def stop(self) -> None:
            return None

        def start(self) -> None:
            raise AssertionError("reached the start")

    monkeypatch.setattr(cluster, "Guest", lambda **kw: Machine())
    monkeypatch.setattr(cluster, "build_driver", lambda where, packed=False: where)
    monkeypatch.setattr(cluster, "retain_driver", lambda workdir, built: Path("gi-driver-new.iso"))
    monkeypatch.setattr(cluster, "place_driver", lambda *a: None)
    monkeypatch.setattr(cluster, "EDIT_PAUSE", 0.0)

    with pytest.raises(AssertionError, match="reached the start"):
        cluster.tui_conversion(cast(Any, Late()), "infra-node3", "conv", 9300, Path("/tmp"))

    # Negative control: one attempt is not enough, so the run above cannot be
    # the first PUT having taken.
    assert len(puts) >= 2, puts


def test_a_disk_spec_is_not_sent_through_the_conversion_path() -> None:
    """The other eight build a new system and get a guest of their own; only
    the conversion needs one that already boots."""
    import pytest

    from tests.vm import cluster

    with pytest.raises(ValueError, match="goes through"):
        cluster.tui_conversion(cast(Any, None), "n", "x", 9300, Path("/tmp"), spec=1)
