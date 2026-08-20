from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.node27_autopipeline as autopipe

RUN_A = "fcst_gfs_2026062012_basins_qhh_shud"
RUN_B = "fcst_gfs_2026062112_basins_qhh_shud"
DIRECT_GRID_RUN = "fcst_gfs_2026070600_dg_0123456789abcdef"
LEGACY_SAME_CYCLE_RUN = "fcst_gfs_2026070600_basins_qhh_shud"
NODE27_DATABASE_URL = "postgresql://node27_writer:secret@127.0.0.1:55432/nhms"


def _stats_guard_result(**overrides: Any) -> dict[str, Any]:
    return {
        "status": "completed",
        "min_mods": autopipe.STATS_GUARD_MIN_MODS,
        "max_chunks": autopipe.STATS_GUARD_MAX_CHUNKS,
        "analyzed": [],
        "deferred": [],
        **overrides,
    }


def _write_run(object_store_root: Path, run_id: str, *, handoff: bool = True) -> None:
    input_dir = object_store_root / "runs" / run_id / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "identity": {
            "run_id": run_id,
            "source_id": "gfs",
            "model_id": "basins_qhh_shud",
            "basin_id": "basins_qhh",
            "basin_version_id": "basins_qhh_v2026_06",
            "model_package_uri": "s3://nhms/models/basins_qhh_shud/v2026_06/package/",
            "forcing_version_id": f"forc_{run_id}",
        },
        "cycle_time": "2026-06-20T12:00:00Z",
        "start_time": "2026-06-20T12:00:00Z",
        "end_time": "2026-06-30T12:00:00Z",
        "forcing": {"forcing_package_uri": f"s3://nhms/forcing/{run_id}/"},
        "output_uri": f"s3://nhms/runs/{run_id}/output/",
        "run_manifest_uri": f"s3://nhms/runs/{run_id}/input/manifest.json",
    }
    (input_dir / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    if handoff:
        (input_dir / "forcing_domain_handoff.json").write_text("{}\n", encoding="utf-8")


def _prepare_autopipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    runs: Mapping[str, bool],
    apply_reports: Mapping[str, dict[str, Any] | BaseException] | None = None,
    command_handler: Callable[[list[str], dict[str, str]], tuple[int, str, str]] | None = None,
) -> tuple[Path, list[list[str]], list[str]]:
    object_store_root = tmp_path / "object-store"
    basins_root = tmp_path / "Basins"
    work_root = tmp_path / "autopipe-work"
    log_root = tmp_path / "autopipe-logs"
    basins_root.mkdir()
    work_root.mkdir()
    log_root.mkdir()
    for run_id, has_handoff in runs.items():
        _write_run(object_store_root, run_id, handoff=has_handoff)

    calls: list[list[str]] = []
    published_calls: list[str] = []

    monkeypatch.setattr(autopipe, "_basin_seeded", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        autopipe,
        "_ensure_seeded_basin_display_ready",
        lambda _database_url, model_id: {
            "model_id": model_id,
            "river_network_version_id": f"{model_id}_rivnet",
            "output_geometry_backfilled": 0,
            "model_activated_rows": 1,
        },
    )
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: set())
    monkeypatch.setenv("AUTOPIPE_WORK_ROOT", str(work_root))
    monkeypatch.setenv("AUTOPIPE_LOG_ROOT", str(log_root))
    monkeypatch.setenv("NHMS_NODE27_INGEST_ROLE", autopipe.INGEST_ROLE)
    monkeypatch.setenv("NHMS_SERVICE_ROLE", autopipe.INGEST_ROLE)
    monkeypatch.setenv("NHMS_NODE27_INGEST_CONFIG_SOURCE", "pytest")
    monkeypatch.setenv("DATABASE_URL", NODE27_DATABASE_URL)

    def fake_publish(database_url: str) -> int:
        published_calls.append(database_url)
        return 7

    monkeypatch.setattr(autopipe, "_publish_display_runs", fake_publish)
    # Phase 3.5 talks to the database directly; the handoff cases below are not
    # about it, and an unstubbed guard would dial a real socket. The #1378 cases
    # at the bottom of this file re-patch it with their own doubles.
    monkeypatch.setattr(autopipe, "_analyze_frontier_chunks", lambda _database_url: _stats_guard_result())

    reports = dict(apply_reports or {})

    def fake_apply_path(manifest_path: str | Path, **_kwargs: object) -> dict[str, Any]:
        run_id = Path(manifest_path).parents[1].name
        report = reports.get(run_id, _handoff_success(run_id))
        if isinstance(report, BaseException):
            raise report
        return report

    monkeypatch.setattr(autopipe, "_apply_object_store_forcing_handoff", fake_apply_path)

    def fake_run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        calls.append(argv)
        if command_handler is not None:
            return command_handler(argv, env)
        command = " ".join(argv)
        if "node27_ingest_run.py" in command:
            return 0, json.dumps({"status": "registered"}) + "\n", ""
        if "workers.output_parser.cli" in command:
            run_id = argv[-1]
            return 0, json.dumps({"status": "parsed", "rows_written": len(run_id)}) + "\n", ""
        if "node27_refresh_coverage.py" in command:
            return 0, json.dumps({"refreshed": True}) + "\n", ""
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(autopipe, "_run", fake_run)
    monkeypatch.setattr(autopipe, "_refresh_coverage_script", lambda: Path("/fake/node27_refresh_coverage.py"))

    return object_store_root, calls, published_calls


def _run_main(capsys: pytest.CaptureFixture[str], object_store_root: Path, *extra: str) -> tuple[int, dict[str, Any]]:
    rc = autopipe.main(
        [
            "--object-store-root",
            str(object_store_root),
            "--basins-root",
            str(object_store_root.parent / "Basins"),
            *extra,
        ]
    )
    return rc, json.loads(capsys.readouterr().out)


def _handoff_success(run_id: str) -> dict[str, Any]:
    return {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "applied",
        "available": True,
        "ready": True,
        "row_counts": {
            "met.forcing_version": 1,
            "met.met_station": 2,
            "met.forcing_station_timeseries": 8,
            "met.interp_weight": 4,
        },
        "identity": {"run_id": run_id, "source_id": "gfs"},
        "unavailable_reasons": [],
    }


def _handoff_unavailable(code: str = "HANDOFF_FIELD_MISSING") -> dict[str, Any]:
    return {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "unavailable",
        "available": False,
        "ready": False,
        "row_counts": {},
        "unavailable_reasons": [{"code": code, "detail": "redacted"}],
    }


def _handoff_failed() -> dict[str, Any]:
    return {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "failed",
        "available": False,
        "ready": False,
        "row_counts": {},
        "unavailable_reasons": [{"code": "HANDOFF_APPLY_SQL_FAILURE", "detail": "redacted"}],
    }


def test_direct_grid_run_discovery_uses_manifest_basin_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={DIRECT_GRID_RUN: True},
    )

    rc, summary = _run_main(capsys, object_store_root, "--only-basin", "qhh")

    assert rc == 0
    assert summary["discovered_runs"] == 1
    assert summary["runs"]["processed"] == 1
    assert summary["runs"]["details"][0]["run_id"] == DIRECT_GRID_RUN
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
    assert published_calls == [NODE27_DATABASE_URL]


def test_exact_cycle_direct_grid_filter_excludes_legacy_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={DIRECT_GRID_RUN: True, LEGACY_SAME_CYCLE_RUN: True, RUN_A: True},
    )

    rc, summary = _run_main(
        capsys,
        object_store_root,
        "--only-cycle",
        "2026070600",
        "--direct-grid-only",
    )

    assert rc == 0
    assert summary["discovered_runs"] == 1
    assert summary["runs"]["processed"] == 1
    assert summary["runs"]["details"][0]["run_id"] == DIRECT_GRID_RUN
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
    assert published_calls == [NODE27_DATABASE_URL]


def test_excluded_basin_is_not_seeded_or_ingested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={DIRECT_GRID_RUN: True},
    )

    rc, summary = _run_main(
        capsys,
        object_store_root,
        "--exclude-basins",
        "basins_qhh",
    )

    assert rc == 0
    assert summary["excluded_basins"] == ["qhh"]
    assert summary["discovered_runs"] == 0
    assert summary["basins"] == []
    assert summary["runs"]["processed"] == 0
    assert calls == []
    assert published_calls == []


def test_parallel_workers_preserve_deterministic_result_order_and_final_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, _calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True, RUN_B: True},
    )

    rc, summary = _run_main(capsys, object_store_root, "--workers", "2")

    assert rc == 0
    assert summary["runs"]["workers"] == 2
    assert [detail["run_id"] for detail in summary["runs"]["details"]] == [RUN_A, RUN_B]
    assert published_calls == [NODE27_DATABASE_URL]


def test_exact_cycle_filter_rejects_noncanonical_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={DIRECT_GRID_RUN: True},
    )

    with pytest.raises(SystemExit, match="2"):
        _run_main(capsys, object_store_root, "--only-cycle", "2026-07-06T00:00:00Z")

    assert calls == []
    assert published_calls == []


def _command_kinds(calls: list[list[str]]) -> list[str]:
    kinds: list[str] = []
    for argv in calls:
        command = " ".join(argv)
        if "node27_ingest_run.py" in command:
            kinds.append("register")
        elif "workers.output_parser.cli" in command:
            kinds.append("parse")
        elif "node27_refresh_coverage.py" in command:
            kinds.append("coverage")
    return kinds


def test_declared_handoff_success_records_run_details_without_mirror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
    assert published_calls == [NODE27_DATABASE_URL]
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "ingested"
    assert detail["stage"] == "coverage"
    assert detail["forcing_stage"] == {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "applied",
        "ready": True,
        "row_counts": {
            "met.forcing_version": 1,
            "met.met_station": 2,
            "met.forcing_station_timeseries": 8,
            "met.interp_weight": 4,
        },
        "reason_codes": [],
    }
    assert detail["parse_status"] == "parsed"
    assert detail["coverage_refresh"] == "refreshed"
    assert summary["runs"]["published"] == 7


def test_missing_handoff_degrades_forcing_stage_without_blocking_qdown_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: False})

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
    assert published_calls == [NODE27_DATABASE_URL]
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "ingested"
    assert detail["stage"] == "coverage"
    assert detail["forcing_stage"] == {
        "mode": autopipe.NO_FORCING_HANDOFF_MODE,
        "status": "skipped",
        "ready": False,
        "row_counts": {},
        "reason_codes": [autopipe.NO_FORCING_HANDOFF_REASON],
    }
    assert detail["parse_status"] == "parsed"
    assert detail["coverage_refresh"] == "refreshed"
    assert summary["runs"]["published"] == 7
    rendered = json.dumps(summary)
    assert "n22-secret" not in rendered


def test_declared_handoff_unavailable_does_not_fallback_to_node22_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable("HANDOFF_PAYLOAD_CHECKSUM_MISMATCH")},
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert _command_kinds(calls) == ["register"]
    assert published_calls == []
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "failed"
    assert detail["stage"] == "forcing_handoff"
    assert detail["forcing_stage"]["mode"] == autopipe.OBJECT_STORE_HANDOFF_MODE
    assert detail["forcing_stage"]["reason_codes"] == ["HANDOFF_PAYLOAD_CHECKSUM_MISMATCH"]


def test_declared_handoff_apply_exception_isolated_without_node22_db_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True, RUN_B: True},
        apply_reports={
            RUN_A: RuntimeError(
                'apply exploded with {"p\\u0061ssword": "n22-secret"}'
            ),
            RUN_B: _handoff_success(RUN_B),
        },
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert _command_kinds(calls) == ["register", "register", "parse", "coverage"]
    details = {detail["run_id"]: detail for detail in summary["runs"]["details"]}
    assert details[RUN_A]["outcome"] == "failed"
    assert details[RUN_A]["stage"] == "forcing_handoff"
    assert details[RUN_A]["forcing_stage"] == {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "failed",
        "ready": False,
        "row_counts": {},
        "reason_codes": [autopipe.FORCING_HANDOFF_FAILED_REASON],
    }
    assert details[RUN_B]["outcome"] == "ingested"
    rendered = json.dumps(summary)
    assert "n22-secret" not in rendered


def test_parse_and_coverage_failures_preserve_forcing_evidence_and_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(argv: list[str], _env: dict[str, str]) -> tuple[int, str, str]:
        command = " ".join(argv)
        run_id = argv[-1]
        if "node27_ingest_run.py" in command:
            return 0, "{}", ""
        if "workers.output_parser.cli" in command and run_id == RUN_A:
            return 1, "", "parse exploded"
        if "workers.output_parser.cli" in command:
            return 0, json.dumps({"status": "parsed", "rows_written": 11}) + "\n", ""
        if "node27_refresh_coverage.py" in command and run_id == RUN_B:
            return 7, "", "coverage exploded"
        raise AssertionError(f"unexpected command: {argv}")

    object_store_root, calls, _published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True, RUN_B: True},
        command_handler=handler,
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert _command_kinds(calls) == ["register", "parse", "register", "parse", "coverage"]
    details = {detail["run_id"]: detail for detail in summary["runs"]["details"]}
    assert details[RUN_A]["outcome"] == "failed"
    assert details[RUN_A]["stage"] == "parse"
    assert details[RUN_A]["forcing_stage"]["mode"] == autopipe.OBJECT_STORE_HANDOFF_MODE
    assert details[RUN_B]["outcome"] == "ingested"
    assert details[RUN_B]["stage"] == "coverage"
    assert details[RUN_B]["coverage_refresh"] == "refresh_failed_rc7"


# ---------------------------------------------------------------------------
# Issue #1378: phase 3.5 frontier-chunk statistics guard. The DB double records
# every statement, so the ANALYZE, its statement_timeout and the last_analyze
# read-back are asserted as behaviour rather than as a summary field alone.
# ---------------------------------------------------------------------------

CHUNK_SCHEMA = "_timescaledb_internal"
_BEFORE = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)
_AFTER = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)


class _FakeCursor:
    def __init__(
        self,
        statements: list[tuple[str, Any]],
        candidates: list[tuple[str, str, int, datetime | None]],
        last_analyze: dict[str, datetime | None],
        analyze_error: Exception | None,
        analyze_errors: Mapping[str, Exception],
        candidates_error: Exception | None = None,
    ) -> None:
        self._statements = statements
        self._candidates = candidates
        self._candidates_error = candidates_error
        self._last_analyze = last_analyze
        self._analyze_error = analyze_error
        self._analyze_errors = analyze_errors
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._statements.append((sql, params))
        if "timescaledb_information.chunks" in sql:
            assert params == (autopipe.STATS_GUARD_MIN_MODS,)
            if self._candidates_error is not None:
                raise self._candidates_error
            self._result = list(self._candidates)
        elif sql.startswith("ANALYZE"):
            if self._analyze_error is not None:
                raise self._analyze_error
            # ``ANALYZE "schema"."chunk"`` -> the quoted identifiers.
            quoted = sql.split('"')[1::2]
            per_chunk = self._analyze_errors.get(".".join(quoted))
            if per_chunk is not None:
                raise per_chunk
        elif "pg_stat_user_tables" in sql:
            self._result = [(self._last_analyze[f"{params[0]}.{params[1]}"],)]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.autocommit = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def _install_fake_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidates: list[tuple[str, str, int, datetime | None]],
    last_analyze: dict[str, datetime | None] | None = None,
    analyze_error: Exception | None = None,
    analyze_errors: Mapping[str, Exception] | None = None,
    candidates_error: Exception | None = None,
) -> tuple[list[tuple[str, Any]], list[str], list[_FakeConnection]]:
    statements: list[tuple[str, Any]] = []
    dsns: list[str] = []
    connections: list[_FakeConnection] = []
    reads = dict(last_analyze or {f"{row[0]}.{row[1]}": _AFTER for row in candidates})
    per_chunk_errors = dict(analyze_errors or {})

    def connect(database_url: str) -> _FakeConnection:
        dsns.append(database_url)
        connection = _FakeConnection(
            _FakeCursor(
                statements,
                candidates,
                reads,
                analyze_error,
                per_chunk_errors,
                candidates_error=candidates_error,
            )
        )
        connections.append(connection)
        return connection

    monkeypatch.setattr(autopipe.psycopg2, "connect", connect)
    return statements, dsns, connections


def test_stats_guard_analyzes_a_touched_frontier_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    statements, dsns, connections = _install_fake_db(
        monkeypatch,
        candidates=[(CHUNK_SCHEMA, "_hyper_1_58_chunk", 6_800_000, _BEFORE)],
    )

    guard = autopipe._analyze_frontier_chunks(NODE27_DATABASE_URL)

    assert dsns == [NODE27_DATABASE_URL]
    # ANALYZE reports its statistics at commit; a read-back inside the same
    # transaction would report the pre-ANALYZE value.
    assert connections[0].autocommit is True
    assert connections[0].closed is True
    assert [sql for sql, _params in statements][1:3] == [
        f"SET statement_timeout = {autopipe.STATS_GUARD_TIMEOUT_MS}",
        f'ANALYZE "{CHUNK_SCHEMA}"."_hyper_1_58_chunk"',
    ]
    assert guard["status"] == "completed"
    assert guard["deferred"] == []
    (entry,) = guard["analyzed"]
    assert entry["chunk"] == f"{CHUNK_SCHEMA}._hyper_1_58_chunk"
    assert entry["n_mod_since_analyze"] == 6_800_000
    assert entry["status"] == "ok"
    assert entry["last_analyze"] == _AFTER.isoformat()
    assert isinstance(entry["seconds"], float)


def test_stats_guard_below_the_floor_does_no_work(monkeypatch: pytest.MonkeyPatch) -> None:
    statements, _dsns, _connections = _install_fake_db(monkeypatch, candidates=[])

    guard = autopipe._analyze_frontier_chunks(NODE27_DATABASE_URL)

    assert [sql for sql, _params in statements if sql.startswith("ANALYZE")] == []
    assert guard == {
        "status": "completed",
        "min_mods": autopipe.STATS_GUARD_MIN_MODS,
        "max_chunks": autopipe.STATS_GUARD_MAX_CHUNKS,
        "analyzed": [],
        "deferred": [],
    }


def test_stats_guard_without_ingest_never_touches_the_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A tick that only re-published earlier runs wrote no rows and moved no frontier.

    Deliberately NOT the publish predicate, which also fires on
    ``already_ingested``: the guard's trigger is rows this tick actually wrote.
    """
    object_store_root, _calls, published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: {RUN_A})
    analyze_calls: list[str] = []
    monkeypatch.setattr(
        autopipe,
        "_analyze_frontier_chunks",
        lambda database_url: analyze_calls.append(database_url) or _stats_guard_result(),
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["runs"]["already_ingested"] == 1
    assert summary["runs"]["ingested"] == 0
    assert published == [NODE27_DATABASE_URL]
    assert analyze_calls == []
    assert summary["stats_guard"]["status"] == "not_triggered"
    assert summary["stats_guard"]["reason"] == "no_run_ingested"


def test_stats_guard_defers_everything_past_the_per_tick_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = [
        (CHUNK_SCHEMA, f"_hyper_1_{index}_chunk", 900_000 - index, _BEFORE)
        for index in range(5)
    ]
    statements, _dsns, _connections = _install_fake_db(monkeypatch, candidates=candidates)

    guard = autopipe._analyze_frontier_chunks(NODE27_DATABASE_URL)

    # The ordering that makes "first 3" mean "the 3 driftiest" lives in SQL.
    assert "ORDER BY s.n_mod_since_analyze DESC" in statements[0][0]
    assert [sql for sql, _params in statements if sql.startswith("ANALYZE")] == [
        f'ANALYZE "{CHUNK_SCHEMA}"."_hyper_1_{index}_chunk"' for index in range(autopipe.STATS_GUARD_MAX_CHUNKS)
    ]
    assert [entry["chunk"] for entry in guard["analyzed"]] == [
        f"{CHUNK_SCHEMA}._hyper_1_{index}_chunk" for index in range(autopipe.STATS_GUARD_MAX_CHUNKS)
    ]
    assert guard["deferred"] == [
        f"{CHUNK_SCHEMA}._hyper_1_3_chunk",
        f"{CHUNK_SCHEMA}._hyper_1_4_chunk",
    ]


def test_stats_guard_reports_an_unrefreshed_last_analyze_as_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """PG15 has no MAINTAIN bit: a non-owner ANALYZE warns and returns success."""

    _install_fake_db(
        monkeypatch,
        candidates=[
            (CHUNK_SCHEMA, "_hyper_1_58_chunk", 6_800_000, _BEFORE),
            (CHUNK_SCHEMA, "_hyper_1_62_chunk", 6_700_000, None),
        ],
        last_analyze={
            f"{CHUNK_SCHEMA}._hyper_1_58_chunk": _BEFORE,
            f"{CHUNK_SCHEMA}._hyper_1_62_chunk": None,
        },
    )

    guard = autopipe._analyze_frontier_chunks(NODE27_DATABASE_URL)

    assert guard["status"] == "completed"
    assert [entry["status"] for entry in guard["analyzed"]] == ["warning", "warning"]


def test_stats_guard_analyze_failure_is_recorded_without_failing_the_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_db(
        monkeypatch,
        candidates=[(CHUNK_SCHEMA, "_hyper_1_58_chunk", 6_800_000, _BEFORE)],
        analyze_error=RuntimeError("canceling statement due to statement timeout"),
    )

    guard = autopipe._analyze_frontier_chunks(NODE27_DATABASE_URL)

    # A chunk-level failure is that chunk's entry, not the guard's verdict:
    # ``stats_guard.status = "failed"`` is reserved for connect / candidate
    # query failures (the main() leg below).
    assert guard["status"] == "completed"
    assert "error" not in guard
    (entry,) = guard["analyzed"]
    assert entry["status"] == "failed"
    assert "statement timeout" in entry["error"]
    assert entry["seconds"] is None
    assert entry["last_analyze"] is None

    # ... and the same failure reaching main() leaves the tick's own verdict alone.
    object_store_root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})

    def explode(_database_url: str) -> dict[str, Any]:
        raise RuntimeError("guard exploded")

    monkeypatch.setattr(autopipe, "_analyze_frontier_chunks", explode)

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["status"] == "completed"
    assert summary["runs"]["ingested"] == 1
    assert summary["stats_guard"]["status"] == "failed"
    assert "guard exploded" in summary["stats_guard"]["error"]


def test_stats_guard_one_failed_chunk_does_not_swallow_the_rest_of_the_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A locked or vanished chunk must not starve the batch queued behind it.

    A failed ANALYZE leaves ``n_mod_since_analyze`` untouched, so the offender
    stays on top of the next tick's descending candidate list: aborting the
    batch would re-lose the very same chunks every single tick.
    """

    real_guard = autopipe._analyze_frontier_chunks
    candidates = [(CHUNK_SCHEMA, f"_hyper_1_{index}_chunk", 900_000 - index, _BEFORE) for index in range(3)]
    statements, _dsns, _connections = _install_fake_db(
        monkeypatch,
        candidates=candidates,
        analyze_errors={
            f"{CHUNK_SCHEMA}._hyper_1_0_chunk": RuntimeError("canceling statement due to lock timeout"),
        },
    )

    guard = real_guard(NODE27_DATABASE_URL)

    # Attempted, not merely recorded: chunks 2 and 3 reached the database.
    assert [sql for sql, _params in statements if sql.startswith("ANALYZE")] == [
        f'ANALYZE "{CHUNK_SCHEMA}"."_hyper_1_{index}_chunk"' for index in range(3)
    ]
    assert guard["status"] == "completed"
    assert "error" not in guard
    assert [(entry["chunk"], entry["status"]) for entry in guard["analyzed"]] == [
        (f"{CHUNK_SCHEMA}._hyper_1_0_chunk", "failed"),
        (f"{CHUNK_SCHEMA}._hyper_1_1_chunk", "ok"),
        (f"{CHUNK_SCHEMA}._hyper_1_2_chunk", "ok"),
    ]
    assert "lock timeout" in guard["analyzed"][0]["error"]

    # ... and the tick carrying that batch still returns 0.
    object_store_root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    monkeypatch.setattr(autopipe, "_analyze_frontier_chunks", real_guard)

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["status"] == "completed"
    assert [entry["status"] for entry in summary["stats_guard"]["analyzed"]] == ["failed", "ok", "ok"]


def test_stats_guard_connect_failure_is_reported_without_the_dsn_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard-level handler is the only thing between a libpq connect error
    and the JSON summary: libpq echoes the whole conninfo (password included)
    into its message, so the handler has to redact, not merely record."""

    def explode(_database_url: str) -> object:
        raise RuntimeError("could not connect to server: password=hunter2secret host=127.0.0.1 port=55432")

    monkeypatch.setattr(autopipe.psycopg2, "connect", explode)

    guard = autopipe._analyze_frontier_chunks(NODE27_DATABASE_URL)

    assert guard["status"] == "failed"
    assert guard["error"]
    assert "could not connect to server" in guard["error"]
    assert "hunter2secret" not in guard["error"]
    assert "password=[redacted]" in guard["error"]
    # Nothing was attempted, and the skeleton stays uniformly shaped.
    assert guard["analyzed"] == []
    assert guard["deferred"] == []


def test_stats_guard_candidate_query_failure_fails_the_guard_not_the_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other guard-level leg: the candidate query itself blowing up (catalog
    permissions, a TimescaleDB version without the view) is the guard's own
    verdict -- and still not the ingest tick's."""

    real_guard = autopipe._analyze_frontier_chunks
    _statements, _dsns, connections = _install_fake_db(
        monkeypatch,
        candidates=[(CHUNK_SCHEMA, "_hyper_1_58_chunk", 6_800_000, _BEFORE)],
        candidates_error=RuntimeError("permission denied for view timescaledb_information.chunks"),
    )

    guard = real_guard(NODE27_DATABASE_URL)

    assert guard["status"] == "failed"
    assert "permission denied for view" in guard["error"]
    assert guard["analyzed"] == []
    assert guard["deferred"] == []
    # The connection is released even on the failing path.
    assert connections[0].closed is True

    # ... and the tick that ran it still reports its own success.
    object_store_root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    monkeypatch.setattr(autopipe, "_analyze_frontier_chunks", real_guard)

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["status"] == "completed"
    assert summary["runs"]["ingested"] == 1
    assert summary["stats_guard"]["status"] == "failed"
    assert "permission denied for view" in summary["stats_guard"]["error"]


def test_stats_guard_switch_off_skips_the_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_store_root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    analyze_calls: list[str] = []
    monkeypatch.setattr(
        autopipe,
        "_analyze_frontier_chunks",
        lambda database_url: analyze_calls.append(database_url) or _stats_guard_result(),
    )
    monkeypatch.setenv("NODE27_AUTOPIPE_STATS_GUARD", "off")

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["runs"]["ingested"] == 1
    assert analyze_calls == []
    assert summary["stats_guard"]["status"] == "skipped"
    assert summary["stats_guard"]["reason"] == "NODE27_AUTOPIPE_STATS_GUARD=off"


def test_stats_guard_candidate_query_pins_the_selection_contract() -> None:
    """The candidate SQL is only ever executed against a fake cursor here, so
    the clauses that decide *which* chunks the guard touches need their own
    oracle (same precedent as the compression runner's ``_CHUNK_QUERY`` pins)."""

    sql = autopipe._STATS_GUARD_CANDIDATES_SQL
    # Drifty chunks are the ones ABOVE the floor -- a flipped comparison would
    # analyze exactly the chunks that need nothing.
    assert "n_mod_since_analyze >= %s" in sql
    # Compressed chunks belong to the compression runner's ride-along.
    assert "is_compressed = false" in sql
    assert "('hydro', 'river_timeseries')" in sql
    assert "('met', 'forcing_station_timeseries')" in sql
    # "First 3" only means "the 3 driftiest" while the ordering holds.
    assert "ORDER BY s.n_mod_since_analyze DESC" in sql


def test_stats_guard_constants_match_the_agreed_budget() -> None:
    """Literal integers on the right-hand side: the tests above spend the
    constants on both sides, so only this pins the values themselves."""

    assert autopipe.STATS_GUARD_MIN_MODS == 10_000
    assert autopipe.STATS_GUARD_TIMEOUT_MS == 120_000
