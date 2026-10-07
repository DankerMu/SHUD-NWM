"""The exclusion edit of the node-27 basin retirement tool and its wait for an autopipe round (#2757).

Partition of the ``scripts/node27_retire_basin.py`` suite; the fakes and the
``space`` fixture are in ``tests/node27_retire_basin_helpers.py``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.basin_retirement import autopipe
from tests.node27_retire_basin_helpers import (
    DATABASE_URL,
    STEPS,
    Space,
    held_flock,
    monotonic_us,
    release_flock,
    round_answer,
    running_answer,
    space,  # noqa: F401 - the fixture
    tree,
)


def _env_directory(space: Space) -> dict[str, tuple[str, str]]:  # noqa: F811
    return tree(space.env_file.parent, without=(space.retire_lock,))


def _all_but_the_failure_receipt(space: Space) -> dict[str, tuple[str, str]]:  # noqa: F811
    """The whole ``tmp_path`` tree but the basin version's receipt directory, the lock and the fake's own files."""

    return tree(space.root, without=(space.retire_lock, space.systemctl_directory, space.directory()))


def _exclude_failed(space: Space, *, wait: float | None = None) -> dict[str, Any]:  # noqa: F811
    """Apply, assert that it stopped in ``exclude`` before any database write, and return the failure receipt."""

    database_before = space.database.snapshot()
    status, report = space.run(wait=wait)
    assert status == 1 and report["outcome"] == "failed" and report["steps"] == {}
    assert space.receipts_present() == []
    assert space.database.snapshot() == database_before and space.database.commits == 0
    assert space.store.preflights == [] and space.store.operations == []
    assert not (space.directory() / "hydro-run-backup.csv").exists()
    (failure,) = space.failures()
    assert failure["schema_version"] == "nhms.basin_retirement.failure_receipt.v1"
    assert (failure["step"], failure["outcome"], failure["completed_steps"]) == ("exclude", "failed", [])
    assert report["failure"]["reason"] == failure["reason"]
    return failure


def _symlink(space: Space) -> None:  # noqa: F811
    real = space.env_file.with_name("real.env")
    space.env_file.rename(real)
    space.env_file.symlink_to(real)


def _mode_0644(space: Space) -> None:  # noqa: F811
    os.chmod(space.env_file, 0o644)


def _two_assignment_lines(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text() + "AUTOPIPE_EXCLUDE_BASINS=neiliuqu\n")


def _quoted_value(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text(excluded='"zhaochen_hhy,hhe"'))


def _trailing_comment(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text(excluded="zhaochen_hhy,hhe # retired 2026-08"))


def _extra_export_line(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text() + "export AUTOPIPE_EXCLUDE_BASINS=neiliuqu\n")


def _extra_append_line(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text() + "  AUTOPIPE_EXCLUDE_BASINS+=,neiliuqu\n")


def _no_assignment_line(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text().replace("AUTOPIPE_EXCLUDE_BASINS=zhaochen_hhy,hhe\n", ""))


def _carriage_returns(space: Space) -> None:  # noqa: F811
    space.env_file.write_bytes(space.env_text().replace("\n", "\r\n").encode("utf-8"))


def _quoted_lock_path(space: Space) -> None:  # noqa: F811
    path = space.autopipe_lock
    space.write_env(space.env_text().replace(f"AUTOPIPE_LOCK_PATH={path}", f'AUTOPIPE_LOCK_PATH="{path}"'))


UNEXPECTED_ENV_FILES: list[tuple[Callable[[Space], None], str]] = [
    (_symlink, "is a symlink"),
    (_mode_0644, "has mode 0644; it must be exactly 0600"),
    (_two_assignment_lines, "it has 2 such lines and 2 lines assigning AUTOPIPE_EXCLUDE_BASINS in any form"),
    (_quoted_value, "it has 0 such lines and 1 lines assigning AUTOPIPE_EXCLUDE_BASINS in any form"),
    (_trailing_comment, "it has 0 such lines and 1 lines assigning AUTOPIPE_EXCLUDE_BASINS in any form"),
    (_extra_export_line, "it has 1 such lines and 2 lines assigning AUTOPIPE_EXCLUDE_BASINS in any form"),
    (_extra_append_line, "it has 1 such lines and 2 lines assigning AUTOPIPE_EXCLUDE_BASINS in any form"),
    (_no_assignment_line, "it has 0 such lines and 0 lines assigning AUTOPIPE_EXCLUDE_BASINS in any form"),
    (_carriage_returns, "it has 0 such lines and 1 lines assigning AUTOPIPE_EXCLUDE_BASINS in any form"),
    (_quoted_lock_path, "Which lock file the autopipe holds cannot be told"),
]


@pytest.mark.parametrize(("arrange", "said"), UNEXPECTED_ENV_FILES)
def test_an_unexpected_env_file_fails_exclude_and_is_left_unchanged(
    space: Space,  # noqa: F811
    arrange: Callable[[Space], None],
    said: str,
) -> None:
    arrange(space)
    if arrange is _carriage_returns:
        # The DATABASE_URL line must still bind: with a carriage return it would be refused earlier.
        space.monkeypatch.setenv("DATABASE_URL", DATABASE_URL + "\r")
    before = _all_but_the_failure_receipt(space)

    failure = _exclude_failed(space)

    assert said in failure["reason"]
    # The file, its mode, the link: all as they were, and no backup or temporary file beside it. The one
    # thing written anywhere is the failure receipt.
    assert _all_but_the_failure_receipt(space) == before
    assert [path.name.startswith("retire-failed-") for path in space.directory().iterdir()] == [True]
    assert space.systemctl_calls() == []


def test_an_env_file_of_another_user_fails_exclude(space: Space) -> None:  # noqa: F811
    space.monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    before = _all_but_the_failure_receipt(space)

    failure = _exclude_failed(space)

    assert "not by this user" in failure["reason"]
    assert _all_but_the_failure_receipt(space) == before


@pytest.mark.parametrize("listed", ["zhaochen_hhy,huai,hhe", "zhaochen_hhy,basins-HUAI", "Basins_Huai"])
def test_a_key_already_in_the_list_is_not_written_and_the_wait_still_runs(space: Space, listed: str) -> None:  # noqa: F811
    space.write_env(space.env_text(excluded=listed))
    before = _env_directory(space)

    status, report = space.run()

    assert status == 0, space.last_stderr
    assert report["steps"] == {step: "completed" for step in STEPS}
    # Nothing written beside or into the env file: no backup either.
    assert _env_directory(space) == before
    assert space.excluded() == listed
    exclude = space.receipt("exclude")
    assert exclude["file_changed"] is False and exclude["env_backup"] is None
    assert exclude["sha256_before"] == exclude["sha256_after"]
    assert "already in the list" in exclude["note"]
    # The wait ran all the same, from a reference taken at the start of the step.
    waited = exclude["autopipe_round"]
    assert waited["polls"] == 1 and waited["start_monotonic_us"] > waited["reference_monotonic_us"]
    assert len(space.systemctl_calls()) == 2


def test_the_key_is_the_first_entry_when_the_list_is_empty(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text(excluded=""))
    before = space.env_file.read_bytes()

    assert space.run()[0] == 0, space.last_stderr

    assert space.env_file.read_bytes() == before.replace(
        b"AUTOPIPE_EXCLUDE_BASINS=\n", b"AUTOPIPE_EXCLUDE_BASINS=huai\n"
    )


def test_an_env_backup_of_an_earlier_attempt_with_the_same_bytes_is_kept(space: Space) -> None:  # noqa: F811
    backup = space.env_backup()
    backup.write_bytes(space.env_file.read_bytes())
    os.chmod(backup, 0o600)
    kept = (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes())

    status, _report = space.run()

    assert status == 0, space.last_stderr
    assert (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes()) == kept
    assert space.excluded() == "zhaochen_hhy,hhe,huai"
    exclude = space.receipt("exclude")
    assert exclude["file_changed"] is True and exclude["env_backup_outcome"] == "kept"


def test_an_env_backup_with_other_bytes_fails_exclude_and_nothing_is_written(space: Space) -> None:  # noqa: F811
    backup = space.env_backup()
    backup.write_bytes(space.env_file.read_bytes().replace(b"zhaochen_hhy,hhe", b"zhaochen_hhy"))
    os.chmod(backup, 0o600)
    before = _all_but_the_failure_receipt(space)

    failure = _exclude_failed(space)

    assert "already exists and its bytes differ" in failure["reason"] and "never" in failure["reason"]
    assert _all_but_the_failure_receipt(space) == before
    assert space.systemctl_calls() == []


def test_a_round_in_flight_at_the_reference_does_not_count(space: Space) -> None:  # noqa: F811
    in_flight_since = monotonic_us()
    space.set_answers(
        running_answer(start=in_flight_since),
        # It ends by itself with 0, after the edit: it read the old list all the same.
        round_answer(start=in_flight_since, end="now"),
        round_answer(),
    )

    status, _report = space.run()

    assert status == 0, space.last_stderr
    waited = space.receipt("exclude")["autopipe_round"]
    assert waited["polls"] == 3
    assert waited["start_monotonic_us"] > waited["reference_monotonic_us"] > in_flight_since


def test_a_round_that_started_after_the_reference_and_exited_with_0_counts(space: Space) -> None:  # noqa: F811
    space.set_answers(round_answer(status=0))

    assert space.run()[0] == 0, space.last_stderr

    waited = space.receipt("exclude")["autopipe_round"]
    assert (waited["polls"], waited["exit_code"], waited["exit_status"]) == (1, 1, 0)
    assert waited["unit"] == "nhms-node27-autopipe.service"


def test_a_round_in_which_a_basin_failed_counts(space: Space) -> None:  # noqa: F811
    # Exit status 1: some basin's run or seed failed; the round read the exclusion list in full.
    space.set_answers(round_answer(state="failed", status=1))

    assert space.run()[0] == 0, space.last_stderr

    waited = space.receipt("exclude")["autopipe_round"]
    assert (waited["polls"], waited["exit_code"], waited["exit_status"]) == (1, 1, 1)
    assert space.receipt("verify")["autopipe_round"]["exit_status"] == 1


@pytest.mark.parametrize(
    "proves_nothing",
    [
        round_answer(state="failed", status=2),  # blocked at its bootstrap or preflight
        round_answer(state="failed", code=2, status=9),  # CLD_KILLED
        round_answer(state="failed", code=3, status=6),  # CLD_DUMPED
        round_answer(state="failed", status=3),  # an exit status the autopipe does not use
        round_answer(end=0),  # no exit timestamp
    ],
)
def test_a_blocked_or_signalled_round_does_not_count(space: Space, proves_nothing: dict[str, Any]) -> None:  # noqa: F811
    space.set_answers(proves_nothing, round_answer())

    assert space.run()[0] == 0, space.last_stderr

    waited = space.receipt("exclude")["autopipe_round"]
    assert (waited["polls"], waited["exit_code"], waited["exit_status"]) == (2, 1, 0)


def test_a_round_that_ended_while_the_autopipe_lock_was_held_does_not_count_and_the_next_does(
    space: Space,  # noqa: F811
) -> None:
    # A round started outside systemd holds the lock, as flock(1) does: with flock(2), from another process.
    with held_flock(space.autopipe_lock) as holder:
        sleeps: list[float] = []

        def manual_round_ends(seconds: float) -> None:
            sleeps.append(seconds)
            release_flock(holder)

        space.monkeypatch.setattr(autopipe, "sleep", manual_round_ends)

        status, _report = space.run()

    assert status == 0, space.last_stderr
    waited = space.receipt("exclude")["autopipe_round"]
    assert waited["polls"] == 2 and len(sleeps) == 1
    (skipped,) = waited["rounds_skipped_while_lock_held"]
    # The skipped round qualified by every other rule.
    assert (skipped["exit_code"], skipped["exit_status"], skipped["active_state"]) == (1, 0, "inactive")
    assert skipped["start_monotonic_us"] > waited["reference_monotonic_us"]
    assert waited["start_monotonic_us"] > skipped["start_monotonic_us"]
    assert waited["lock_held"] is False and waited["lock_path_probed"] == str(space.autopipe_lock)


def test_the_same_round_is_not_accepted_once_the_lock_is_free(space: Space) -> None:  # noqa: F811
    # The systemd round only skipped; reading it again after the manual round ended proves nothing new.
    skipped_start = monotonic_us() + 60_000_000
    space.set_answers(
        round_answer(start=skipped_start),
        round_answer(start=skipped_start),
        round_answer(start=skipped_start + 1),
    )
    with held_flock(space.autopipe_lock) as holder:
        space.monkeypatch.setattr(autopipe, "sleep", lambda _seconds: release_flock(holder))

        status, _report = space.run()

    assert status == 0, space.last_stderr
    waited = space.receipt("exclude")["autopipe_round"]
    assert waited["polls"] == 3 and waited["start_monotonic_us"] == skipped_start + 1


def test_the_lock_path_is_the_env_files_line_when_there_is_one(space: Space) -> None:  # noqa: F811
    process_lock = Path(os.environ["NODE27_AUTOPIPE_LOCK_PATH"])
    assert process_lock != space.autopipe_lock

    # The lock of the process environment is held, the one the env file names is not: the round counts.
    with held_flock(process_lock):
        status, _report = space.run()

    assert status == 0, space.last_stderr
    waited = space.receipt("exclude")["autopipe_round"]
    assert waited["polls"] == 1 and waited["lock_path_probed"] == str(space.autopipe_lock)
    # Opened read-only and never created.
    assert not space.autopipe_lock.exists()


@pytest.mark.parametrize("line", ["", "AUTOPIPE_LOCK_PATH=\n"])
def test_without_a_line_in_the_env_file_the_lock_path_is_the_process_environments(
    space: Space,  # noqa: F811
    line: str,
) -> None:
    process_lock = Path(os.environ["NODE27_AUTOPIPE_LOCK_PATH"])
    space.write_env(space.env_text().replace(f"AUTOPIPE_LOCK_PATH={space.autopipe_lock}\n", line))

    with held_flock(space.autopipe_lock), held_flock(process_lock) as holder:
        space.monkeypatch.setattr(autopipe, "sleep", lambda _seconds: release_flock(holder))
        status, _report = space.run()

    assert status == 0, space.last_stderr
    waited = space.receipt("exclude")["autopipe_round"]
    assert waited["lock_path_probed"] == str(process_lock)
    assert len(waited["rounds_skipped_while_lock_held"]) == 1


UNRELIABLE_ANSWERS: list[tuple[dict[str, Any], str]] = [
    (
        {"raw": "ActiveState=inactive\nExecMainStartTimestampMonotonic=soon\nExecMainExitTimestampMonotonic=0\n"
                "ExecMainCode=1\nExecMainStatus=0\n"},
        "ExecMainStartTimestampMonotonic='soon', which is not a number",
    ),  # fmt: skip
    (
        {"raw": "ActiveState=inactive\nExecMainStartTimestampMonotonic=5\nExecMainExitTimestampMonotonic=6\n"},
        "did not print ['ExecMainCode', 'ExecMainStatus']",
    ),
    ({"raw": "Failed to connect to bus: No medium found\n"}, "printed a line this tool does not know"),
    ({"raw": ""}, "did not print"),
    (
        {"raw": "ActiveState=inactive\nExecMainStartTimestampMonotonic=5\nExecMainExitTimestampMonotonic=6\n"
                "ExecMainCode=exited\nExecMainStatus=0\n"},
        "ExecMainCode='exited', which is not a number",
    ),  # fmt: skip
    ({"rc": 1}, "exited 1"),
]


@pytest.mark.parametrize(("answer", "said"), UNRELIABLE_ANSWERS)
def test_an_answer_of_systemctl_that_cannot_be_relied_on_fails_the_step(
    space: Space,  # noqa: F811
    answer: dict[str, Any],
    said: str,
) -> None:
    space.set_answers(answer)

    failure = _exclude_failed(space)

    assert said in failure["reason"]
    assert len(space.systemctl_calls()) == 1


def test_a_systemctl_that_cannot_be_run_fails_the_step(space: Space) -> None:  # noqa: F811
    space.monkeypatch.setenv("NHMS_BASIN_RETIREMENT_SYSTEMCTL", str(space.root / "no-such-systemctl"))

    failure = _exclude_failed(space)

    assert "could not be run" in failure["reason"]


def test_a_timeout_fails_the_step_and_the_rerun_waits_again_without_a_second_backup(space: Space) -> None:  # noqa: F811
    env_before = space.env_file.read_bytes()
    space.set_answers(running_answer())

    failure = _exclude_failed(space, wait=1.5)

    assert "No autopipe round that started after the reference ended within" in failure["reason"]
    assert "it waits again, from a new reference" in failure["reason"]
    # The key is in the env file, the backup is written, and there is no receipt of the step.
    assert space.excluded() == "zhaochen_hhy,hhe,huai"
    backup = space.env_backup()
    kept = (backup.stat().st_ino, backup.stat().st_mtime_ns)
    assert backup.read_bytes() == env_before
    env_after_first = space.env_file.read_bytes()
    polls_before = len(space.systemctl_calls())
    assert polls_before >= 2

    space.set_answers(round_answer())
    status, report = space.run()

    assert status == 0, space.last_stderr
    assert report["steps"] == {step: "completed" for step in STEPS}
    # The rerun waited again and wrote neither the env file nor a second backup.
    assert len(space.systemctl_calls()) == polls_before + 2
    assert space.env_file.read_bytes() == env_after_first
    assert (backup.stat().st_ino, backup.stat().st_mtime_ns) == kept and backup.read_bytes() == env_before
    assert sorted(path.name for path in space.env_file.parent.glob("*.bak-*")) == [backup.name]
    exclude = space.receipt("exclude")
    assert exclude["file_changed"] is False and exclude["env_backup"] == str(backup)
    assert exclude["autopipe_round"]["polls"] == 1
    assert len(space.failures()) == 1


def test_the_tool_only_ever_shows_the_unit(space: Space) -> None:  # noqa: F811
    recorded: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(command: list[str], **kwargs: Any) -> Any:
        recorded.append(list(command))
        return real_run(command, **kwargs)

    space.monkeypatch.setattr(subprocess, "run", recording_run)

    assert space.run()[0] == 0, space.last_stderr

    systemctl = [command for command in recorded if command[0].endswith("systemctl")]
    assert len(systemctl) == 2
    for command in systemctl:
        assert command[1:4] == ["--user", "show", "nhms-node27-autopipe.service"]
        assert command[4:] == [
            "-p",
            "ActiveState,ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic,ExecMainCode,ExecMainStatus",
        ]
    # Nothing else is started by the tool but git, for the commit in the receipts.
    assert {command[0] for command in recorded if command not in systemctl} <= {"git"}
    assert stat.S_IMODE(space.env_file.stat().st_mode) == 0o600
