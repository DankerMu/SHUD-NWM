from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apps.api.routes.hydro_display import _postgis_tile_params
from scripts import node27_timeseries_compression_benchmark as benchmark
from scripts import node27_timeseries_compression_live_evidence as live_evidence
from services.tiles.mvt import postgis_tile_sql


class _FakeCursor:
    def __init__(
        self,
        *,
        result_rows: list[dict[str, Any]],
        plan_reads: list[int],
        activity_sessions: list[list[dict[str, Any]]] | None = None,
        decompress: bool = True,
    ) -> None:
        self.result_rows = result_rows
        self.plan_reads = iter(plan_reads)
        self.activity_sessions = iter(activity_sessions or [[] for _ in range(5)])
        self.current = ""
        self.decompress = decompress
        self.executions: list[tuple[str, Any]] = []

    def execute(self, statement: str, parameters: Any) -> None:
        self.current = statement
        self.executions.append((statement, parameters))

    def fetchall(self) -> list[dict[str, Any]]:
        assert not self.current.startswith(benchmark.EXPLAIN_PREFIX)
        if "pg_stat_activity" in self.current:
            return next(self.activity_sessions)
        return self.result_rows

    def fetchone(self) -> dict[str, Any]:
        reads = next(self.plan_reads)
        return {
            "QUERY PLAN": [
                {
                    "Planning Time": 1.25,
                    "Execution Time": 5.5,
                    "Plan": {
                        "Node Type": "Custom Scan" if self.decompress else "Index Scan",
                        **(
                            {"Custom Plan Provider": "DecompressChunk"}
                            if self.decompress
                            else {}
                        ),
                        "Relation Name": "_hyper_3_7_chunk" if self.decompress else "river_timeseries",
                        "Shared Hit Blocks": 3,
                        "Shared Read Blocks": reads,
                        "Plans": [
                            {
                                "Node Type": "Index Scan",
                                "Shared Hit Blocks": 2,
                                "Shared Read Blocks": 0,
                            }
                        ],
                    },
                }
            ]
        }


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self.fake_cursor = cursor
        self.session: dict[str, Any] | None = None
        self.rolled_back = False
        self.closed = False

    def set_session(self, **kwargs: Any) -> None:
        self.session = kwargs

    def cursor(self) -> _FakeCursor:
        return self.fake_cursor

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _inputs() -> dict[str, Any]:
    return {
        "database_url": "opaque-test-dsn",
        "phase": "after",
        "curve_basin_version_id": "basin-heihe-v1",
        "curve_river_segment_id": "heihe_reach_000001",
        "curve_river_network_version_id": "heihe-network-v1",
        "curve_issue_time": datetime(2026, 7, 5, tzinfo=UTC),
        "curve_end_time": datetime(2026, 7, 12, tzinfo=UTC),
        "curve_scenario": "forecast_ifs_deterministic",
        "mvt_run_id": "fcst_ifs_2026070500_basins_heihe_shud",
        "mvt_basin_version_id": "basin-heihe-v1",
        "mvt_river_network_version_id": "heihe-network-v1",
        "mvt_valid_time": datetime(2026, 7, 6, tzinfo=UTC),
        "mvt_z": 9,
        "mvt_x": 420,
        "mvt_y": 210,
    }


def _capture(
    *, curve_reads: list[int], mvt_reads: list[int], phase: str = "after"
) -> tuple[dict[str, Any], list[_FakeConnection]]:
    curve_cursor = _FakeCursor(
        result_rows=[
            {
                "scenario_id": "forecast_ifs_deterministic",
                "model_id": "basins_heihe_shud",
                "source_id": "IFS",
                "cycle_time": datetime(2026, 7, 5, tzinfo=UTC),
                "run_end_time": datetime(2026, 7, 12, tzinfo=UTC),
                "forcing_version_id": "forcing-1",
                "river_network_version_id": "heihe-network-v1",
                "valid_time": datetime(2026, 7, 6, tzinfo=UTC),
                "value": 12.5,
                "unit": "m3/s",
            }
        ],
        plan_reads=curve_reads,
        decompress=phase == "after",
    )
    mvt_cursor = _FakeCursor(
        result_rows=[{"tile": b"\x1a\x02ok"}],
        plan_reads=mvt_reads,
        decompress=phase == "after",
    )
    curve_monitor = _FakeCursor(result_rows=[], plan_reads=[])
    mvt_monitor = _FakeCursor(result_rows=[], plan_reads=[])
    connections = [
        _FakeConnection(curve_cursor),
        _FakeConnection(curve_monitor),
        _FakeConnection(mvt_cursor),
        _FakeConnection(mvt_monitor),
    ]

    def connect(database_url: str) -> _FakeConnection:
        assert database_url == "opaque-test-dsn"
        return connections.pop(0)

    original = list(connections)
    inputs = {**_inputs(), "phase": phase}
    return benchmark.capture_benchmark_phase(**inputs, connect=connect), original


def test_capture_uses_exact_production_queries_bindings_and_new_readonly_connections() -> None:
    document, connections = _capture(curve_reads=[4, 0, 0, *([0] * 7)], mvt_reads=[2, 0, 0, *([0] * 7)])
    curve, mvt = document["queries"]

    assert [query["name"] for query in document["queries"]] == ["curve", "mvt"]
    assert "FROM hydro.river_timeseries rt" in curve["query_text"]
    assert "JOIN hydro.hydro_run h" in curve["query_text"]
    assert curve["query_text"].count("%s") == 8
    assert curve["binding"]["parameter_names"] == [
        "basin_version_id",
        "river_segment_id",
        "river_network_version_id",
        "issue_time",
        "start_time",
        "end_time",
        "source_or_scenario_tokens",
        "scenario_tokens",
    ]
    assert curve["binding"]["bound_parameters"] == [
        "basin-heihe-v1",
        "heihe_shud_riv_000001",
        "heihe-network-v1",
        "2026-07-05T00:00:00Z",
        "2026-07-05T00:00:00Z",
        "2026-07-12T00:00:00Z",
        ["forecast_ifs_deterministic"],
        ["forecast_ifs_deterministic"],
    ]
    assert mvt["query_text"] == postgis_tile_sql("hydro")
    assert mvt["binding"] == benchmark._json_value(
        _postgis_tile_params(
            {
                "run_id": _inputs()["mvt_run_id"],
                "basin_version_id": _inputs()["mvt_basin_version_id"],
                "river_network_version_id": _inputs()["mvt_river_network_version_id"],
                "variable": "q_down",
                "valid_time": _inputs()["mvt_valid_time"],
            },
            z=9,
            x=420,
            y=210,
        )
    )
    assert all(
        connection.session
        == {"isolation_level": "REPEATABLE READ", "readonly": True, "autocommit": False}
        for connection in (connections[0], connections[2])
    )
    assert all(
        connection.session == {"readonly": True, "autocommit": True}
        for connection in (connections[1], connections[3])
    )
    assert all(connection.rolled_back and connection.closed for connection in (connections[0], connections[2]))
    assert all(connection.closed for connection in (connections[1], connections[3]))
    mvt_statements = [
        statement
        for statement, _parameters in connections[2].fake_cursor.executions
        if "hydro.river_timeseries ts" in statement
    ]
    assert mvt_statements
    assert all("%(run_id)s" in statement for statement in mvt_statements)
    assert all("HH24:MI:SS" in statement for statement in mvt_statements)


def test_capture_has_full_plans_two_warmups_seven_measurements_hashes_and_activity() -> None:
    document, _ = _capture(curve_reads=[3, 0, 0, *([0] * 7)], mvt_reads=[3, 0, 0, *([0] * 7)])
    curve_phase = document["queries"][0]["after"]
    mvt_phase = document["queries"][1]["after"]

    assert len(curve_phase["warmups"]) == 2
    assert len(curve_phase["measurements"]) == 7
    assert curve_phase["cold"]["shared_hit_blocks"] == 5
    assert curve_phase["cold"]["shared_read_blocks"] == 3
    assert curve_phase["cache_class"] == "warm-cache"
    assert curve_phase["result_payload"][0]["cycle_time"] == "2026-07-05T00:00:00Z"
    curve_raw = benchmark._canonical_json_bytes(curve_phase["result_payload"])
    assert curve_phase["result_sha256"] == hashlib.sha256(curve_raw).hexdigest()
    assert curve_phase["bytes"] == len(curve_raw)
    assert curve_phase["rows"] == 1
    assert mvt_phase["result_payload"] == b"\x1a\x02ok".hex()
    assert mvt_phase["result_sha256"] == hashlib.sha256(b"\x1a\x02ok").hexdigest()
    assert len(curve_phase["activity_samples"]) == 5
    assert {tuple(sample["sessions"]) for sample in curve_phase["activity_samples"]} == {()}
    assert all(sample["material_load_stable"] for sample in curve_phase["activity_samples"])


def test_warmup_continues_to_five_while_reads_remain() -> None:
    document, _ = _capture(
        curve_reads=[8, 7, 6, 5, 4, 3, *([2] * 7)],
        mvt_reads=[8, 7, 6, 5, 4, 3, *([2] * 7)],
    )
    for query in document["queries"]:
        phase = query["after"]
        assert len(phase["warmups"]) == 5
        assert len(phase["measurements"]) == 7
        assert phase["cache_class"] == "mixed-cache"


def test_source_refs_are_complete_content_hashes() -> None:
    document, _ = _capture(curve_reads=[0] * 10, mvt_reads=[0] * 10)
    expected = [
        [benchmark.CURVE_SOURCE],
        [benchmark.MVT_SOURCE, benchmark.MVT_ROUTE_SOURCE],
    ]
    for query, paths in zip(document["queries"], expected, strict=True):
        assert [ref["path"] for ref in query["source_refs"]] == [str(path.resolve()) for path in paths]
        for ref, path in zip(query["source_refs"], paths, strict=True):
            raw = path.read_bytes()
            assert ref["bytes"] == len(raw)
            assert ref["sha256"] == hashlib.sha256(raw).hexdigest()


def test_before_and_after_slices_merge_into_exact_live_evidence_contract() -> None:
    before, _ = _capture(curve_reads=[0] * 10, mvt_reads=[0] * 10, phase="before")
    after, _ = _capture(curve_reads=[0] * 10, mvt_reads=[0] * 10, phase="after")
    merged = benchmark.merge_benchmark_slices(before, after)

    normalized = live_evidence._validate_benchmarks(
        merged,
        {
            "range_start": "2026-07-05T00:00:00Z",
            "range_end": "2026-07-12T00:00:01Z",
        },
        selected_relation_names={"_hyper_3_7_chunk"},
        mutation_head_sha=live_evidence.subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip(),
    )
    assert [query["name"] for query in normalized] == ["curve", "mvt"]


def test_merge_rejects_production_identity_drift() -> None:
    before, _ = _capture(curve_reads=[0] * 10, mvt_reads=[0] * 10, phase="before")
    after, _ = _capture(curve_reads=[0] * 10, mvt_reads=[0] * 10, phase="after")
    after["queries"][0]["binding"]["bound_parameters"][0] = "drifted-basin"
    with pytest.raises(benchmark.BenchmarkCaptureError, match="identity drift"):
        benchmark.merge_benchmark_slices(before, after)


def test_activity_drift_is_preserved_not_claimed_stable() -> None:
    cursor = _FakeCursor(result_rows=[{"value": 1}], plan_reads=[0] * 10)
    session = {
        "pid": 42,
        "backend_start": datetime(2026, 7, 15, tzinfo=UTC),
        "xact_start": None,
        "query_start": datetime(2026, 7, 15, tzinfo=UTC),
        "state": "active",
        "wait_event_type": None,
        "query_signature": "a" * 32,
    }
    monitor_cursor = _FakeCursor(
        result_rows=[],
        plan_reads=[],
        activity_sessions=[[], [session], [session], [session], [session]],
    )
    connection = _FakeConnection(cursor)
    monitor = _FakeConnection(monitor_cursor)
    phase = benchmark._capture_phase(
        connection,
        monitor_connection=monitor,
        statement="SELECT 1 AS value",
        parameters=(),
        result_kind="curve",
    )
    assert [len(sample["sessions"]) for sample in phase["activity_samples"]] == [0, 1, 1, 1, 1]
    assert not any(sample["material_load_stable"] for sample in phase["activity_samples"])


def test_phase_deadline_rolls_back_and_closes_both_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor(result_rows=[{"value": 1}], plan_reads=[0] * 10)
    monitor_cursor = _FakeCursor(result_rows=[], plan_reads=[])
    connection = _FakeConnection(cursor)
    monitor = _FakeConnection(monitor_cursor)
    ticks = iter([0.0, 901.0])
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: next(ticks))

    with pytest.raises(benchmark.BenchmarkCaptureError, match="wall deadline"):
        benchmark._capture_phase(
            connection,
            monitor_connection=monitor,
            statement="SELECT 1 AS value",
            parameters=(),
            result_kind="curve",
        )

    assert connection.rolled_back and connection.closed
    assert monitor.closed


def test_rejects_credentials_in_document() -> None:
    with pytest.raises(benchmark.BenchmarkCaptureError, match="credential"):
        benchmark._reject_secrets(
            {"queries": [{"binding": {"value": "postgresql://user:password@db/nhms"}}]},
            "opaque-dsn",
        )


def test_cli_writes_canonical_mode_0600_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "phase.json"
    document = {"queries": [{"name": "curve"}, {"name": "mvt"}]}
    monkeypatch.setenv("DATABASE_URL", "opaque-dsn")
    monkeypatch.setattr(benchmark, "capture_benchmark_phase", lambda **_kwargs: document)
    args = [
        "--phase",
        "before",
        "--output",
        str(output),
        "--curve-basin-version-id",
        "basin-v1",
        "--curve-river-segment-id",
        "segment-1",
        "--curve-river-network-version-id",
        "network-v1",
        "--curve-issue-time",
        "2026-07-05T00:00:00Z",
        "--curve-scenario",
        "forecast_ifs_deterministic",
        "--mvt-run-id",
        "run-1",
        "--mvt-basin-version-id",
        "basin-v1",
        "--mvt-river-network-version-id",
        "network-v1",
        "--mvt-valid-time",
        "2026-07-06T00:00:00Z",
        "--mvt-z",
        "9",
        "--mvt-x",
        "420",
        "--mvt-y",
        "210",
    ]
    assert benchmark.main(args) == 0
    assert output.read_bytes() == benchmark._canonical_json_bytes(document)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_cli_failure_is_generic_does_not_publish_or_leak_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "absent.json"
    secret = "postgresql://user:super-secret@db/nhms"
    monkeypatch.setenv("DATABASE_URL", secret)

    def fail(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(secret)

    monkeypatch.setattr(benchmark, "capture_benchmark_phase", fail)
    args = [
        "--phase",
        "before",
        "--output",
        str(output),
        "--curve-basin-version-id",
        "b",
        "--curve-river-segment-id",
        "s",
        "--curve-river-network-version-id",
        "n",
        "--curve-issue-time",
        "2026-07-05T00:00:00Z",
        "--curve-scenario",
        "forecast_ifs_deterministic",
        "--mvt-run-id",
        "r",
        "--mvt-basin-version-id",
        "b",
        "--mvt-river-network-version-id",
        "n",
        "--mvt-valid-time",
        "2026-07-06T00:00:00Z",
        "--mvt-z",
        "9",
        "--mvt-x",
        "1",
        "--mvt-y",
        "1",
    ]
    assert benchmark.main(args) == 2
    marker = json.loads(output.read_text(encoding="utf-8"))
    assert marker["schema_version"] == "2.0"
    assert marker["outcome"] == "failed"
    assert marker["provenance_state"] == "unavailable"
    assert secret not in capsys.readouterr().err


def test_partial_connection_acquisition_closes_primary() -> None:
    primary = _FakeConnection(_FakeCursor(result_rows=[], plan_reads=[]))
    calls = 0

    def connect(_database_url: str) -> _FakeConnection:
        nonlocal calls
        calls += 1
        if calls == 1:
            return primary
        raise RuntimeError("monitor unavailable")

    with pytest.raises(RuntimeError, match="monitor unavailable"):
        benchmark.capture_benchmark_phase(**_inputs(), connect=connect)
    assert primary.closed is True


@pytest.mark.parametrize("hardlink", [False, True])
def test_after_output_alias_preserves_before_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hardlink: bool
) -> None:
    before = tmp_path / "before.json"
    before.write_text('{"queries":[]}\n', encoding="utf-8")
    output = before
    if hardlink:
        output = tmp_path / "output.json"
        output.hardlink_to(before)
    original = before.read_bytes()
    monkeypatch.setenv("DATABASE_URL", "opaque-test-dsn")
    args = [
        "--phase", "after", "--output", str(output), "--before-path", str(before),
        "--curve-basin-version-id", "b", "--curve-river-segment-id", "s",
        "--curve-river-network-version-id", "n", "--curve-issue-time", "2026-07-05T00:00:00Z",
        "--curve-scenario", "forecast_ifs_deterministic", "--mvt-run-id", "r",
        "--mvt-basin-version-id", "b", "--mvt-river-network-version-id", "n",
        "--mvt-valid-time", "2026-07-06T00:00:00Z", "--mvt-z", "9",
        "--mvt-x", "1", "--mvt-y", "1",
    ]
    assert benchmark.main(args) == 2
    assert before.read_bytes() == original


def test_secret_assignment_in_benign_string_is_rejected() -> None:
    with pytest.raises(benchmark.BenchmarkCaptureError, match="credential") as caught:
        benchmark._reject_secrets(
            {"note": "status=ok token=do-not-echo"}, "opaque-test-dsn"
        )
    assert "do-not-echo" not in str(caught.value)


def test_result_row_ceiling_fails_before_publication() -> None:
    cursor = _FakeCursor(result_rows=[{"value": 1}], plan_reads=[])
    cursor.fetchall = lambda: [{"value": 1}] * (benchmark.MAX_RESULT_ROWS + 1)  # type: ignore[method-assign]
    with pytest.raises(benchmark.BenchmarkCaptureError, match="row ceiling"):
        benchmark._fetch_all(
            cursor,
            deadline=benchmark._Deadline(),
            label="oversized result",
        )
