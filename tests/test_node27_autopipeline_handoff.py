from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2
import pytest

import scripts.node27_autopipeline as autopipe
from packages.common.forcing_domain_handoff_apply import (
    REASON_APPLY_COMPRESSED_CHUNK_BLOCKED,
)

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


def _authority_result(**overrides: Any) -> dict[str, Any]:
    """The repair leg's summary shape (issue #1468)."""

    return {
        "status": "completed",
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


class _DeclineStore:
    """In-memory stand-in for `ops.ingest_recompute_decline` (#1781).

    Both DB-boundary wrappers are patched onto the SAME store, so
    ``declines_active`` in the summary is genuinely the row count the writes
    produced rather than a second, independently faked number.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.write_error: BaseException | None = None
        self.count_calls = 0

    def record(
        self,
        _database_url: str,
        *,
        run_id: str,
        init_state_id: str,
        product_mtime: float,
        reason_code: str,
        detail: str | None,
    ) -> None:
        if self.write_error is not None:
            raise self.write_error
        key = (run_id, init_state_id, product_mtime)
        if key in {(row["run_id"], row["init_state_id"], row["product_mtime"]) for row in self.rows}:
            return  # ON CONFLICT DO NOTHING
        self.rows.append(
            {
                "run_id": run_id,
                "init_state_id": init_state_id,
                "product_mtime": product_mtime,
                "reason_code": reason_code,
                "detail": detail,
            }
        )

    def count(self, _database_url: str) -> int:
        self.count_calls += 1
        return len(self.rows)


def _set_initial_state(object_store_root: Path, run_id: str, state_id: str) -> None:
    path = object_store_root / "runs" / run_id / "input" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["initial_state"] = {"state_id": state_id}
    path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")


def _prepare_autopipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    runs: Mapping[str, bool],
    apply_reports: Mapping[str, dict[str, Any] | BaseException] | None = None,
    command_handler: Callable[[list[str], dict[str, str]], tuple[int, str, str]] | None = None,
    decline_store: _DeclineStore | None = None,
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
    # #1781: both decline surfaces talk to the database directly and run on
    # every tick (the count does), so every case in this file needs the double,
    # not just the decline cases.
    store = decline_store if decline_store is not None else _DeclineStore()
    monkeypatch.setattr(autopipe, "_record_recompute_decline", store.record)
    monkeypatch.setattr(autopipe, "_active_decline_count", store.count)
    # Phase 3.5 talks to the database directly; the handoff cases below are not
    # about it, and an unstubbed guard would dial a real socket. The #1378 cases
    # at the bottom of this file re-patch it with their own doubles. BOTH legs
    # need a double: the #1468 repair leg runs on every tick, ingest or not.
    monkeypatch.setattr(autopipe, "_analyze_frontier_chunks", lambda _database_url: _stats_guard_result())
    monkeypatch.setattr(
        autopipe, "_analyze_unanalyzed_authority_tables", lambda _database_url: _authority_result()
    )

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


def _handoff_unavailable(
    code: str = "HANDOFF_FIELD_MISSING",
    *,
    detail: str | None = "redacted",
) -> dict[str, Any]:
    reason: dict[str, Any] = {"code": code}
    if detail is not None:
        reason["detail"] = detail
    return {
        "mode": autopipe.OBJECT_STORE_HANDOFF_MODE,
        "status": "unavailable",
        "available": False,
        "ready": False,
        "row_counts": {},
        "unavailable_reasons": [reason],
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
        "reason_details": {},
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
        authority_candidates: list[tuple[str, str, int, datetime | None]] | None = None,
        authority_candidates_error: Exception | None = None,
    ) -> None:
        self._statements = statements
        self._candidates = candidates
        self._candidates_error = candidates_error
        self._authority_candidates = list(authority_candidates or [])
        self._authority_candidates_error = authority_candidates_error
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
        # The repair leg's candidate query also selects from pg_stat_user_tables,
        # so it MUST be matched before the generic read-back branch below --
        # otherwise it would be answered with a last_analyze row and the leg
        # would silently analyze nothing. `hypertables` is unique to it.
        elif "timescaledb_information.hypertables" in sql:
            # Constant SQL, unlike the frontier candidate query: no parameters.
            assert params is None
            if self._authority_candidates_error is not None:
                raise self._authority_candidates_error
            self._result = list(self._authority_candidates)
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
    authority_candidates: list[tuple[str, str, int, datetime | None]] | None = None,
    authority_candidates_error: Exception | None = None,
) -> tuple[list[tuple[str, Any]], list[str], list[_FakeConnection]]:
    statements: list[tuple[str, Any]] = []
    dsns: list[str] = []
    connections: list[_FakeConnection] = []
    authority_rows = list(authority_candidates or [])
    reads = dict(
        last_analyze
        or {f"{row[0]}.{row[1]}": _AFTER for row in list(candidates) + authority_rows}
    )
    per_chunk_errors = dict(analyze_errors or {})

    def connect(database_url: str, **_kwargs: Any) -> _FakeConnection:
        dsns.append(database_url)
        connection = _FakeConnection(
            _FakeCursor(
                statements,
                candidates,
                reads,
                analyze_error,
                per_chunk_errors,
                candidates_error=candidates_error,
                authority_candidates=authority_rows,
                authority_candidates_error=authority_candidates_error,
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


def test_stats_guard_without_ingest_frontier_leg_never_touches_the_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A tick that only re-published earlier runs wrote no rows and moved no frontier.

    Deliberately NOT the publish predicate, which also fires on
    ``already_ingested``: the FRONTIER leg's trigger is rows this tick actually
    wrote. The repair leg (#1468) is a different question entirely -- statistics
    being absent -- so it still runs on this very tick, and its result still
    reaches the summary.
    """
    object_store_root, _calls, published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: {RUN_A})
    analyze_calls: list[str] = []
    monkeypatch.setattr(
        autopipe,
        "_analyze_frontier_chunks",
        lambda database_url: analyze_calls.append(database_url) or _stats_guard_result(),
    )
    authority_calls: list[str] = []
    repaired = {
        "table": "core.river_segment_crosswalk",
        "relpages": 2_180,
        "seconds": 0.4,
        "last_analyze": _AFTER.isoformat(),
        "status": "ok",
    }
    monkeypatch.setattr(
        autopipe,
        "_analyze_unanalyzed_authority_tables",
        lambda database_url: authority_calls.append(database_url) or _authority_result(analyzed=[repaired]),
    )

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["runs"]["already_ingested"] == 1
    assert summary["runs"]["ingested"] == 0
    assert published == [NODE27_DATABASE_URL]
    assert analyze_calls == []
    assert summary["stats_guard"]["status"] == "not_triggered"
    assert summary["stats_guard"]["reason"] == "no_run_ingested"
    # The gate is per-leg, not per-guard: the repair leg ran on the same tick.
    assert authority_calls == [NODE27_DATABASE_URL]
    assert summary["stats_guard"]["authority"] == {
        "status": "completed",
        "analyzed": [repaired],
        "deferred": [],
    }


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

    def explode(_database_url: str, **_kwargs: Any) -> object:
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
    # Compressed chunks are out of scope (#1596/#1468): ANALYZE on a bare
    # compressed-chunk name would zero the relstats TimescaleDB preserves at
    # compression time — see runbook §4.7 陷阱 and the change's design D3.
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


# --------------------------------------------------------------------------- #
# Phase 3.5 repair leg (issue #1468): authority tables whose cumulative
# statistics were wiped by a crash recovery and can never re-trigger a
# threshold on their own.
# --------------------------------------------------------------------------- #

CROSSWALK = ("core", "river_segment_crosswalk")
GRID_CELL = ("met", "canonical_grid_cell")


def _run_main_captured(
    capsys: pytest.CaptureFixture[str], object_store_root: Path, *extra: str
) -> tuple[int, pytest.CaptureResult[str]]:
    """``_run_main`` consumes stderr with the summary; the --progress lines live there."""

    rc = autopipe.main(
        [
            "--object-store-root",
            str(object_store_root),
            "--basins-root",
            str(object_store_root.parent / "Basins"),
            *extra,
        ]
    )
    return rc, capsys.readouterr()


def test_stats_guard_authority_candidate_query_pins_the_repair_contract() -> None:
    """The repair leg's candidate SQL only ever meets a fake cursor here, so the
    clauses deciding WHICH tables get ANALYZEd need their own oracle (same
    precedent as the frontier candidate pin above)."""

    sql = autopipe._STATS_GUARD_AUTHORITY_CANDIDATES_SQL

    # The wipe signature: BOTH timestamps absent. Either one alone would also
    # match tables that autovacuum is looking after perfectly well.
    assert "s.last_analyze IS NULL" in sql
    assert "s.last_autoanalyze IS NULL" in sql
    # An empty table has nothing to analyze; relpages lives in pg_class and so
    # survives the very wipe that zeroed the cumulative counters.
    assert "c.relpages > 0" in sql
    # Ordinary tables only -- not views, not partitioned parents, not indexes.
    assert "c.relkind = 'r'" in sql
    assert "s.schemaname IN ('core', 'met', 'hydro')" in sql
    # Hypertable roots are excluded (ANALYZE on a root recurses into chunks);
    # chunks themselves live in _timescaledb_internal, outside the schema tuple.
    assert "NOT EXISTS" in sql
    assert "timescaledb_information.hypertables h" in sql
    assert "h.hypertable_schema = s.schemaname" in sql
    assert "h.hypertable_name = s.relname" in sql
    assert "_timescaledb_internal" not in sql
    # "First 3" only means "the 3 biggest" while the ordering holds.
    assert "ORDER BY c.relpages DESC" in sql


def test_stats_guard_repair_leg_analyzes_a_wiped_authority_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements, dsns, connections = _install_fake_db(
        monkeypatch,
        candidates=[],
        authority_candidates=[(*CROSSWALK, 2_180, None)],
    )

    authority = autopipe._analyze_unanalyzed_authority_tables(NODE27_DATABASE_URL)

    assert dsns == [NODE27_DATABASE_URL]
    # Same connection discipline as the frontier leg: the read-back is only
    # honest on a later transaction.
    assert connections[0].autocommit is True
    assert connections[0].closed is True
    assert [sql for sql, _params in statements][1:3] == [
        f"SET statement_timeout = {autopipe.STATS_GUARD_TIMEOUT_MS}",
        'ANALYZE "core"."river_segment_crosswalk"',
    ]
    assert authority["status"] == "completed"
    assert authority["deferred"] == []
    (entry,) = authority["analyzed"]
    assert entry == {
        "table": "core.river_segment_crosswalk",
        "relpages": 2_180,
        "seconds": entry["seconds"],
        "last_analyze": _AFTER.isoformat(),
        "status": "ok",
    }
    assert isinstance(entry["seconds"], float)


def test_stats_guard_repair_leg_runs_on_a_tick_that_ingested_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``ingested_runs`` gate belongs to the frontier leg alone.

    Driven through ``_stats_guard`` rather than the leg function, because the
    gate is what is under test: a wiped table is repaired on precisely the quiet
    ticks the old single-leg guard returned early from.
    """

    _install_fake_db(
        monkeypatch,
        candidates=[],
        authority_candidates=[(*GRID_CELL, 4_100, None)],
    )
    frontier_calls: list[str] = []
    monkeypatch.setattr(
        autopipe,
        "_analyze_frontier_chunks",
        lambda database_url: frontier_calls.append(database_url) or _stats_guard_result(),
    )

    guard = autopipe._stats_guard(NODE27_DATABASE_URL, ingested_runs=0, env={})

    assert guard["status"] == "not_triggered"
    assert guard["reason"] == "no_run_ingested"
    assert frontier_calls == []
    authority = guard["authority"]
    assert authority["status"] == "completed"
    assert [(entry["table"], entry["status"]) for entry in authority["analyzed"]] == [
        ("met.canonical_grid_cell", "ok")
    ]
    assert authority["analyzed"][0]["last_analyze"] == _AFTER.isoformat()


def test_stats_guard_repair_leg_defers_everything_past_the_shared_per_tick_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One cap for both legs: the repair leg does not get its own budget."""

    authority_candidates = [
        ("core", f"authority_table_{index}", 9_000 - index, None) for index in range(5)
    ]
    statements, _dsns, _connections = _install_fake_db(
        monkeypatch,
        candidates=[],
        authority_candidates=authority_candidates,
    )

    authority = autopipe._analyze_unanalyzed_authority_tables(NODE27_DATABASE_URL)

    assert [sql for sql, _params in statements if sql.startswith("ANALYZE")] == [
        f'ANALYZE "core"."authority_table_{index}"' for index in range(autopipe.STATS_GUARD_MAX_CHUNKS)
    ]
    assert [entry["table"] for entry in authority["analyzed"]] == [
        f"core.authority_table_{index}" for index in range(autopipe.STATS_GUARD_MAX_CHUNKS)
    ]
    assert authority["deferred"] == ["core.authority_table_3", "core.authority_table_4"]


def test_stats_guard_repair_leg_isolates_one_failed_table_from_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked or vanished authority table must not starve the batch behind it.

    A failed ANALYZE leaves the double-NULL signature untouched, so the offender
    keeps re-appearing at the top of the candidate list: aborting the batch would
    re-lose the very same tables every tick, forever.
    """

    authority_candidates = [
        (*CROSSWALK, 2_180, None),
        (*GRID_CELL, 1_900, None),
        ("hydro", "run_display_coverage", 40, None),
    ]
    statements, _dsns, _connections = _install_fake_db(
        monkeypatch,
        candidates=[],
        authority_candidates=authority_candidates,
        analyze_errors={
            "core.river_segment_crosswalk": RuntimeError("canceling statement due to lock timeout"),
        },
    )

    authority = autopipe._analyze_unanalyzed_authority_tables(NODE27_DATABASE_URL)

    # Attempted, not merely recorded: tables 2 and 3 reached the database.
    assert [sql for sql, _params in statements if sql.startswith("ANALYZE")] == [
        'ANALYZE "core"."river_segment_crosswalk"',
        'ANALYZE "met"."canonical_grid_cell"',
        'ANALYZE "hydro"."run_display_coverage"',
    ]
    # A table-level failure is that table's entry, never the leg's verdict.
    assert authority["status"] == "completed"
    assert "error" not in authority
    assert [(entry["table"], entry["status"]) for entry in authority["analyzed"]] == [
        ("core.river_segment_crosswalk", "failed"),
        ("met.canonical_grid_cell", "ok"),
        ("hydro.run_display_coverage", "ok"),
    ]
    assert "lock timeout" in authority["analyzed"][0]["error"]
    assert authority["analyzed"][0]["seconds"] is None
    assert authority["analyzed"][0]["last_analyze"] is None


def test_stats_guard_repair_leg_candidate_query_failure_never_reaches_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard-level isolation, repair-leg side: the catalog query blowing up is
    the leg's own verdict and leaves the frontier leg's summary intact."""

    _install_fake_db(
        monkeypatch,
        candidates=[],
        authority_candidates=[(*CROSSWALK, 2_180, None)],
        authority_candidates_error=RuntimeError("permission denied for view timescaledb_information.hypertables"),
    )
    monkeypatch.setattr(autopipe, "_analyze_frontier_chunks", lambda _database_url: _stats_guard_result())

    guard = autopipe._stats_guard(NODE27_DATABASE_URL, ingested_runs=1, env={})

    assert guard["status"] == "completed"
    assert guard["authority"]["status"] == "failed"
    assert "permission denied for view" in guard["authority"]["error"]
    assert guard["authority"]["analyzed"] == []
    assert guard["authority"]["deferred"] == []


def test_stats_guard_switch_off_skips_both_legs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One switch, both legs -- and the summary says so for each of them."""

    object_store_root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    frontier_calls: list[str] = []
    authority_calls: list[str] = []
    monkeypatch.setattr(
        autopipe,
        "_analyze_frontier_chunks",
        lambda database_url: frontier_calls.append(database_url) or _stats_guard_result(),
    )
    monkeypatch.setattr(
        autopipe,
        "_analyze_unanalyzed_authority_tables",
        lambda database_url: authority_calls.append(database_url) or _authority_result(),
    )
    monkeypatch.setenv("NODE27_AUTOPIPE_STATS_GUARD", "off")

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["runs"]["ingested"] == 1
    assert frontier_calls == []
    assert authority_calls == []
    assert summary["stats_guard"]["status"] == "skipped"
    assert summary["stats_guard"]["reason"] == "NODE27_AUTOPIPE_STATS_GUARD=off"
    assert summary["stats_guard"]["authority"] == {
        "status": "skipped",
        "reason": "NODE27_AUTOPIPE_STATS_GUARD=off",
        "analyzed": [],
        "deferred": [],
    }


def test_stats_guard_summary_carries_both_legs_on_an_ingesting_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Structure pin: the summary an operator reads carries the repair leg under
    ``stats_guard.authority`` with its own status / analyzed / deferred, without
    disturbing any key the frontier leg already published."""

    object_store_root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    chunk_entry = {
        "chunk": f"{CHUNK_SCHEMA}._hyper_1_58_chunk",
        "n_mod_since_analyze": 6_800_000,
        "seconds": 1.5,
        "last_analyze": _AFTER.isoformat(),
        "status": "ok",
    }
    authority_entry = {
        "table": "met.canonical_grid_cell",
        "relpages": 4_100,
        "seconds": 0.9,
        "last_analyze": _AFTER.isoformat(),
        "status": "ok",
    }
    monkeypatch.setattr(
        autopipe,
        "_analyze_frontier_chunks",
        lambda _database_url: _stats_guard_result(analyzed=[chunk_entry]),
    )
    monkeypatch.setattr(
        autopipe,
        "_analyze_unanalyzed_authority_tables",
        lambda _database_url: _authority_result(
            analyzed=[authority_entry], deferred=["core.river_segment_crosswalk"]
        ),
    )

    rc, summary = _run_main(capsys, object_store_root, "--progress")

    assert rc == 0
    assert summary["stats_guard"] == {
        "status": "completed",
        "min_mods": autopipe.STATS_GUARD_MIN_MODS,
        "max_chunks": autopipe.STATS_GUARD_MAX_CHUNKS,
        "analyzed": [chunk_entry],
        "deferred": [],
        "authority": {
            "status": "completed",
            "analyzed": [authority_entry],
            "deferred": ["core.river_segment_crosswalk"],
        },
    }


def test_stats_guard_progress_line_reports_the_repair_leg_on_a_quiet_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The old print condition suppressed the whole line on ``not_triggered`` --
    i.e. on exactly the ticks where the repair leg is the only thing working."""

    object_store_root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: {RUN_A})
    monkeypatch.setattr(
        autopipe,
        "_analyze_unanalyzed_authority_tables",
        lambda _database_url: _authority_result(
            analyzed=[
                {
                    "table": "core.river_segment_crosswalk",
                    "relpages": 2_180,
                    "seconds": 0.4,
                    "last_analyze": _AFTER.isoformat(),
                    "status": "ok",
                },
                {
                    "table": "met.canonical_grid_cell",
                    "relpages": 4_100,
                    "seconds": None,
                    "last_analyze": None,
                    "status": "failed",
                    "error": "RuntimeError: nope",
                },
                # PG15 non-owner silent skip: ANALYZE returned success and
                # refreshed nothing. Counted apart from ok and from failed, or
                # the line would report work that did not happen.
                {
                    "table": "core.basin_version",
                    "relpages": 12,
                    "seconds": 0.1,
                    "last_analyze": _BEFORE.isoformat(),
                    "status": "warning",
                },
            ],
            deferred=["hydro.run_display_coverage"],
        ),
    )

    rc, captured = _run_main_captured(capsys, object_store_root, "--progress")

    assert rc == 0
    guard_lines = [line for line in captured.err.splitlines() if line.startswith("[stats-guard]")]
    assert guard_lines == [
        "[stats-guard] not_triggered: ok 0, warning 0, failed 0, deferred 0"
        ", authority completed: ok 1, warning 1, failed 1, deferred 1"
    ]


def test_stats_guard_progress_line_reports_a_failed_repair_leg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A guard-level repair-leg failure (connect / candidate query) leaves
    ``analyzed`` empty, so every count reads 0 -- exactly like a leg that found
    nothing to do. Only the leg's own status tells the two apart, which is why
    the line prints it (round-1 review C3)."""

    object_store_root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: {RUN_A})
    monkeypatch.setattr(
        autopipe,
        "_analyze_unanalyzed_authority_tables",
        lambda _database_url: _authority_result(
            status="failed", error="OperationalError: could not connect to server"
        ),
    )

    rc, captured = _run_main_captured(capsys, object_store_root, "--progress")

    assert rc == 0
    guard_lines = [line for line in captured.err.splitlines() if line.startswith("[stats-guard]")]
    assert guard_lines == [
        "[stats-guard] not_triggered: ok 0, warning 0, failed 0, deferred 0"
        ", authority failed: ok 0, warning 0, failed 0, deferred 0"
    ]


def test_stats_guard_progress_line_stays_silent_when_neither_leg_worked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The relaxed condition must not turn into "print on every tick": a tick
    where both legs found nothing still says nothing."""

    object_store_root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={RUN_A: True})
    monkeypatch.setattr(autopipe, "_already_ingested_runs", lambda *_args, **_kwargs: {RUN_A})

    rc, captured = _run_main_captured(capsys, object_store_root, "--progress")

    assert rc == 0
    assert [line for line in captured.err.splitlines() if line.startswith("[stats-guard]")] == []


# --------------------------------------------------------------------------- #
# #1781 -- terminal state for a compressed-chunk-blocked recompute.
#
# The production loop this pins: node-22 regenerates a run's products, the tick
# correctly detects the recompute, the forcing handoff is correctly rejected
# because its target chunk is compressed, nothing advances, and the next tick
# repeats it forever (88 runs when this was written, 116 by deploy time -- the
# set grows, every tick, rc=1). The fix is a recorded terminal
# state, so the failure mode a test has to bite on is BOTH directions: a
# blocked recompute must stop retrying, and everything else must not.
# --------------------------------------------------------------------------- #

BLOCKED = REASON_APPLY_COMPRESSED_CHUNK_BLOCKED
# Literal, not the constant: the gate must key on BLOCKED alone, and pinning the
# wire value here keeps this file honest if the apply layer renames it (#1781).
GUARD_FAILED = "HANDOFF_APPLY_COMPRESSED_CHUNK_GUARD_FAILED"
BLOCKED_DETAIL = (
    "Reingest targets compressed chunk _timescaledb_internal._hyper_1_52_chunk in "
    "met.forcing_station_timeseries; run decompress procedure per "
    "docs/runbooks/tier-node27-timeseries-storage.md#43-decompress-procedure before retrying."
)


def test_compressed_chunk_blocked_recompute_is_declined_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _DeclineStore()
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable(BLOCKED, detail=BLOCKED_DETAIL)},
        decline_store=store,
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")
    expected_mtime = autopipe._run_product_mtime(object_store_root, RUN_A)

    rc, summary = _run_main(capsys, object_store_root)

    # rc is the whole point: this tick used to be red forever.
    assert rc == 0
    assert summary["status"] == "completed"
    assert _command_kinds(calls) == ["register"]
    assert published_calls == []
    detail = summary["runs"]["details"][0]
    assert detail["outcome"] == "declined"
    # The evidence of what was declined is preserved verbatim, not swapped for
    # a softer story: stage and reason codes stay exactly as the failure had.
    assert detail["stage"] == "forcing_handoff"
    assert detail["forcing_stage"]["reason_codes"] == [BLOCKED]
    # #1781 diagnosability: the decline row is the only post-hoc handle on a
    # permanently suppressed run, so `detail` must carry the guard's (redacted)
    # message naming the offending chunk -- not a copy of the reason code, which
    # `reason_code` already holds.
    assert store.rows == [
        {
            "run_id": RUN_A,
            "init_state_id": "state-a",
            "product_mtime": expected_mtime,
            "reason_code": BLOCKED,
            "detail": BLOCKED_DETAIL,
        }
    ]
    assert summary["runs"]["declined"] == 1
    assert summary["runs"]["declined_runs"] == [{"run_id": RUN_A, "reason_code": BLOCKED}]
    assert summary["declines_active"] == 1


def test_declined_run_is_named_on_the_progress_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A terminal state is silent by construction -- the tick goes green and
    never mentions the run again. The progress line is the only place an
    operator watching the loop sees which runs stopped."""

    object_store_root, _calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable(BLOCKED)},
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")

    rc, captured = _run_main_captured(capsys, object_store_root, "--progress")

    assert rc == 0
    decline_lines = [line for line in captured.err.splitlines() if line.startswith("[declines]")]
    assert len(decline_lines) == 1
    assert RUN_A in decline_lines[0]
    assert "active records 1" in decline_lines[0]


def test_progress_line_reports_an_unknown_decline_count_on_a_quiet_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The steady state of a long-standing backlog: nothing declined this tick,
    and the count read failed. `None` is falsy exactly like `0`, so a gate on
    truthiness goes silent in precisely the case the line exists to surface."""

    object_store_root, _calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
    )
    monkeypatch.setattr(autopipe, "_active_decline_count", lambda *_args, **_kwargs: None)

    rc, captured = _run_main_captured(capsys, object_store_root, "--progress")

    assert rc == 0
    decline_lines = [line for line in captured.err.splitlines() if line.startswith("[declines]")]
    assert len(decline_lines) == 1
    assert "active records unknown" in decline_lines[0]


def test_decline_detail_falls_back_to_the_reason_codes_when_none_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reason without a `detail` field must still produce a non-null decline
    detail -- degraded, but never a null column on the only surviving record."""

    store = _DeclineStore()
    object_store_root, _calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable(BLOCKED, detail=None)},
        decline_store=store,
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["runs"]["details"][0]["outcome"] == "declined"
    assert store.rows[0]["detail"] == BLOCKED


@pytest.mark.parametrize(
    "report",
    [
        pytest.param(_handoff_unavailable("HANDOFF_APPLY_SQL_FAILURE"), id="transient-reason"),
        pytest.param(_handoff_unavailable(GUARD_FAILED), id="guard-internal-failure"),
        pytest.param(_handoff_failed(), id="failed-status"),
        pytest.param(RuntimeError("apply exploded"), id="generic-exception"),
    ],
)
def test_transient_forcing_failure_still_fails_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    report: dict[str, Any] | BaseException,
) -> None:
    """Terminal-stating the wrong failure class is the expensive mistake here:
    a transient failure declared terminal stops retrying a run that would have
    succeeded on the next tick, and goes green while doing it."""

    store = _DeclineStore()
    object_store_root, _calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: report},
        decline_store=store,
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert summary["runs"]["details"][0]["outcome"] == "failed"
    assert summary["runs"]["declined"] == 0
    assert summary["runs"]["declined_runs"] == []
    assert store.rows == []
    assert summary["declines_active"] == 0


def test_blocked_recompute_without_product_mtime_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _DeclineStore()
    object_store_root, _calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable(BLOCKED)},
        decline_store=store,
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")
    monkeypatch.setattr(autopipe, "_run_product_mtime", lambda *_args, **_kwargs: None)

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert summary["runs"]["details"][0]["outcome"] == "failed"
    assert store.rows == []


def test_failed_decline_write_keeps_the_run_failing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one unrecoverable mistake: reporting a run as accounted for on a
    record that never committed. It would never be retried and never appear in
    the decline table either."""

    store = _DeclineStore()
    store.write_error = RuntimeError("insert exploded")
    object_store_root, _calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable(BLOCKED)},
        decline_store=store,
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert summary["runs"]["details"][0]["outcome"] == "failed"
    assert summary["runs"]["failed"] == 1
    assert store.rows == []
    assert summary["declines_active"] == 0


def test_declined_run_does_not_pollute_ingest_publish_or_stats_counters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """E6: the new outcome must be invisible to every counter that was already
    matching outcome strings exactly."""

    guard_calls: list[int] = []
    object_store_root, _calls, published_calls = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True, RUN_B: True},
        apply_reports={RUN_A: _handoff_unavailable(BLOCKED), RUN_B: _handoff_success(RUN_B)},
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")

    def fake_stats_guard(_database_url: str, *, ingested_runs: int, env: Any) -> dict[str, Any]:
        guard_calls.append(ingested_runs)
        return _stats_guard_result(authority=_authority_result())

    monkeypatch.setattr(autopipe, "_stats_guard", fake_stats_guard)

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 0
    assert summary["runs"]["ingested"] == 1
    assert summary["runs"]["failed"] == 0
    assert summary["runs"]["declined"] == 1
    assert summary["runs"]["failed_runs"] == []
    assert summary["runs"]["ingested_by_source"] == {"gfs": 1, "ifs": 0}
    # publish_eligible fired on the ingested run alone, and the frontier leg saw
    # exactly one row-writing run.
    assert published_calls == [NODE27_DATABASE_URL]
    assert guard_calls == [1]


def test_every_tick_summary_carries_the_live_decline_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """E10: the anti-silence field. A tick with nothing declined must still say
    how many terminal records are standing, or a permanent decline is invisible
    on every tick after the one that wrote it."""

    store = _DeclineStore()
    object_store_root, _calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        decline_store=store,
    )

    rc, summary = _run_main(capsys, object_store_root)
    assert rc == 0
    assert summary["runs"]["declined"] == 0
    assert summary["declines_active"] == 0

    store.rows.append(
        {
            "run_id": "some-earlier-run",
            "init_state_id": "state-z",
            "product_mtime": 1.0,
            "reason_code": BLOCKED,
            "detail": None,
        }
    )

    rc, summary = _run_main(capsys, object_store_root)
    assert rc == 0
    assert summary["runs"]["declined"] == 0
    assert summary["declines_active"] == len(store.rows) == 1


# --- read side: `_already_ingested_runs` ------------------------------------ #


class _IngestedRunsCursor:
    def __init__(self, conn: "_IngestedRunsConnection") -> None:
        self.conn = conn
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_IngestedRunsCursor":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.conn.executed.append(sql)
        statement = sql.strip().upper()
        # psycopg2 semantics, deliberately faithful (#1781): once a statement
        # errors, EVERY later statement in the same transaction raises
        # InFailedSqlTransaction until the transaction is rewound. Only
        # ROLLBACK TO SAVEPOINT clears it -- which is what makes this double
        # able to tell a savepoint-scoped degrade apart from a bare
        # `except psycopg2.Error`, where the completeness query below would
        # then blow up instead.
        if statement.startswith("ROLLBACK TO SAVEPOINT"):
            self.conn.aborted = False
            self.rows = []
            return
        if self.conn.aborted:
            raise psycopg2.errors.InFailedSqlTransaction("current transaction is aborted")
        if statement.startswith("SAVEPOINT"):
            if self.conn.savepoint_error is not None:
                self.conn.aborted = True
                raise self.conn.savepoint_error
            self.rows = []
            return
        if statement.startswith("RELEASE SAVEPOINT"):
            self.rows = []
            return
        if "status = 'superseded'" in sql:
            assert params is not None
            bound = set(params[0])
            self.rows = [(run_id,) for run_id in self.conn.superseded_rows if run_id in bound]
        elif "ops.ingest_recompute_decline" in sql:
            if self.conn.decline_error is not None:
                self.conn.aborted = True
                raise self.conn.decline_error
            assert params is not None
            bound = set(params[0])
            self.rows = [row for row in self.conn.decline_rows if row[0] in bound]
        else:
            self.rows = list(self.conn.completeness_rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _IngestedRunsConnection:
    """Only the three statements `_already_ingested_runs` issues, dispatched on
    their text. A real database is the oracle for the SQL semantics
    (`tests/test_river_identity_normalization_integration.py`); what needs
    isolating here is the read-count budget and the key-matching rule."""

    def __init__(
        self,
        *,
        decline_rows: list[tuple[str, str, float]],
        completeness_rows: list[tuple[Any, ...]] | None = None,
        decline_error: BaseException | None = None,
        savepoint_error: BaseException | None = None,
        superseded_rows: list[str] | None = None,
    ) -> None:
        self.decline_rows = decline_rows
        self.completeness_rows = completeness_rows or []
        self.decline_error = decline_error
        self.savepoint_error = savepoint_error
        self.superseded_rows = superseded_rows or []
        self.aborted = False
        self.executed: list[str] = []

    def cursor(self) -> _IngestedRunsCursor:
        return _IngestedRunsCursor(self)

    def close(self) -> None:
        return None


def _count_object_store_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real_manifest = autopipe._load_run_manifest_or_none
    real_mtime = autopipe._run_product_mtime

    def counting_manifest(root: Path, run_id: str) -> Any:
        reads.append(run_id)
        return real_manifest(root, run_id)

    def counting_mtime(root: Path, run_id: str) -> Any:
        reads.append(run_id)
        return real_mtime(root, run_id)

    monkeypatch.setattr(autopipe, "_load_run_manifest_or_none", counting_manifest)
    monkeypatch.setattr(autopipe, "_run_product_mtime", counting_mtime)
    return reads


def test_matching_decline_record_suppresses_the_run_and_reads_only_its_products(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """E7: one batched query, and the object-store reads scale with the number
    of decline records -- not with the pending population. On node-27 the
    pending set is hundreds of runs and the decline set is a handful; reading
    per pending run would put a per-tick stat storm on NFS."""

    object_store_root = tmp_path / "object-store"
    pending = [RUN_A, RUN_B, DIRECT_GRID_RUN, LEGACY_SAME_CYCLE_RUN]
    for run_id in pending:
        _write_run(object_store_root, run_id)
        _set_initial_state(object_store_root, run_id, "state-a")
    mtime = autopipe._run_product_mtime(object_store_root, RUN_A)

    conn = _IngestedRunsConnection(decline_rows=[(RUN_A, "state-a", mtime)])
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn)
    reads = _count_object_store_reads(monkeypatch)

    assert autopipe._already_ingested_runs(
        NODE27_DATABASE_URL, pending, object_store_root=object_store_root
    ) == {RUN_A}
    assert set(reads) == {RUN_A}
    assert len([sql for sql in conn.executed if "ops.ingest_recompute_decline" in sql]) == 1


@pytest.mark.parametrize(
    ("recorded_state", "mtime_delta"),
    [
        pytest.param("state-b", 0.0, id="init-state-changed"),
        pytest.param("state-a", -5.0, id="products-regenerated"),
    ],
)
def test_unmatched_decline_key_reopens_the_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recorded_state: str,
    mtime_delta: float,
) -> None:
    """The key IS the reopen condition: any new evidence must put the run back
    into pending, or an operator who fixes the chunk and recomputes gets
    silence."""

    object_store_root = tmp_path / "object-store"
    _write_run(object_store_root, RUN_A)
    _set_initial_state(object_store_root, RUN_A, "state-a")
    mtime = autopipe._run_product_mtime(object_store_root, RUN_A)

    conn = _IngestedRunsConnection(decline_rows=[(RUN_A, recorded_state, mtime + mtime_delta)])
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn)

    assert (
        autopipe._already_ingested_runs(
            NODE27_DATABASE_URL, [RUN_A], object_store_root=object_store_root
        )
        == set()
    )


def test_any_of_a_runs_decline_records_may_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The table accumulates one row per blocked regeneration. Matching only the
    newest (or only one) row would silently drop the older decisions."""

    object_store_root = tmp_path / "object-store"
    _write_run(object_store_root, RUN_A)
    _set_initial_state(object_store_root, RUN_A, "state-a")
    mtime = autopipe._run_product_mtime(object_store_root, RUN_A)

    conn = _IngestedRunsConnection(
        decline_rows=[(RUN_A, "state-a", mtime - 90.0), (RUN_A, "state-a", mtime)]
    )
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn)

    assert autopipe._already_ingested_runs(
        NODE27_DATABASE_URL, [RUN_A], object_store_root=object_store_root
    ) == {RUN_A}


def test_decline_record_is_inert_without_an_object_store_side(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Read-side fail-closed: with no evidence to compare against, suppress
    nothing."""

    object_store_root = tmp_path / "object-store"
    _write_run(object_store_root, RUN_A)
    conn = _IngestedRunsConnection(decline_rows=[(RUN_A, "state-a", 1.0)])
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn)

    assert autopipe._already_ingested_runs(NODE27_DATABASE_URL, [RUN_A], object_store_root=None) == set()


def test_missing_decline_table_degrades_to_no_suppression(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#1781 deploy window: new code, migration 000055 not applied yet. The
    decline read must not take the completeness read down with it -- it shares
    the transaction, so an unscoped catch leaves it poisoned and the next
    statement raises InFailedSqlTransaction instead."""

    object_store_root = tmp_path / "object-store"
    _write_run(object_store_root, RUN_A)
    _set_initial_state(object_store_root, RUN_A, "state-a")

    conn = _IngestedRunsConnection(
        decline_rows=[],
        decline_error=psycopg2.errors.UndefinedTable(
            'relation "ops.ingest_recompute_decline" does not exist'
        ),
    )
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn)

    # Degrade to "suppress nothing": the run goes back into pending, gets
    # blocked again, and the decline write fails against the same missing table
    # -- i.e. exactly the pre-#1781 behaviour, which is the safe direction.
    assert (
        autopipe._already_ingested_runs(
            NODE27_DATABASE_URL, [RUN_A], object_store_root=object_store_root
        )
        == set()
    )
    # The completeness query still ran: the transaction survived the degrade.
    assert any("hydro.hydro_run h" in sql for sql in conn.executed)


def test_missing_decline_table_still_emits_a_summary_and_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole tick, not just the helper: an unguarded read would propagate
    out of main() with a traceback and NO summary at all -- strictly worse than
    the rc=1-with-summary this replaces."""

    real_already_ingested_runs = autopipe._already_ingested_runs
    store = _DeclineStore()
    store.write_error = psycopg2.errors.UndefinedTable(
        'relation "ops.ingest_recompute_decline" does not exist'
    )
    object_store_root, _calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable(BLOCKED, detail=BLOCKED_DETAIL)},
        decline_store=store,
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")
    # Un-stub the read side: this case is about it.
    monkeypatch.setattr(autopipe, "_already_ingested_runs", real_already_ingested_runs)
    monkeypatch.setattr(autopipe, "_active_decline_count", lambda *_args, **_kwargs: None)
    conn = _IngestedRunsConnection(
        decline_rows=[],
        decline_error=psycopg2.errors.UndefinedTable(
            'relation "ops.ingest_recompute_decline" does not exist'
        ),
    )
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn)

    rc, summary = _run_main(capsys, object_store_root)

    assert rc == 1
    assert summary["status"] == "completed_with_failures"
    assert summary["runs"]["details"][0]["outcome"] == "failed"
    assert summary["runs"]["declined"] == 0
    assert summary["declines_active"] is None
    assert store.rows == []


def _tick_with_real_read_side(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    superseded: bool,
) -> tuple[dict[str, Any], list[str], list[str], _DeclineStore]:
    """One whole tick on a run that is already excluded -- by `superseded` or by
    a standing decline record -- with the REAL `_already_ingested_runs` in play.

    The harness stubs that function outright, so no end-to-end case ever lets an
    exclusion reach `already_count` / `publish_eligible`. Un-stubbing it here (as
    `test_missing_decline_table_still_emits_a_summary_and_returns_one` does) is
    what closes that gap.
    """

    root.mkdir(parents=True, exist_ok=True)
    real_already_ingested_runs = autopipe._already_ingested_runs
    store = _DeclineStore()
    object_store_root, calls, published_calls = _prepare_autopipe(
        monkeypatch,
        root,
        runs={RUN_A: True},
        decline_store=store,
    )
    _set_initial_state(object_store_root, RUN_A, "state-a")
    mtime = autopipe._run_product_mtime(object_store_root, RUN_A)
    monkeypatch.setattr(autopipe, "_already_ingested_runs", real_already_ingested_runs)

    if superseded:
        conn = _IngestedRunsConnection(decline_rows=[], superseded_rows=[RUN_A])
    else:
        # The decline was written by an EARLIER tick: the row stands before this
        # one starts, on both the read surface and the count surface.
        conn = _IngestedRunsConnection(decline_rows=[(RUN_A, "state-a", mtime)])
        store.rows.append(
            {
                "run_id": RUN_A,
                "init_state_id": "state-a",
                "product_mtime": mtime,
                "reason_code": BLOCKED,
                "detail": BLOCKED_DETAIL,
            }
        )
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn)

    rc, summary = _run_main(capsys, object_store_root)
    assert rc == 0
    return summary, _command_kinds(calls), published_calls, store


def test_standing_decline_counts_as_already_done_exactly_like_a_retired_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The tick AFTER the decline was written, end to end.

    Helper-level coverage stops at `_already_ingested_runs`; nothing pinned what
    the tick summary then says. The contract (spec: "A standing decline counts
    as already-done, exactly like a retired run") is that the run is neither
    ingested nor failed nor retried, and that it lands in `already_count` and
    satisfies `publish_eligible` on its own -- the same shape `superseded` has
    had since before #1781. Running BOTH exclusions through the same tick and
    demanding an identical `runs` block is what makes that parity an assertion
    rather than a comment.
    """

    declined_summary, declined_kinds, declined_published, store = _tick_with_real_read_side(
        monkeypatch, tmp_path / "declined", capsys, superseded=False
    )

    # Not ingested, not failed, and never re-attempted: no register/parse means
    # `_process_run` -- and therefore the forcing handoff -- never ran.
    assert declined_summary["runs"]["ingested"] == 0
    assert declined_summary["runs"]["failed"] == 0
    assert declined_summary["runs"]["declined"] == 0
    assert declined_summary["runs"]["processed"] == 0
    assert declined_summary["runs"]["details"] == []
    assert declined_summary["runs"]["failed_runs"] == []
    assert declined_summary["runs"]["declined_runs"] == []
    assert declined_summary["runs"]["ingested_by_source"] == {"gfs": 0, "ifs": 0}
    assert "register" not in declined_kinds
    assert "parse" not in declined_kinds
    # No second decline row: the terminal record is not rewritten every tick.
    assert [row["run_id"] for row in store.rows] == [RUN_A]
    assert declined_summary["declines_active"] == 1
    # `already_count` carries it, and it alone satisfies `publish_eligible`.
    assert declined_summary["runs"]["already_ingested"] == 1
    assert declined_published == [NODE27_DATABASE_URL]

    retired_summary, retired_kinds, retired_published, retired_store = _tick_with_real_read_side(
        monkeypatch, tmp_path / "retired", capsys, superseded=True
    )

    # The parity claim, asserted: a standing decline produces the same tick
    # accounting as `status='superseded'`. `declines_active` is the one field
    # that legitimately differs -- it counts records, not exclusions.
    assert retired_summary["runs"] == declined_summary["runs"]
    assert retired_kinds == declined_kinds
    assert retired_published == declined_published
    assert retired_summary["declines_active"] == 0
    assert retired_store.rows == []


def test_declined_runs_degrades_when_the_savepoint_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """"Degrades to suppress nothing on any DB error" has to cover the statement
    that opens the scope too, and the degrade must NOT try to rewind to a
    savepoint that was never established -- that is itself an error."""

    object_store_root = tmp_path / "object-store"
    _write_run(object_store_root, RUN_A)
    _set_initial_state(object_store_root, RUN_A, "state-a")
    mtime = autopipe._run_product_mtime(object_store_root, RUN_A)

    conn = _IngestedRunsConnection(
        decline_rows=[(RUN_A, "state-a", mtime)],
        savepoint_error=psycopg2.errors.DiskFull("could not extend file"),
    )
    cur = conn.cursor()

    assert autopipe._declined_runs(cur, [RUN_A], object_store_root) == set()
    assert not [sql for sql in conn.executed if sql.strip().upper().startswith("ROLLBACK TO")]


# --- D11: "no manifest" is a known-empty key, not missing evidence ---------- #


def _write_product_output(object_store_root: Path, run_id: str) -> None:
    output_dir = object_store_root / "runs" / run_id / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "rivqdown.csv").write_text("time,value\n", encoding="utf-8")


def test_blocked_run_without_init_evidence_is_declined_then_suppressed_next_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D11, and it has to be TWO ticks.

    On node-27 tick 1 this shape was 14 of 158 runs: no manifest
    `initial_state`, `hydro_run.init_state_id` NULL, product mtime present. The
    old fail-closed rule turned "no init evidence" into retry-forever -- exactly
    the loop #1781 exists to kill -- and rc never reached 0.

    The half-fix this test exists to falsify is: write `''`, but keep skipping
    `''` on the read side. Under it tick 1 looks identical (rc 0, outcome
    declined, one row), and `ON CONFLICT DO NOTHING` even keeps `store.rows`
    at one row on tick 2 -- so neither rc nor the row count discriminates. The
    only observable that does is whether tick 2 attempts the handoff again,
    which is why tick 2 asserts on the command log and the apply call log.
    """

    store = _DeclineStore()
    real_already_ingested_runs = autopipe._already_ingested_runs
    # `_write_run`'s manifest carries no `initial_state` block at all, so the
    # init component of the key is the `''` sentinel on both sides.
    object_store_root, calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={RUN_A: True},
        apply_reports={RUN_A: _handoff_unavailable(BLOCKED, detail=BLOCKED_DETAIL)},
        decline_store=store,
    )
    monkeypatch.setattr(autopipe, "_already_ingested_runs", real_already_ingested_runs)

    apply_calls: list[str] = []
    stubbed_apply = autopipe._apply_object_store_forcing_handoff

    def recording_apply(manifest_path: str | Path, **kwargs: object) -> dict[str, Any]:
        apply_calls.append(Path(manifest_path).parents[1].name)
        return stubbed_apply(manifest_path, **kwargs)

    monkeypatch.setattr(autopipe, "_apply_object_store_forcing_handoff", recording_apply)

    # Tick 1: nothing recorded yet.
    monkeypatch.setattr(
        autopipe, "_connect", lambda *_args, **_kwargs: _IngestedRunsConnection(decline_rows=[])
    )
    rc_one, summary_one = _run_main(capsys, object_store_root)

    assert rc_one == 0
    assert summary_one["runs"]["details"][0]["outcome"] == "declined"
    assert summary_one["runs"]["declined"] == 1
    assert summary_one["runs"]["failed"] == 0
    assert [(row["run_id"], row["init_state_id"]) for row in store.rows] == [(RUN_A, "")]
    assert apply_calls == [RUN_A]
    tick_one_kinds = _command_kinds(calls)
    assert "register" in tick_one_kinds

    # Tick 2: same store, same evidence, and the row tick 1 wrote now stands on
    # the read surface -- built from the store, not hand-authored, so a write
    # side that recorded something unreadable cannot be papered over here.
    conn_two = _IngestedRunsConnection(
        decline_rows=[
            (row["run_id"], row["init_state_id"], row["product_mtime"]) for row in store.rows
        ]
    )
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn_two)
    rc_two, summary_two = _run_main(capsys, object_store_root)

    assert rc_two == 0
    # The discriminating assertions: zero handoff attempts for this run on tick
    # 2, on the real command log and the real apply call log.
    assert apply_calls == [RUN_A]
    assert _command_kinds(calls) == tick_one_kinds
    assert summary_two["runs"]["processed"] == 0
    assert summary_two["runs"]["details"] == []
    assert summary_two["runs"]["declined"] == 0
    assert summary_two["runs"]["failed"] == 0
    assert summary_two["runs"]["already_ingested"] == 1
    # No second row either: the terminal record is written once.
    assert len(store.rows) == 1
    assert summary_two["declines_active"] == 1


def test_empty_init_state_key_suppresses_a_run_with_no_manifest_at_all(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The literal production shape: `input/manifest.json` absent entirely, with
    the mtime coming from the output products. The read side must treat the
    recorded `''` as a value that matches, not as evidence it failed to get."""

    object_store_root = tmp_path / "object-store"
    _write_product_output(object_store_root, RUN_A)
    assert autopipe._load_run_manifest_or_none(object_store_root, RUN_A) is None
    mtime = autopipe._run_product_mtime(object_store_root, RUN_A)
    assert mtime is not None

    conn = _IngestedRunsConnection(decline_rows=[(RUN_A, "", mtime)])
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn)

    assert autopipe._already_ingested_runs(
        NODE27_DATABASE_URL, [RUN_A], object_store_root=object_store_root
    ) == {RUN_A}


def test_decline_on_the_empty_init_sentinel_reopens_once_a_manifest_appears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`''` is a key value, so it is also a reopen condition: the day the run
    gains a manifest with a real `initial_state_id`, the key changes, the record
    stops matching, and the run is re-evaluated instead of staying terminal."""

    object_store_root = tmp_path / "object-store"
    _write_run(object_store_root, RUN_A)
    _set_initial_state(object_store_root, RUN_A, "state-a")
    # Computed after the rewrite so the mismatch is isolated on the init
    # component -- `_set_initial_state` rewrites the manifest and moves mtime.
    mtime = autopipe._run_product_mtime(object_store_root, RUN_A)

    conn = _IngestedRunsConnection(decline_rows=[(RUN_A, "", mtime)])
    monkeypatch.setattr(autopipe, "_connect", lambda *_args, **_kwargs: conn)

    assert (
        autopipe._already_ingested_runs(
            NODE27_DATABASE_URL, [RUN_A], object_store_root=object_store_root
        )
        == set()
    )
