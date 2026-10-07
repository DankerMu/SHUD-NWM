"""The dry-run of the node-27 basin retirement tool: everything is run that can be, nothing is kept (#2757).

Partition of the ``scripts/node27_retire_basin.py`` suite; the fakes and the
``space`` fixture are in ``tests/node27_retire_basin_helpers.py``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from scripts.basin_retirement import autopipe
from tests.node27_retire_basin_helpers import (
    COPY_PREFIX,
    HUAI,
    STEPS,
    SUPERSEDE_SQL,
    WEI,
    Space,
    manifest_row,
    round_answer,
    running_answer,
    space,  # noqa: F401 - the fixture
)

ACTIVE = ["basins_huai_shud", "dg_huai_gfs", "dg_huai_ifs"]


def _dry_run(space: Space) -> tuple[int, dict[str, Any]]:  # noqa: F811
    """A dry-run that must leave the whole tree (the lock file aside) and the committed database as they were."""

    before = space.everything()
    status, report = space.run(apply=False)
    assert report is not None and report["dry_run"] is True, space.last_stderr
    assert space.everything() == before
    # No write transaction was committed on any connection, and none was left open.
    assert space.database.commits == 0
    assert all(connection.closed and connection.autocommit is False for connection in space.database.connections)
    # Preflights at most: the lifecycle operation is never called.
    assert space.store.operations == [] and space.database.audit_log == []
    # The temporary CSV of the supersede dry-run is under TMPDIR and gone.
    assert list((space.root / "tmp").iterdir()) == []
    assert "DRY-RUN (no --apply)" in space.last_stderr
    return status, report


def _never_sleep(_seconds: float) -> None:
    raise AssertionError("a dry-run must not wait for an autopipe round")


def test_a_dry_run_runs_the_real_statements_and_keeps_nothing(space: Space) -> None:  # noqa: F811
    space.monkeypatch.setattr(autopipe, "sleep", _never_sleep)
    # A round is in flight: an apply would wait, the dry-run only says what it read.
    space.set_answers(running_answer())

    status, report = _dry_run(space)

    assert status == 0 and report["would_be_refused"] == []
    # The four steps, none of them reported as missing the receipt of the one before.
    assert sorted(report["steps"]) == sorted(STEPS)
    assert (report["basin_id"], report["basin_key"]) == ("basins_huai", "huai")
    assert not space.directory().exists() and not space.env_backup().exists()
    assert space.excluded() == "zhaochen_hhy,hhe"

    # The unit was read once and not waited for.
    assert len(space.systemctl_calls()) == 1
    assert report["autopipe_unit_now"]["active_state"] == "activating"
    assert report["autopipe_unit_now"]["exit_monotonic_us"] == 0

    exclude = report["steps"]["exclude"]
    assert exclude["env_file_checks"] == "passed" and exclude["key_in_exclusion_list"] is False
    assert exclude["autopipe_lock_path"] == str(space.autopipe_lock)
    assert "append the key" in exclude["would"]

    # supersede: the same statements as an apply, on a writable connection that was rolled back.
    supersede = report["steps"]["supersede"]
    assert supersede["statements"] == "executed and rolled back"
    assert (supersede["row_count"], supersede["updated_row_count"]) == (4, 4)
    assert supersede["status_counts"] == {"succeeded": 1, "parsed": 1, "published": 2}
    (writer,) = [connection for connection in space.database.connections if not connection.readonly]
    assert writer.statements[0].startswith(COPY_PREFIX) and writer.statements[1] == SUPERSEDE_SQL
    assert len(writer.statements) == 3 and (writer.commits, writer.closed) == (0, True) and writer.rollbacks >= 1
    assert space.database.statuses(HUAI)["huai-03"] == "published"

    # deactivate: the preflight of every active row, and its result.
    assert [call["model_id"] for call in space.store.preflights] == ACTIVE
    rows = report["steps"]["deactivate"]["active_model_rows"]
    assert [(row["model_id"], row["preflight_status"], row["preflight_blockers"]) for row in rows] == [
        (model_id, "ready", []) for model_id in ACTIVE
    ]
    assert all(row["preflight_warnings"][0]["code"] == "COPIED_ROOT_EVIDENCE_MISSING" for row in rows)
    assert space.database.active(HUAI) == ACTIVE

    assert "wait up to" in report["steps"]["verify"]["would"]
    assert len(report["not_done_by_this_tool"]) == 3


def test_a_dry_run_lists_every_failed_precondition_and_goes_on_with_the_read_only_checks(space: Space) -> None:  # noqa: F811
    (space.succession / "step-finish.json").unlink()
    space.write_manifest(
        [manifest_row("dg_huai_gfs", "basins_huai", HUAI), manifest_row("dg_wei_gfs", "basins_wei", WEI)]
    )

    status, report = _dry_run(space)

    assert status == 1
    refused = report["would_be_refused"]
    assert len(refused) == 2 and all(entry.startswith("precondition: ") for entry in refused)
    assert "has not finished on node-22" in refused[0]
    assert f"still holds rows of basin version {HUAI}: ['dg_huai_gfs']" in refused[1]
    assert report["preconditions"]["manifest"]["rows_of_this_basin_version"] == ["dg_huai_gfs"]
    assert report["preconditions"]["model_row_count"] == 4

    # The read-only checks still ran: the env file, the unit, the preflights.
    assert report["steps"]["exclude"]["env_file_checks"] == "passed"
    assert len(space.systemctl_calls()) == 1 and report["autopipe_unit_now"]["exit_status"] == 0
    assert [call["model_id"] for call in space.store.preflights] == ACTIVE
    # The statements that write were not sent: an apply would be refused before any of them.
    assert report["steps"]["supersede"]["status"] == "not run"
    assert all(connection.readonly for connection in space.database.connections)
    assert SUPERSEDE_SQL not in space.database.statements
    assert not any(statement.startswith("COPY") for statement in space.database.statements)


def test_a_dry_run_for_a_basin_version_that_does_not_exist_still_checks_the_rest(space: Space) -> None:  # noqa: F811
    before = space.everything()

    status, report = space.run("basins_huai_v9", apply=False)

    assert status == 1 and space.everything() == before
    (refused,) = report["would_be_refused"]
    assert "'basins_huai_v9' is not a row of core.basin_version" in refused
    assert report["basin_id"] is None
    assert report["steps"]["exclude"]["env_file_checks"] == "passed"
    assert "not predicted" in report["steps"]["exclude"]["status"]
    assert report["steps"]["deactivate"]["active_model_rows"] == []
    assert len(space.systemctl_calls()) == 1


def test_a_dry_run_reports_an_env_file_and_a_unit_an_apply_would_fail_on(space: Space) -> None:  # noqa: F811
    os.chmod(space.env_file, 0o644)
    space.set_answers({"raw": "Failed to connect to bus\n"})

    status, report = _dry_run(space)

    assert status == 1
    refused = report["would_be_refused"]
    assert any(entry.startswith("autopipe unit: ") and "does not know" in entry for entry in refused)
    assert any(entry.startswith("exclude: ") and "has mode 0644" in entry for entry in refused)
    assert len(refused) == 2
    assert report["autopipe_unit_now"].startswith("unknown (")
    assert report["steps"]["exclude"]["status"] == "would fail"
    # The other steps were still previewed.
    assert report["steps"]["supersede"]["row_count"] == 4
    assert len(report["steps"]["deactivate"]["active_model_rows"]) == 3


def test_a_dry_run_reports_a_preflight_blocker(space: Space) -> None:  # noqa: F811
    space.store.preflight_blockers["dg_huai_ifs"] = [{"code": "MISSING_ACTIVE_RISK", "message": "would be left"}]

    status, report = _dry_run(space)

    assert status == 1
    (refused,) = report["would_be_refused"]
    assert refused.startswith("deactivate: the preflight of dg_huai_ifs is 'blocked'")
    assert "MISSING_ACTIVE_RISK" in refused
    assert [call["model_id"] for call in space.store.preflights] == ACTIVE


def test_a_dry_run_of_a_count_mismatch_reports_it_and_keeps_nothing(space: Space) -> None:  # noqa: F811
    added: list[str] = []

    def register_once() -> None:
        if not added:
            added.append("huai-99")
            space.database.add_run("huai-99", HUAI, "published")

    space.database.add_run("huai-98", HUAI, "failed")
    space.database.before_update = register_once
    before_tree = space.everything()["tree"]

    status, report = space.run(apply=False)

    assert status == 1
    (refused,) = report["would_be_refused"]
    assert refused.startswith("supersede: The update changed 5 rows") and "the backup holds 4" in refused
    assert space.everything()["tree"] == before_tree
    assert space.database.commits == 0 and list((space.root / "tmp").iterdir()) == []


def test_a_dry_run_after_a_partial_apply_shows_what_is_done_and_what_is_left(space: Space) -> None:  # noqa: F811
    space.store.operation_outcomes["dg_huai_gfs"] = "blocked"
    assert space.run()[0] == 1
    space.store.operation_outcomes.clear()
    space.store.preflights.clear()
    space.store.operations.clear()
    commits_after_apply = space.database.commits
    before = space.everything()

    status, report = space.run(apply=False)

    assert status == 0 and report["would_be_refused"] == []
    assert space.everything() == before and space.database.commits == commits_after_apply
    assert report["steps"]["exclude"]["status"] == "completed"
    assert report["steps"]["supersede"]["status"] == "completed"
    assert report["steps"]["supersede"]["receipt"] == str(space.directory() / "retire-supersede.json")
    rows = report["steps"]["deactivate"]["active_model_rows"]
    assert [row["model_id"] for row in rows] == ["dg_huai_gfs", "dg_huai_ifs"]
    assert space.store.operations == []


@pytest.mark.parametrize(("candidates_left", "expected_status"), [(True, 1), (False, 0)])
def test_a_dry_run_with_the_run_backup_already_there_says_what_an_apply_would_do(
    space: Space,  # noqa: F811
    candidates_left: bool,
    expected_status: int,
) -> None:
    space.directory().mkdir(parents=True)
    backup = space.directory() / "hydro-run-backup.csv"
    backup.write_text("run_id,status\nhuai-01,succeeded\n", encoding="utf-8")
    if not candidates_left:
        for run in space.database.runs.values():
            if run["basin_version_id"] == HUAI and run["status"] in ("succeeded", "parsed", "published"):
                run["status"] = "superseded"

    status, report = _dry_run(space)

    assert status == expected_status
    supersede = report["steps"]["supersede"]
    assert supersede["run_backup_exists"] is True and supersede["candidate_run_count"] == (4 if candidates_left else 0)
    if candidates_left:
        (refused,) = report["would_be_refused"]
        assert refused.startswith("supersede: the run backup") and "4 runs are in" in refused
    else:
        assert supersede["would"] == "write the receipt from the existing backup and change nothing"
    # Neither case sends the backup or the update again.
    assert SUPERSEDE_SQL not in space.database.statements
    assert all(connection.readonly for connection in space.database.connections)


def test_a_dry_run_of_a_completed_retirement_reports_four_completed_steps(space: Space) -> None:  # noqa: F811
    assert space.run()[0] == 0, space.last_stderr
    space.set_answers(round_answer())
    calls = len(space.systemctl_calls())
    statements = len(space.database.statements)
    before = space.everything()

    status, report = space.run(apply=False)

    assert status == 0 and report["would_be_refused"] == []
    assert space.everything() == before
    assert {step: report["steps"][step]["status"] for step in STEPS} == {step: "completed" for step in STEPS}
    assert len(space.systemctl_calls()) == calls + 1
    # Only the precondition reads.
    later = space.database.statements[statements:]
    assert later and all(statement.startswith("SELECT") for statement in later)
