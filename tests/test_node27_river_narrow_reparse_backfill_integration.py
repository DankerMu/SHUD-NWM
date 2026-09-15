"""Disposable-database oracle for the #2382 narrow reparse backfill runner.

Each fixture run is first parsed by the production parser into the narrow
table, then moved to the post-re-forward shape: facts copied into
``hydro.river_timeseries_legacy`` as text identities, narrow facts removed,
route ``legacy``, status ``published``. The runner must bring it back.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from packages.common.node27_timeseries_lifecycle_lock import (
    acquire_timeseries_lifecycle_lock,
    release_timeseries_lifecycle_lock,
)
from packages.common.object_store import LocalObjectStore
from scripts import node27_river_narrow_reparse_backfill as backfill
from tests.integration_helpers import apply_migrations_from_zero
from workers.output_parser.parser import OutputParser, OutputParserConfig, PsycopgOutputParserRepository

pytestmark = pytest.mark.integration

_PREFIX = "s3://nhms"
_SEGMENTS = 4
_HOURS = 3
_NOW_HOUR = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
_FRESH = _NOW_HOUR - timedelta(days=2)
_FRESH_OLDER = _NOW_HOUR - timedelta(days=5)
_AGED = _NOW_HOUR - timedelta(days=40)


def _connect(url: str) -> Any:
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    return connection


def _scalar(connection: Any, sql: str, params: Any = None) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return None if row is None else next(iter(row.values()))


def _execute(connection: Any, sql: str, params: Any = None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


def _seed_authority(connection: Any) -> None:
    _execute(connection, "INSERT INTO core.basin VALUES ('b1', 'B1', NULL, NULL, now()) ON CONFLICT DO NOTHING")
    _execute(
        connection,
        """
        INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag)
        VALUES ('bv1', 'b1', 'v1', ST_SetSRID(ST_GeomFromText('MULTIPOLYGON(((0 0,0 1,1 1,0 0)))'), 4490), true)
        """,
    )
    _execute(
        connection,
        "INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, version_label, "
        "segment_count) VALUES ('rnv1', 'bv1', 'v1', %s)",
        (_SEGMENTS,),
    )
    _execute(
        connection,
        """
        INSERT INTO core.river_segment (river_segment_id, river_network_version_id, segment_order, properties_json)
        SELECT 'seg-' || g, 'rnv1', g, '{"shud_output_river": "true"}'::jsonb FROM generate_series(1, %s) g
        """,
        (_SEGMENTS,),
    )
    _execute(
        connection,
        """
        INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                                         calibration_version_id, shud_code_version, model_package_uri)
        VALUES ('m1', 'bv1', 'rnv1', 'mv1', 'cal1', '1.0', 's3://x')
        """,
    )


def _write_rivqdown(root: Path, run_id: str, start: datetime) -> None:
    store = LocalObjectStore(root, _PREFIX)
    lines = [",".join(["time", *(f"seg-{index}" for index in range(1, _SEGMENTS + 1))])]
    for hour in range(_HOURS):
        stamp = (start + timedelta(hours=hour)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(",".join([stamp, *(str(86400 * (hour + 1) * n) for n in range(1, _SEGMENTS + 1))]))
    store.write_bytes_atomic(f"runs/{run_id}/output/demo.rivqdown", ("\n".join(lines) + "\n").encode())


def _parser(url: str, root: Path) -> OutputParser:
    return OutputParser(
        config=OutputParserConfig(object_store_root=root, object_store_prefix=_PREFIX),
        repository=PsycopgOutputParserRepository(database_url=url),
        object_store=LocalObjectStore(root, _PREFIX),
    )


def _legacy_run(connection: Any, url: str, root: Path, run_id: str, start: datetime,
                status: str = "published") -> None:
    """A run in the exact post-re-forward shape: legacy route, legacy facts only."""
    _execute(
        connection,
        """
        INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id, cycle_time,
                                     start_time, end_time, status, run_manifest_uri, output_uri)
        VALUES (%s, 'forecast', 'sc', 'm1', 'bv1', %s, %s, %s, 'succeeded', 's3://m', %s)
        """,
        (run_id, start, start, start + timedelta(hours=_HOURS), f"{_PREFIX}/runs/{run_id}/output/"),
    )
    _write_rivqdown(root, run_id, start)
    _parser(url, root).parse_run(run_id)
    _execute(
        connection,
        """
        INSERT INTO hydro.river_timeseries_legacy (run_id, basin_version_id, river_network_version_id,
            river_segment_id, valid_time, lead_time_hours, variable, value, unit, quality_flag)
        SELECT h.run_id, h.basin_version_id, s.river_network_version_id, s.river_segment_id, n.valid_time,
               n.lead_time_hours, n.variable_e::text, n.value, n.unit_e::text, n.quality_flag_e::text
        FROM hydro.river_timeseries n
        JOIN hydro.hydro_run h ON h.run_key = n.run_key
        JOIN core.river_segment s ON s.river_segment_key = n.river_segment_key
        WHERE h.run_id = %s
        """,
        (run_id,),
    )
    _execute(connection, "DELETE FROM hydro.river_timeseries n USING hydro.hydro_run h "
                         "WHERE h.run_key = n.run_key AND h.run_id = %s", (run_id,))
    _execute(connection, "UPDATE hydro.hydro_run SET timeseries_store = 'legacy', status = %s WHERE run_id = %s",
             (status, run_id))


def _state(connection: Any, run_id: str) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT h.status, h.timeseries_store, h.parsed_at, h.error_code,
                   (SELECT count(*) FROM hydro.river_timeseries n WHERE n.run_key = h.run_key) AS narrow_rows,
                   (SELECT count(*) FROM hydro.river_timeseries_legacy l WHERE l.run_id = h.run_id) AS legacy_rows
            FROM hydro.hydro_run h WHERE h.run_id = %s
            """,
            (run_id,),
        )
        return dict(cursor.fetchone())


@pytest.fixture()
def world(throwaway_database_url: str, tmp_path: Path) -> Any:
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    _seed_authority(connection)
    root = tmp_path / "object-store"
    settings = backfill.Settings(
        database_url=throwaway_database_url,
        object_store_root=str(root),
        object_store_prefix=_PREFIX,
        window_days=21,
    )
    try:
        yield connection, settings, root
    finally:
        connection.close()


def _run(settings: backfill.Settings, tmp_path: Path, **overrides: Any) -> tuple[int, dict[str, Any]]:
    options: dict[str, Any] = {
        "receipt_dir": tmp_path,
        "concurrency": 2,
        "deadline": None,
        "max_failures": 10,
        "max_decompress_bytes": backfill.DEFAULT_MAX_DECOMPRESS_BYTES,
        "limit": None,
        "end_time_after": None,
        "lock_path": tmp_path / "lifecycle.lock",
    }
    options.update(overrides)
    return backfill.execute_run(settings, **options)


def test_backfill_reparses_in_window_runs_and_leaves_aged_runs_legacy(world: Any, tmp_path: Path) -> None:
    connection, settings, root = world
    url = settings.database_url
    _legacy_run(connection, url, root, "run_fresh", _FRESH)
    _legacy_run(connection, url, root, "run_superseded", _FRESH_OLDER, status="superseded")
    _legacy_run(connection, url, root, "run_aged", _AGED)
    _legacy_run(connection, url, root, "run_not_published", _FRESH, status="parsed")
    stamps = {run: _state(connection, run)["parsed_at"] for run in ("run_fresh", "run_aged")}

    plan_connection = backfill.connect(url)
    try:
        plan = backfill.build_plan(plan_connection, settings, None)
    finally:
        plan_connection.close()
    assert plan["candidates"] == 2
    assert plan["candidates_by_status"] == {"published": 1, "superseded": 1}
    assert plan["estimated_rows"] == 2 * _SEGMENTS * _HOURS
    assert _state(connection, "run_fresh")["timeseries_store"] == "legacy"

    code, receipt = _run(settings, tmp_path)
    assert code == backfill.EXIT_COMPLETE
    assert receipt["result"] == "complete"
    assert receipt["dispositions"] == {"reparsed": 2}
    assert receipt["rows_written"] == 2 * _SEGMENTS * _HOURS
    assert receipt["stop_reason"] == "exhausted"
    assert {"in_window": True, "status": "published", "runs": 1} in receipt["legacy_route_counts_before"]
    assert all(not row["in_window"] or row["status"] == "parsed" for row in receipt["legacy_route_counts_after"])

    fresh = _state(connection, "run_fresh")
    assert (fresh["status"], fresh["timeseries_store"], fresh["narrow_rows"], fresh["legacy_rows"]) == (
        "published", "narrow", _SEGMENTS * _HOURS, _SEGMENTS * _HOURS)
    assert fresh["parsed_at"] > stamps["run_fresh"]
    superseded = _state(connection, "run_superseded")
    assert (superseded["status"], superseded["timeseries_store"]) == ("superseded", "narrow")
    aged = _state(connection, "run_aged")
    assert (aged["timeseries_store"], aged["narrow_rows"], aged["parsed_at"]) == ("legacy", 0, stamps["run_aged"])
    assert _state(connection, "run_not_published")["timeseries_store"] == "legacy"
    # The row-lock re-check, not only the candidate query, keeps an aged run legacy.
    assert backfill.reparse_one(settings, "run_aged").disposition == "aged_out"
    assert _state(connection, "run_aged")["timeseries_store"] == "legacy"

    records = [json.loads(line) for line in (tmp_path / "runs.jsonl").read_text().splitlines()]
    assert sorted(record["run_id"] for record in records) == ["run_fresh", "run_superseded"]
    assert url not in (tmp_path / "runs.jsonl").read_text()

    report = backfill.verify_runs(settings, ["run_fresh", "run_superseded", "run_aged"])
    verdicts = {item["run_id"]: item["verdict"] for item in report["runs"]}
    assert verdicts == {"run_fresh": "pass", "run_superseded": "pass", "run_aged": "fail"}

    code, again = _run(settings, tmp_path)
    assert (code, again["candidates"], again["dispositions"]) == (backfill.EXIT_COMPLETE, 0, {})


def test_verify_detects_a_value_divergence(world: Any, tmp_path: Path) -> None:
    connection, settings, root = world
    _legacy_run(connection, settings.database_url, root, "run_fresh", _FRESH)
    assert _run(settings, tmp_path)[0] == backfill.EXIT_COMPLETE
    _execute(connection, "UPDATE hydro.river_timeseries SET value = value + 1 WHERE valid_time = %s", (_FRESH,))
    report = backfill.verify_runs(settings, ["run_fresh"])
    assert report["runs"][0]["verdict"] == "fail"
    assert report["runs"][0]["mismatched_rows"] == _SEGMENTS


def test_failed_parse_rolls_back_route_and_facts(world: Any, tmp_path: Path) -> None:
    connection, settings, root = world
    _legacy_run(connection, settings.database_url, root, "run_fresh", _FRESH)
    _legacy_run(connection, settings.database_url, root, "run_missing_artifact", _FRESH_OLDER)
    before = _state(connection, "run_missing_artifact")
    for path in (root / "runs" / "run_missing_artifact" / "output").iterdir():
        path.unlink()

    code, receipt = _run(settings, tmp_path, concurrency=1)
    assert code == backfill.EXIT_FAILED
    assert receipt["dispositions"] == {"reparsed": 1, "failed": 1}
    assert _state(connection, "run_missing_artifact") == before
    (failed,) = [json.loads(line) for line in (tmp_path / "runs.jsonl").read_text().splitlines()
                 if '"failed"' in line]
    assert failed["run_id"] == "run_missing_artifact" and failed["error_code"]


def test_post_parse_mismatch_rolls_back(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    connection, settings, root = world
    _legacy_run(connection, settings.database_url, root, "run_fresh", _FRESH)
    before = _state(connection, "run_fresh")
    monkeypatch.setattr(backfill, "_post_parse_problem", lambda *_args: "injected mismatch")
    outcome = backfill.reparse_one(settings, "run_fresh")
    assert (outcome.disposition, outcome.error_code) == ("failed", "VERIFY_MISMATCH")
    assert _state(connection, "run_fresh") == before


def test_failure_budget_stops_dispatch(world: Any, tmp_path: Path) -> None:
    connection, settings, root = world
    for index in range(3):
        run_id = f"run_broken_{index}"
        _legacy_run(connection, settings.database_url, root, run_id, _FRESH - timedelta(hours=index))
        for path in (root / "runs" / run_id / "output").iterdir():
            path.unlink()
    code, receipt = _run(settings, tmp_path, concurrency=1, max_failures=1)
    assert code == backfill.EXIT_FAILED
    assert (receipt["dispositions"], receipt["not_dispatched"], receipt["stop_reason"]) == (
        {"failed": 1}, 2, "failure_budget")


def test_deadline_and_limit_leave_the_rest_for_a_resumed_run(world: Any, tmp_path: Path) -> None:
    connection, settings, root = world
    _legacy_run(connection, settings.database_url, root, "run_a", _FRESH)
    _legacy_run(connection, settings.database_url, root, "run_b", _FRESH_OLDER)
    code, receipt = _run(settings, tmp_path, deadline=datetime.now(UTC) - timedelta(seconds=1))
    assert (code, receipt["not_dispatched"], receipt["stop_reason"]) == (backfill.EXIT_PARTIAL, 2, "deadline")
    code, receipt = _run(settings, tmp_path, limit=1)
    assert (code, receipt["dispositions"]) == (backfill.EXIT_COMPLETE, {"reparsed": 1})
    assert _state(connection, "run_a")["timeseries_store"] == "narrow"  # newest cycle first
    assert _state(connection, "run_b")["timeseries_store"] == "legacy"
    code, receipt = _run(settings, tmp_path, end_time_after=_FRESH)
    assert receipt["candidates"] == 0
    assert _run(settings, tmp_path)[1]["dispositions"] == {"reparsed": 1}


def test_compressed_narrow_overlap_is_decompressed_within_budget(world: Any, tmp_path: Path) -> None:
    connection, settings, root = world
    url = settings.database_url
    _legacy_run(connection, url, root, "run_fresh", _FRESH)
    _execute(
        connection,
        """
        INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id, cycle_time,
                                     start_time, end_time, status, run_manifest_uri, output_uri)
        VALUES ('run_neighbour', 'forecast', 'sc2', 'm1', 'bv1', %s, %s, %s, 'succeeded', 's3://m', %s)
        """,
        (_FRESH, _FRESH, _FRESH + timedelta(hours=_HOURS), f"{_PREFIX}/runs/run_neighbour/output/"),
    )
    _write_rivqdown(root, "run_neighbour", _FRESH)
    _parser(url, root).parse_run("run_neighbour")
    _execute(connection, "SELECT compress_chunk(c) FROM show_chunks('hydro.river_timeseries') c")
    compressed = "SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'river_timeseries' " \
                 "AND is_compressed"
    assert _scalar(connection, compressed) >= 1

    before = _state(connection, "run_fresh")
    with pytest.raises(backfill.RefusedError) as caught:
        _run(settings, tmp_path, max_decompress_bytes=0)
    assert caught.value.code == "DECOMPRESS_BUDGET_EXCEEDED"
    assert _state(connection, "run_fresh") == before
    compressed_before = _scalar(connection, compressed)
    assert compressed_before >= 1

    code, receipt = _run(settings, tmp_path)
    assert code == backfill.EXIT_COMPLETE
    assert receipt["dispositions"] == {"reparsed": 1}
    assert len(receipt["decompressed_chunks"]) == compressed_before
    assert _scalar(connection, compressed) == 0
    assert _state(connection, "run_fresh")["narrow_rows"] == _SEGMENTS * _HOURS
    assert _state(connection, "run_neighbour")["narrow_rows"] == _SEGMENTS * _HOURS


def test_refusals_change_nothing(world: Any, tmp_path: Path) -> None:
    connection, settings, root = world
    _legacy_run(connection, settings.database_url, root, "run_fresh", _FRESH)
    before = _state(connection, "run_fresh")
    lock_path = tmp_path / "lifecycle.lock"
    fd = acquire_timeseries_lifecycle_lock(lock_path)
    assert fd is not None
    try:
        with pytest.raises(backfill.RefusedError) as caught:
            _run(settings, tmp_path)
        assert caught.value.code == "LIFECYCLE_LOCK_CONTENDED"
    finally:
        release_timeseries_lifecycle_lock(fd)
    assert _state(connection, "run_fresh") == before
    assert not (tmp_path / "runs.jsonl").exists()
