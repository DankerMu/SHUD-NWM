"""The supersede, deactivate and verify steps of the node-27 basin retirement tool: failures and reruns (#2757).

Partition of the ``scripts/node27_retire_basin.py`` suite; the fakes and the
``space`` fixture are in ``tests/node27_retire_basin_helpers.py``.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from packages.common import succession_receipt
from tests.node27_retire_basin_helpers import (
    HUAI,
    STEPS,
    SUPERSEDE_SQL,
    Space,
    round_answer,
    running_answer,
    space,  # noqa: F401 - the fixture
    tree,
)

ACTIVE = ["basins_huai_shud", "dg_huai_gfs", "dg_huai_ifs"]
CANDIDATE_RUNS = {"huai-01": "succeeded", "huai-02": "parsed", "huai-03": "published", "huai-04": "published"}


def _failed_in(space: Space, step: str) -> dict[str, Any]:  # noqa: F811
    """Apply, assert that it stopped in ``step`` with the steps before it completed, and return the failure receipt."""

    status, report = space.run()
    before = list(STEPS[: STEPS.index(step)])
    assert status == 1 and report["outcome"] == "failed", space.last_stderr
    assert [name for name in STEPS if name in report["steps"]] == before
    assert space.receipts_present() == before
    # The receipt this run wrote: two of the same second are told apart by a counter, not by their order.
    failure = json.loads(Path(report["failure"]["failure_receipt"]).read_text(encoding="utf-8"))
    assert (failure["step"], failure["outcome"], failure["completed_steps"]) == (step, "failed", before)
    assert failure in space.failures()
    return failure


def _candidate_statuses(space: Space) -> dict[str, str]:  # noqa: F811
    return {run_id: status for run_id, status in space.database.statuses(HUAI).items() if run_id in CANDIDATE_RUNS}


def test_supersede_with_no_run_to_supersede_succeeds_with_zero(space: Space) -> None:  # noqa: F811
    for run_id in CANDIDATE_RUNS:
        space.database.runs[run_id]["status"] = "failed"
    statuses = space.database.statuses(HUAI)

    status, _report = space.run()

    assert status == 0, space.last_stderr
    supersede = space.receipt("supersede")
    assert (supersede["row_count"], supersede["updated_row_count"]) == (0, 0)
    assert supersede["status_counts"] == {"succeeded": 0, "parsed": 0, "published": 0}
    # The backup of nothing is still a file: its header line.
    backup = space.directory() / "hydro-run-backup.csv"
    assert backup.read_text(encoding="utf-8").splitlines() == [
        "run_id,run_type,model_id,basin_version_id,status,error_message,updated_at"
    ]
    assert space.database.statuses(HUAI) == statuses


def test_an_update_count_that_differs_from_the_backup_rolls_back_and_removes_the_backup(space: Space) -> None:  # noqa: F811
    # A run is registered between the backup and the update: the update would change a row nobody backed up.
    space.database.before_update = lambda: space.database.add_run("huai-99", HUAI, "published")

    failure = _failed_in(space, "supersede")

    assert "the update returned 5 rows and the backup holds 4" in failure["reason"]
    assert "updated only: ['huai-99'] (1)" in failure["reason"]
    assert "rolled back" in failure["reason"]
    # Rolled back: no run changed, the late one included; no receipt; the backup this run created is gone.
    assert _candidate_statuses(space) == CANDIDATE_RUNS
    assert space.database.runs["huai-99"]["status"] == "published"
    assert not (space.directory() / "hydro-run-backup.csv").exists()
    names = sorted(path.name for path in space.directory().iterdir())
    assert len(names) == 2 and names[0] == "retire-exclude.json" and names[1].startswith("retire-failed-")
    (writer,) = [connection for connection in space.database.connections if not connection.readonly]
    assert (writer.commits, writer.closed) == (0, True) and writer.rollbacks >= 1
    assert space.store.preflights == [] and space.store.operations == []

    # The rerun backs all five up and supersedes them.
    space.database.before_update = None
    status, _report = space.run()
    assert status == 0, space.last_stderr
    assert (space.receipt("supersede")["row_count"], space.receipt("supersede")["updated_row_count"]) == (5, 5)
    assert space.database.statuses(HUAI)["huai-99"] == "superseded"


def test_a_rerun_with_the_backup_and_no_candidate_run_writes_the_receipt_from_the_backup(space: Space) -> None:  # noqa: F811
    assert space.run()[0] == 0, space.last_stderr
    # The commit happened and the receipt did not.
    for step in ("supersede", "deactivate", "verify"):
        (space.directory() / f"retire-{step}.json").unlink()
    backup = space.directory() / "hydro-run-backup.csv"
    backup_before = (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes())
    database_before = space.database.snapshot()
    statements_before = len(space.database.statements)
    commits_before = space.database.commits

    status, report = space.run()

    assert status == 0, space.last_stderr
    assert report["steps"] == {"exclude": "skipped", "supersede": "completed", "deactivate": "completed",
                               "verify": "completed"}  # fmt: skip
    supersede = space.receipt("supersede")
    assert supersede["recovered_from_existing_backup"] is True
    assert (supersede["row_count"], supersede["updated_row_count"]) == (4, 0)
    assert supersede["status_counts"] == {"succeeded": 1, "parsed": 1, "published": 2}
    assert supersede["run_backup_sha256"] == hashlib.sha256(backup_before[2]).hexdigest()
    # Nothing changed: no second backup, no update, no commit.
    assert (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes()) == backup_before
    assert space.database.snapshot() == database_before
    later = space.database.statements[statements_before:]
    assert SUPERSEDE_SQL not in later and not any(statement.startswith("COPY") for statement in later)
    assert space.database.commits == commits_before


def test_a_rerun_with_the_backup_and_candidate_runs_fails_and_changes_nothing(space: Space) -> None:  # noqa: F811
    # An attempt killed before its commit left its backup behind.
    space.directory().mkdir(parents=True)
    backup = space.directory() / "hydro-run-backup.csv"
    backup.write_text("run_id,status\nhuai-01,succeeded\n", encoding="utf-8")
    backup_before = (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes())

    failure = _failed_in(space, "supersede")

    # Both facts, and the way on.
    assert f"The run backup {backup} of an earlier attempt exists" in failure["reason"]
    assert f"4 runs of {HUAI} are in succeeded / parsed / published" in failure["reason"]
    assert "move the CSV aside" in failure["reason"]
    assert (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes()) == backup_before
    assert _candidate_statuses(space) == CANDIDATE_RUNS
    assert SUPERSEDE_SQL not in space.database.statements and space.database.commits == 0
    assert space.store.preflights == [] and space.store.operations == []

    # The operator compared, moved the CSV aside and ran again.
    backup.rename(backup.with_name("hydro-run-backup.csv.aside"))
    status, _report = space.run()
    assert status == 0, space.last_stderr
    assert space.receipt("supersede")["row_count"] == 4


def test_deactivate_fails_before_any_row_when_the_key_left_the_env_file(space: Space) -> None:  # noqa: F811
    space.store.preflight_blockers["dg_huai_gfs"] = [{"code": "MISSING_ACTIVE_RISK", "message": "blocked once"}]
    _failed_in(space, "deactivate")
    # Meanwhile somebody restored the env file from its backup.
    space.env_backup().replace(space.env_file)
    space.store.preflight_blockers.clear()
    space.store.preflights.clear()

    failure = _failed_in(space, "deactivate")

    assert "The key 'huai' is no longer in AUTOPIPE_EXCLUDE_BASINS" in failure["reason"]
    assert "re-activates every row" in failure["reason"] and "No row was changed." in failure["reason"]
    # Not even a preflight: the store was not asked at all.
    assert space.store.preflights == [] and space.store.operations == []
    assert space.database.active(HUAI) == ACTIVE
    assert space.excluded() == "zhaochen_hhy,hhe"


def test_a_preflight_blocker_on_one_row_deactivates_none(space: Space) -> None:  # noqa: F811
    blocker = {"code": "OVERRIDE_REQUIRES_SYS_ADMIN", "message": "Missing-active override requires sys_admin."}
    space.store.preflight_blockers["dg_huai_gfs"] = [blocker]

    failure = _failed_in(space, "deactivate")

    assert "The deactivate preflight is not ready for ['dg_huai_gfs']" in failure["reason"]
    assert "No row was changed." in failure["reason"]
    # All three were preflighted, in order, before anything else; no operation was called.
    assert [call["model_id"] for call in space.store.preflights] == ACTIVE
    assert space.store.operations == []
    assert space.database.active(HUAI) == ACTIVE and space.database.audit_log == []
    assert failure["rows_done"] == []
    rows = {row["model_id"]: row for row in failure["model_rows"]}
    assert rows["dg_huai_gfs"]["preflight_blockers"] == [blocker]
    assert rows["dg_huai_gfs"]["preflight_status"] == "blocked"
    assert rows["basins_huai_shud"]["preflight_blockers"] == [] and rows["dg_huai_ifs"]["preflight_ready"] is True


RETURNED_FAILURES: list[tuple[str, str]] = [
    ("blocked", "returned status 'blocked'"),
    # The audit row could not be written: the store rolls the mutation back and returns, it does not raise.
    ("audit_failure", "LIFECYCLE_AUDIT_PERSISTENCE_FAILED"),
    ("raise", "failed (RuntimeError: Model registry database operation failed"),
]


@pytest.mark.parametrize(("outcome", "said"), RETURNED_FAILURES)
def test_a_failure_on_the_second_row_names_the_first_as_done_and_the_rerun_takes_the_rest(
    space: Space,  # noqa: F811
    outcome: str,
    said: str,
) -> None:
    space.store.operation_outcomes["dg_huai_gfs"] = outcome

    failure = _failed_in(space, "deactivate")

    assert said in failure["reason"]
    assert "Rows deactivated before it: ['basins_huai_shud']" in failure["reason"]
    assert failure["rows_done"] == ["basins_huai_shud"]
    statuses = {row["model_id"]: row.get("status") for row in failure["model_rows"]}
    assert statuses["basins_huai_shud"] == "allowed" and statuses["dg_huai_ifs"] is None
    assert statuses["dg_huai_gfs"] == ("raised RuntimeError" if outcome == "raise" else "blocked")
    # It stopped at that row: the third was not attempted.
    assert [call["model_id"] for call in space.store.operations] == ["basins_huai_shud", "dg_huai_gfs"]
    assert space.database.active(HUAI) == ["dg_huai_gfs", "dg_huai_ifs"]

    space.store.operation_outcomes.clear()
    space.store.preflights.clear()
    space.store.operations.clear()
    status, report = space.run()

    assert status == 0, space.last_stderr
    assert report["steps"] == {"exclude": "skipped", "supersede": "skipped", "deactivate": "completed",
                               "verify": "completed"}  # fmt: skip
    # The rerun acts on the rows still active only.
    remaining = ["dg_huai_gfs", "dg_huai_ifs"]
    assert [call["model_id"] for call in space.store.preflights] == remaining
    assert [call["model_id"] for call in space.store.operations] == remaining
    assert space.receipt("deactivate")["rows_done"] == remaining
    assert space.database.active(HUAI) == []


def test_a_row_that_is_already_inactive_at_the_operation_counts_as_done(space: Space) -> None:  # noqa: F811
    space.store.operation_outcomes["dg_huai_gfs"] = "already_current"

    status, _report = space.run()

    assert status == 0, space.last_stderr
    rows = space.receipt("deactivate")["model_rows"]
    assert [(row["model_id"], row["status"]) for row in rows] == [
        ("basins_huai_shud", "allowed"),
        ("dg_huai_gfs", "already_current"),
        ("dg_huai_ifs", "allowed"),
    ]
    assert space.receipt("deactivate")["rows_done"] == ACTIVE
    assert space.database.active(HUAI) == []


def _a_row_is_active_again(space: Space) -> None:  # noqa: F811
    space.database.models["dg_huai_gfs"]["active_flag"] = True


def _a_run_is_a_candidate_again(space: Space) -> None:  # noqa: F811
    space.database.runs["huai-03"]["status"] = "published"


def _the_key_left_the_env_file(space: Space) -> None:  # noqa: F811
    space.write_env(space.env_text(excluded="zhaochen_hhy,hhe"))


REVERTS: list[tuple[Callable[[Space], None], str]] = [
    (_a_row_is_active_again, f"core.model_instance rows of {HUAI} are active again: ['dg_huai_gfs']"),
    (_a_run_is_a_candidate_again, f"1 runs of {HUAI} are back in succeeded / parsed / published"),
    (_the_key_left_the_env_file, "the key 'huai' is gone from AUTOPIPE_EXCLUDE_BASINS"),
]


@pytest.mark.parametrize(("revert", "said"), REVERTS)
def test_verify_fails_naming_what_was_reverted(space: Space, revert: Callable[[Space], None], said: str) -> None:  # noqa: F811
    # The first three steps complete; verify cannot read the unit yet.
    space.set_answers(round_answer(), {"raw": "Failed to connect to bus\n"})
    _failed_in(space, "verify")
    revert(space)
    space.set_answers(round_answer())
    database_before = space.database.snapshot()
    env_before = tree(space.env_file.parent, without=(space.retire_lock,))
    commits_before = space.database.commits

    failure = _failed_in(space, "verify")

    assert "The retirement was reverted" in failure["reason"] and said in failure["reason"]
    # Why that happens, and what to do first.
    assert "an autopipe round that had read the exclusion list before the key was in it" in failure["reason"]
    assert "an ingest forced by hand" in failure["reason"] and "the exclusion entry first" in failure["reason"]
    assert failure["autopipe_round"]["exit_status"] == 0
    assert (
        bool(failure["active_model_ids"]),
        bool(failure["candidate_run_count"]),
        not failure["key_in_exclusion_list"],
    ) == tuple(revert is candidate for candidate, _said in REVERTS)
    # verify writes nothing but its failure receipt.
    assert space.database.snapshot() == database_before and space.database.commits == commits_before
    assert tree(space.env_file.parent, without=(space.retire_lock,)) == env_before
    assert not (space.directory() / "retire-verify.json").exists()


def test_verify_completes_once_the_cause_is_put_right(space: Space) -> None:  # noqa: F811
    space.set_answers(round_answer(), {"raw": "Failed to connect to bus\n"})
    _failed_in(space, "verify")
    _the_key_left_the_env_file(space)
    space.set_answers(round_answer())
    _failed_in(space, "verify")

    space.write_env(space.env_text(excluded="zhaochen_hhy,hhe,huai"))
    status, report = space.run()

    assert status == 0, space.last_stderr
    assert report["steps"] == {"exclude": "skipped", "supersede": "skipped", "deactivate": "skipped",
                               "verify": "completed"}  # fmt: skip
    assert len(space.failures()) == 2


def _supersede_left_only_its_backup(space: Space) -> Path:  # noqa: F811
    """After a supersede that reached its commit call and did not finish: the backup is there, the receipt is not."""

    backup = space.directory() / "hydro-run-backup.csv"
    assert backup.exists(), "the backup was removed although the commit call had been reached"
    assert backup.read_text(encoding="utf-8").count("model-of-huai-0") == 4
    assert space.receipts_present() == ["exclude"]
    assert space.store.preflights == [] and space.store.operations == []
    return backup


def test_returned_ids_that_are_not_the_backups_roll_back_although_the_counts_are_equal(space: Space) -> None:  # noqa: F811
    def another_run_took_its_place() -> None:
        # Between the two statements one backed-up run left the candidate statuses and another entered them.
        space.database.runs["huai-04"]["status"] = "failed"
        space.database.add_run("huai-99", HUAI, "published")

    space.database.before_update = another_run_took_its_place

    failure = _failed_in(space, "supersede")

    assert "The rows the update changed are not the rows of the backup" in failure["reason"]
    assert "the update returned 4 rows and the backup holds 4" in failure["reason"]
    assert "in the backup only: ['huai-04'] (1), updated only: ['huai-99'] (1)" in failure["reason"]
    # Rolled back, no receipt, and the backup this run created is gone.
    assert space.database.statuses(HUAI)["huai-99"] == "published"
    assert space.database.statuses(HUAI)["huai-01"] == "succeeded"
    assert not (space.directory() / "hydro-run-backup.csv").exists()
    assert space.database.commits == 0


def test_a_database_error_on_the_update_rolls_back_and_removes_the_backup(space: Space) -> None:  # noqa: F811
    # An ingest holds a row lock for longer than the lock timeout.
    space.database.update_error = psycopg2.errors.LockNotAvailable("canceling statement due to lock timeout")

    failure = _failed_in(space, "supersede")

    assert "The database refused: LockNotAvailable: canceling statement due to lock timeout" in failure["reason"]
    assert _candidate_statuses(space) == CANDIDATE_RUNS
    assert not (space.directory() / "hydro-run-backup.csv").exists()
    (writer,) = [connection for connection in space.database.connections if not connection.readonly]
    assert (writer.commits, writer.closed) == (0, True) and writer.rollbacks >= 1
    assert space.store.preflights == [] and space.store.operations == []

    space.database.update_error = None
    assert space.run()[0] == 0, space.last_stderr


@pytest.mark.parametrize("landed", [False, True])
def test_a_commit_call_that_raises_keeps_the_backup_and_the_rerun_decides_from_the_table(
    space: Space,  # noqa: F811
    landed: bool,
) -> None:
    space.database.commit_error = psycopg2.OperationalError("server closed the connection unexpectedly")
    space.database.commit_lands_before_error = landed

    failure = _failed_in(space, "supersede")

    # The failure names the unknown outcome; nothing concluded "not committed".
    assert "The COMMIT of the supersede transaction did not return (OperationalError" in failure["reason"]
    assert "whether the runs were superseded is not known" in failure["reason"]
    backup = _supersede_left_only_its_backup(space)
    backup_before = (backup.stat().st_ino, backup.read_bytes())

    space.database.commit_error = None
    status, _report = space.run()

    # The "backup present" branch, either way: no second backup is taken.
    assert (backup.stat().st_ino, backup.read_bytes()) == backup_before
    if landed:
        assert status == 0, space.last_stderr
        assert space.receipt("supersede")["recovered_from_existing_backup"] is True
        assert space.receipt("supersede")["row_count"] == 4
    else:
        assert status == 1
        assert any("of an earlier attempt exists, and 4 runs" in failure["reason"] for failure in space.failures())
        assert _candidate_statuses(space) == CANDIDATE_RUNS


def _closing_the_backup_fails(space: Space) -> None:  # noqa: F811
    # The descriptor is gone when the tool closes the file after its commit returned: a real EBADF.
    space.database.after_commit = lambda: os.close(space.database.last_copy_handle.fileno())


def _the_receipt_cannot_be_written(space: Space) -> None:  # noqa: F811
    real_write = succession_receipt.write_receipt

    def full_disk(path: Path, receipt: Any) -> None:
        if path.name == "retire-supersede.json":
            raise OSError(errno.ENOSPC, "No space left on device", str(path))
        real_write(path, receipt)

    space.monkeypatch.setattr(succession_receipt, "write_receipt", full_disk)


@pytest.mark.parametrize(
    ("arrange", "said"),
    [
        (_closing_the_backup_fails, "The supersede step failed after its commit call (OSError"),
        (_the_receipt_cannot_be_written, "No space left on device"),
    ],
)
def test_an_error_after_the_commit_returned_keeps_the_backup(
    space: Space,  # noqa: F811
    arrange: Callable[[Space], None],
    said: str,
) -> None:
    arrange(space)

    failure = _failed_in(space, "supersede")

    assert said in failure["reason"]
    # Committed: the runs are superseded, so the backup is the only record of what they were.
    assert set(_candidate_statuses(space).values()) == {"superseded"}
    backup = _supersede_left_only_its_backup(space)
    backup_before = (backup.stat().st_ino, backup.read_bytes())

    # The rerun, with the same fault still there for the receipt case, never takes a second backup.
    space.database.after_commit = None
    status, _report = space.run()
    assert (backup.stat().st_ino, backup.read_bytes()) == backup_before
    if arrange is _closing_the_backup_fails:
        assert status == 0, space.last_stderr
        assert space.receipt("supersede")["recovered_from_existing_backup"] is True
    else:
        assert status == 1 and "No space left on device" in space.last_stderr


def test_an_interrupt_after_the_commit_returned_keeps_the_backup_and_the_rerun_writes_the_receipt(
    space: Space,  # noqa: F811
) -> None:
    def interrupted() -> None:
        raise KeyboardInterrupt

    space.database.on_writer_close = interrupted

    with pytest.raises(KeyboardInterrupt):
        space.run()

    assert set(_candidate_statuses(space).values()) == {"superseded"}
    backup = _supersede_left_only_its_backup(space)
    backup_before = (backup.stat().st_ino, backup.read_bytes())

    space.database.on_writer_close = None
    status, report = space.run()

    assert status == 0, space.last_stderr
    assert report["steps"]["supersede"] == "completed"
    assert space.receipt("supersede")["recovered_from_existing_backup"] is True
    assert (backup.stat().st_ino, backup.read_bytes()) == backup_before


def test_an_interrupt_before_the_commit_call_removes_the_backup(space: Space) -> None:  # noqa: F811
    def interrupted() -> None:
        raise KeyboardInterrupt

    space.database.before_update = interrupted

    with pytest.raises(KeyboardInterrupt):
        space.run()

    assert _candidate_statuses(space) == CANDIDATE_RUNS and space.database.commits == 0
    assert not (space.directory() / "hydro-run-backup.csv").exists()


def test_a_preflight_that_raises_fails_the_step_with_no_row_changed(space: Space) -> None:  # noqa: F811
    space.store.preflight_raises["dg_huai_gfs"] = RuntimeError("model_id not found: dg_huai_gfs")

    failure = _failed_in(space, "deactivate")

    assert "The deactivate preflight of dg_huai_gfs failed (RuntimeError: model_id not found" in failure["reason"]
    assert "No row was changed." in failure["reason"]
    assert space.store.operations == [] and space.database.audit_log == []
    assert space.database.active(HUAI) == ACTIVE


def test_a_database_that_cannot_be_read_in_a_step_fails_the_step(space: Space) -> None:  # noqa: F811
    # The connection is lost right after the supersede commit: deactivate cannot read the active rows.
    def lost() -> None:
        space.database.read_error = psycopg2.OperationalError("could not connect to server")

    space.database.after_commit = lost

    failure = _failed_in(space, "deactivate")

    assert "The database refused: OperationalError: could not connect to server" in failure["reason"]
    assert space.store.preflights == [] and space.store.operations == []
    assert space.database.active(HUAI) == ACTIVE


def test_verify_times_out_when_no_round_ends_and_the_rerun_does_not_repeat_the_exclude_wait(space: Space) -> None:  # noqa: F811
    # exclude's round ends; the round verify follows is still running on every later poll.
    space.set_answers(round_answer(), running_answer(mark="slow"), running_answer(start="@slow"))
    space.wait_seconds = 1.5

    failure = _failed_in(space, "verify")

    assert "No autopipe round that started after the reference ended within" in failure["reason"]
    assert "it was following the round that started at" in failure["reason"]
    assert space.database.active(HUAI) == [] and not (space.directory() / "retire-verify.json").exists()
    exclude_before = (space.directory() / "retire-exclude.json").read_bytes()
    calls = len(space.systemctl_calls())

    space.set_answers(round_answer())
    status, report = space.run()

    assert status == 0, space.last_stderr
    assert report["steps"]["exclude"] == "skipped" and report["steps"]["verify"] == "completed"
    # One reading: verify's own wait. The exclude wait is not repeated.
    assert len(space.systemctl_calls()) == calls + 1
    assert (space.directory() / "retire-exclude.json").read_bytes() == exclude_before
