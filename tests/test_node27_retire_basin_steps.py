"""The supersede, deactivate and verify steps of the node-27 basin retirement tool: failures and reruns (#2757).

Partition of the ``scripts/node27_retire_basin.py`` suite; the fakes and the
``space`` fixture are in ``tests/node27_retire_basin_helpers.py``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.node27_retire_basin_helpers import (
    HUAI,
    STEPS,
    SUPERSEDE_SQL,
    Space,
    round_answer,
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

    assert "The update changed 5 rows of hydro.hydro_run but the backup holds 4" in failure["reason"]
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
