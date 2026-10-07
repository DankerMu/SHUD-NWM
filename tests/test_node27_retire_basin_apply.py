"""A full basin retirement on node-27: four receipts, one env edit, one run backup, rows deactivated one by one (#2757).

Partition of the ``scripts/node27_retire_basin.py`` suite; the fakes and the
``space`` fixture are in ``tests/node27_retire_basin_helpers.py``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import stat

from tests.node27_retire_basin_helpers import (
    CANDIDATES,
    COPY_PREFIX,
    COPY_SUFFIX,
    DATABASE_URL,
    HUAI,
    LOCK_TIMEOUT_SQL,
    STEPS,
    SUCCESSION_ID,
    SUPERSEDE_SQL,
    TAO,
    UNIT,
    WEI,
    Space,
    space,  # noqa: F401 - the fixture
)


def _backup_rows(space: Space, basin_version_id: str = HUAI) -> list[dict[str, str]]:  # noqa: F811
    text = (space.directory(basin_version_id) / "hydro-run-backup.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def test_full_apply_retires_the_basin_version_and_nothing_else(space: Space) -> None:  # noqa: F811
    env_before = space.env_file.read_bytes()
    runs_before = space.database.snapshot()["runs"]

    status, report = space.run()

    assert status == 0, space.last_stderr
    assert report["outcome"] == "completed"
    assert report["steps"] == {step: "completed" for step in STEPS}
    assert (report["basin_id"], report["basin_key"]) == ("basins_huai", "huai")

    # The four receipts, under retire-<basin-version-id>/ of the succession, written in order.
    directory = space.succession / f"retire-{HUAI}"
    assert report["receipt_directory"] == str(directory)
    receipts = {step: space.receipt(step) for step in STEPS}
    assert [receipts[step]["step"] for step in STEPS] == list(STEPS)
    stamps = [receipts[step]["generated_at"] for step in STEPS]
    assert stamps == sorted(stamps)
    for receipt in receipts.values():
        assert receipt["schema_version"] == "nhms.basin_retirement.step_receipt.v1"
        assert receipt["outcome"] == "completed"
        assert (receipt["succession_id"], receipt["basin_version_id"]) == (SUCCESSION_ID, HUAI)
        assert (receipt["basin_id"], receipt["operator_id"]) == ("basins_huai", "danker")
        assert receipt["reason"] == "owner decision: the basin leaves production"
    assert space.failures() == []

    # The env file gains the key at the end of its one line, keeps mode 0600 and every other byte.
    env_after = space.env_file.read_bytes()
    assert env_after == env_before.replace(
        b"AUTOPIPE_EXCLUDE_BASINS=zhaochen_hhy,hhe\n", b"AUTOPIPE_EXCLUDE_BASINS=zhaochen_hhy,hhe,huai\n"
    )
    assert env_after != env_before
    assert stat.S_IMODE(space.env_file.stat().st_mode) == 0o600
    backup = space.env_backup()
    assert backup.read_bytes() == env_before and stat.S_IMODE(backup.stat().st_mode) == 0o600
    exclude = receipts["exclude"]
    assert exclude["basin_key"] == "huai" and exclude["file_changed"] is True
    assert exclude["env_backup"] == str(backup) and exclude["env_backup_outcome"] == "written"
    assert exclude["sha256_before"] == hashlib.sha256(env_before).hexdigest()
    assert exclude["sha256_after"] == hashlib.sha256(env_after).hexdigest()
    waited = exclude["autopipe_round"]
    assert (waited["exit_code"], waited["exit_status"], waited["active_state"]) == (1, 0, "inactive")
    assert (waited["ended"], waited["lock_probe"], waited["load_state"]) == ("seen_at_rest", "free", "loaded")
    assert waited["start_monotonic_us"] > waited["reference_monotonic_us"]
    assert waited["exit_monotonic_us"] >= waited["start_monotonic_us"]
    assert waited["lock_path_probed"] == str(space.autopipe_lock) and waited["lock_held"] is False
    # No temporary file is left beside the env file.
    assert sorted(path.name for path in space.env_file.parent.iterdir()) == [
        "node27-ingest.env",
        f"node27-ingest.env.bak-{SUCCESSION_ID}-{HUAI}",
        "node27-ingest.env.retire-lock",
    ]

    # Runs of the three statuses are superseded; updated_at and every other column are untouched.
    runs_after = space.database.runs
    assert space.database.statuses(HUAI) == {
        "huai-01": "superseded",
        "huai-02": "superseded",
        "huai-03": "superseded",
        "huai-04": "superseded",
        "huai-05": "failed",
        "huai-06": "superseded",
        "huai-07": "running",
    }
    for run_id, before in runs_before.items():
        assert {**runs_after[run_id], "status": before["status"]} == before, run_id
    for run_id, before in runs_before.items():
        if before["basin_version_id"] in (TAO, WEI):
            assert runs_after[run_id] == before, run_id

    # The run backup holds exactly the rows that were changed, as they were.
    backed_up = _backup_rows(space)
    assert [row["run_id"] for row in backed_up] == ["huai-01", "huai-02", "huai-03", "huai-04"]
    assert [row["status"] for row in backed_up] == ["succeeded", "parsed", "published", "published"]
    assert [row["updated_at"] for row in backed_up] == [runs_before[row["run_id"]]["updated_at"] for row in backed_up]
    supersede = receipts["supersede"]
    assert (supersede["row_count"], supersede["updated_row_count"]) == (4, 4)
    assert supersede["status_counts"] == {"succeeded": 1, "parsed": 1, "published": 2}
    assert supersede["recovered_from_existing_backup"] is False
    csv_path = directory / "hydro-run-backup.csv"
    assert supersede["run_backup"] == str(csv_path)
    assert supersede["run_backup_sha256"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()

    # Every active row of the basin version, baseline and dg_* alike, one by one through the store.
    store = space.store
    active_before = ["basins_huai_shud", "dg_huai_gfs", "dg_huai_ifs"]
    assert [call["model_id"] for call in store.preflights] == active_before
    assert [call["model_id"] for call in store.operations] == active_before
    assert "dg_huai_old" not in {call["model_id"] for call in (*store.preflights, *store.operations)}
    for call in (*store.preflights, *store.operations):
        decision = call["policy_decision"]
        assert (decision.actor_id, decision.roles, decision.auth_mode) == (
            "ops:danker",
            ("sys_admin",),
            "trusted_internal",
        )
        assert call["operation"] == "deactivate" and call["override_missing_active"] is True
        assert call["reason"] == "owner decision: the basin leaves production"
    assert space.database.active(HUAI) == []
    assert space.database.active(TAO) == ["basins_tao_shud", "dg_tao_gfs"]
    assert space.database.active(WEI) == ["basins_wei_shud", "dg_wei_gfs"]
    rows = receipts["deactivate"]["model_rows"]
    assert [(row["model_id"], row["status"]) for row in rows] == [(model_id, "allowed") for model_id in active_before]
    assert all(row["preflight_warnings"][0]["code"] == "COPIED_ROOT_EVIDENCE_MISSING" for row in rows)
    assert receipts["deactivate"]["rows_done"] == active_before

    # verify: the counts and its own round, which is not the one exclude waited for.
    verify = receipts["verify"]
    assert (verify["active_model_row_count"], verify["candidate_run_count"]) == (0, 0)
    assert verify["key_in_exclusion_list"] is True
    assert verify["autopipe_round"]["start_monotonic_us"] > waited["exit_monotonic_us"]

    # core.basin and core.basin_version are never written: the one write statement is the status update.
    statements = space.database.statements
    writes = [statement for statement in statements if not statement.lstrip().upper().startswith(("SELECT", "COPY"))]
    # The lock timeout is the first statement of the supersede transaction.
    assert writes == [LOCK_TIMEOUT_SQL, SUPERSEDE_SQL]
    (writer,) = [connection for connection in space.database.connections if not connection.readonly]
    assert writer.statements[0] == LOCK_TIMEOUT_SQL and writer.statements[2] == SUPERSEDE_SQL
    assert not any("core.basin " in statement or "core.basin\n" in statement for statement in statements)
    assert [s for s in statements if "core.basin_version" in s] == [
        "SELECT basin_id FROM core.basin_version WHERE basin_version_id = %s"
    ]
    assert space.database.commits == 1

    # The unit was only ever shown: nothing started, stopped, enabled or disabled.
    show = f"--user show {UNIT} -p LoadState,ActiveState,ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic"
    calls = space.systemctl_calls()
    assert len(calls) == 2 and all(call == f"{show},ExecMainCode,ExecMainStatus" for call in calls)

    # What the tool does not do is said at the end.
    assert len(report["not_done_by_this_tool"]) == 3
    for word in ("Basins directory", "static geojson", "must stay"):
        assert word in space.last_stderr


def test_the_copy_statement_holds_the_basin_version_as_a_rendered_literal(space: Space) -> None:  # noqa: F811
    status, _report = space.run()

    assert status == 0, space.last_stderr
    (copy_statement,) = [sql for connection in space.database.connections for sql in connection.copies]
    assert copy_statement == f"{COPY_PREFIX}'{HUAI}'{COPY_SUFFIX}"
    assert "%s" not in copy_statement and "%(" not in copy_statement
    # The update is the bound one: the placeholder stays in the statement, the value goes beside it.
    assert SUPERSEDE_SQL.count("%s") == 1 and SUPERSEDE_SQL in space.database.statements
    for status_name in CANDIDATES:
        assert f"'{status_name}'" in copy_statement


def test_every_connection_is_attributed_and_never_autocommits(space: Space) -> None:  # noqa: F811
    status, report = space.run()

    assert status == 0, space.last_stderr
    connections = space.database.connections
    assert connections and all(connection.dsn == DATABASE_URL for connection in connections)
    assert all(connection.kwargs == {"fallback_application_name": "nhms-retire-basin"} for connection in connections)
    assert all(connection.closed and connection.autocommit is False for connection in connections)
    # Only the supersede transaction is writable; every other connection is a read-only session.
    writable = [connection for connection in connections if not connection.readonly]
    assert len(writable) == 1 and writable[0].commits == 1
    assert all(connection.commits == 0 for connection in connections if connection.readonly)
    assert space.store.created == [((DATABASE_URL,), {"application_name": "nhms-retire-basin"})]
    # The database URL reaches no receipt, no report and no message.
    written = "".join(path.read_text(encoding="utf-8") for path in space.directory().iterdir())
    assert "s3cret-pw" not in written + str(report) + space.last_stderr


def test_two_basin_versions_of_one_succession_are_retired_apart(space: Space) -> None:  # noqa: F811
    first_status, _ = space.run(HUAI)
    huai_tree = {path.name: path.read_bytes() for path in space.directory(HUAI).iterdir()}
    operations_before = len(space.store.operations)

    second_status, second = space.run(TAO)

    assert (first_status, second_status) == (0, 0), space.last_stderr
    # The second run is not skipped: its own four steps ran.
    assert second["steps"] == {step: "completed" for step in STEPS}
    assert space.directory(TAO) != space.directory(HUAI)
    assert space.receipts_present(TAO) == list(STEPS)
    assert [call["model_id"] for call in space.store.operations[operations_before:]] == [
        "basins_tao_shud",
        "dg_tao_gfs",
    ]
    # Its own run backup and env backup; the first basin's files are untouched.
    assert [row["run_id"] for row in _backup_rows(space, TAO)] == ["tao-01", "tao-02"]
    assert [row["run_id"] for row in _backup_rows(space, HUAI)] == ["huai-01", "huai-02", "huai-03", "huai-04"]
    assert {path.name: path.read_bytes() for path in space.directory(HUAI).iterdir()} == huai_tree
    assert space.env_backup(TAO).exists() and space.env_backup(HUAI).exists()
    # The second backup is the env file as the first retirement left it.
    assert b"AUTOPIPE_EXCLUDE_BASINS=zhaochen_hhy,hhe,huai\n" in space.env_backup(TAO).read_bytes()
    assert space.excluded() == "zhaochen_hhy,hhe,huai,tao"
    assert space.database.active(HUAI) == [] and space.database.active(TAO) == []
    assert space.database.active(WEI) == ["basins_wei_shud", "dg_wei_gfs"]
    assert space.database.statuses(WEI) == {"wei-01": "published", "wei-02": "succeeded"}


def test_a_rerun_of_a_completed_retirement_skips_every_step(space: Space) -> None:  # noqa: F811
    assert space.run()[0] == 0
    before = space.everything()
    calls = len(space.systemctl_calls())
    lifecycle_calls = len(space.store.preflights) + len(space.store.operations)

    status, report = space.run()

    assert status == 0 and report["steps"] == {step: "skipped" for step in STEPS}
    assert space.everything() == before
    assert len(space.systemctl_calls()) == calls
    assert len(space.store.preflights) + len(space.store.operations) == lifecycle_calls


def test_the_receipt_directory_is_usable_by_the_group_and_the_run_backup_is_not_world_readable(
    space: Space,  # noqa: F811
) -> None:
    previous = os.umask(0o077)
    try:
        status, _report = space.run()
    finally:
        os.umask(previous)

    assert status == 0, space.last_stderr
    # As every succession directory: the other node writes and reads here as another user of the group.
    assert stat.S_IMODE(space.directory().stat().st_mode) & 0o075 == 0o075
    assert stat.S_IMODE((space.directory() / "retire-verify.json").stat().st_mode) == 0o644
    assert stat.S_IMODE((space.directory() / "hydro-run-backup.csv").stat().st_mode) == 0o640
