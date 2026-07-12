from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from workers.output_parser import (
    HydroRunContext,
    OutputParser,
    OutputParserConfig,
    OutputParsingError,
    RiverSegmentOrder,
    RiverTimeseriesRow,
)


class FakeOutputRepository:
    def __init__(self, *, context: HydroRunContext, segments: tuple[RiverSegmentOrder, ...]) -> None:
        self.context = context
        self.segments = segments
        self.rows: dict[tuple[str, str, str, str, datetime], RiverTimeseriesRow] = {}
        self.qc_results: list[Any] = []
        self.statuses: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def load_run_context(self, run_id: str) -> HydroRunContext:
        assert run_id == self.context.run_id
        return self.context

    def load_river_segments(self, river_network_version_id: str) -> tuple[RiverSegmentOrder, ...]:
        assert river_network_version_id == self.context.river_network_version_id
        return self.segments

    def upsert_river_timeseries(self, rows: tuple[RiverTimeseriesRow, ...], *, batch_size: int) -> None:
        assert batch_size > 0
        replacement_keys = {
            (row.run_id, row.river_network_version_id, row.variable)
            for row in rows
        }
        self.rows = {
            key: row
            for key, row in self.rows.items()
            if (key[0], key[1], key[3]) not in replacement_keys
        }
        for row in rows:
            key = (row.run_id, row.river_network_version_id, row.river_segment_id, row.variable, row.valid_time)
            self.rows[key] = row

    def insert_qc_result(self, record: Any) -> dict[str, Any]:
        self.qc_results.append(record)
        return {"qc_id": len(self.qc_results)}

    def mark_run_parsed(self, run_id: str) -> dict[str, Any]:
        assert run_id == self.context.run_id
        self.statuses.append("parsed")
        return {}

    def mark_run_failed(self, run_id: str, error_code: str, error_message: str) -> dict[str, Any]:
        assert run_id == self.context.run_id
        self.statuses.append("failed")
        self.failures.append((error_code, error_message))
        return {}


def test_parse_csv_converts_units_and_writes_q_down(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(tmp_path)
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown",
        ("time,seg_a,seg_b\n2026-05-01T00:00:00Z,86400,172800\n2026-05-02T00:00:00Z,0,43200\n").encode("utf-8"),
    )

    result = parser.parse_run("run_001")

    assert result.status == "parsed"
    assert result.rows_written == 4
    assert repository.statuses == ["parsed"]
    rows = list(repository.rows.values())
    assert {row.variable for row in rows} == {"q_down"}
    assert {row.unit for row in rows} == {"m3/s"}
    assert repository.rows[_row_key("seg_a", "2026-05-01T00:00:00Z")].value == pytest.approx(1.0)
    assert repository.rows[_row_key("seg_b", "2026-05-01T00:00:00Z")].value == pytest.approx(2.0)
    assert repository.rows[_row_key("seg_a", "2026-05-02T00:00:00Z")].lead_time_hours == 24
    assert repository.qc_results[-1].passed is True
    assert repository.qc_results[-1].target_type == "river_timeseries"
    assert repository.qc_results[-1].checks_json["range_check"]["max_value"] == pytest.approx(2.0)


def test_parse_dat_relative_minutes_from_run_start(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(tmp_path)
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown",
        "0 86400 172800\n60 43200 0\n".encode("utf-8"),
    )

    parser.parse_run("run_001")

    first_time = _dt("2026-05-01T00:00:00Z")
    second_time = _dt("2026-05-01T01:00:00Z")
    assert repository.rows[("run_001", "rivnet_v1", "seg_a", "q_down", first_time)].lead_time_hours == 0
    assert repository.rows[("run_001", "rivnet_v1", "seg_a", "q_down", second_time)].lead_time_hours == 1


def test_parse_qhh_time_min_header_is_relative_to_run_start(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(tmp_path)
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown.csv",
        "0 2 20260501\nTime_min X1 X2\n0 86400 172800\n60 43200 0\n".encode("utf-8"),
    )

    parser.parse_run("run_001")

    first_time = _dt("2026-05-01T00:00:00Z")
    second_time = _dt("2026-05-01T01:00:00Z")
    assert repository.rows[("run_001", "rivnet_v1", "seg_a", "q_down", first_time)].lead_time_hours == 0
    assert repository.rows[("run_001", "rivnet_v1", "seg_a", "q_down", second_time)].lead_time_hours == 1


def test_parse_dat_long_relative_minutes_stay_relative_to_run_start(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(tmp_path)
    minutes = 367 * 24 * 60
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown",
        f"{minutes} 86400 172800\n".encode("utf-8"),
    )

    parser.parse_run("run_001")

    expected_time = _dt("2027-05-03T00:00:00Z")
    assert repository.rows[("run_001", "rivnet_v1", "seg_a", "q_down", expected_time)].lead_time_hours == 8808


def test_parse_qhh_time_min_absolute_minutes_when_header_declares_unix_minutes(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(tmp_path)
    absolute_minutes = int(_dt("2026-05-01T03:00:00Z").timestamp() / 60)
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown.csv",
        f"Time_min,seg_a,seg_b\n{absolute_minutes},86400,172800\n".encode("utf-8"),
    )

    parser.parse_run("run_001")

    expected_time = _dt("2026-05-01T03:00:00Z")
    assert repository.rows[("run_001", "rivnet_v1", "seg_a", "q_down", expected_time)].lead_time_hours == 3


def test_column_mismatch_marks_run_failed_without_writing_rows(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(tmp_path)
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown",
        "time,seg_a,seg_b,seg_c\n2026-05-01T00:00:00Z,1,2,3\n".encode("utf-8"),
    )

    with pytest.raises(OutputParsingError) as exc_info:
        parser.parse_run("run_001")

    assert exc_info.value.error_code == "COLUMN_COUNT_MISMATCH"
    assert repository.rows == {}
    assert repository.statuses == ["failed"]
    assert "file has 3 columns" in repository.failures[0][1]


@pytest.mark.parametrize("bad_value", ["NaN", "Inf", "-Infinity"])
def test_non_finite_flow_marks_run_failed_without_writing_rows(tmp_path: Path, bad_value: str) -> None:
    store, parser, repository = _build_parser(tmp_path)
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown",
        f"time,seg_a,seg_b\n2026-05-01T00:00:00Z,{bad_value},2\n".encode("utf-8"),
    )

    with pytest.raises(OutputParsingError) as exc_info:
        parser.parse_run("run_001")

    assert exc_info.value.error_code == "NON_FINITE_FLOW"
    assert repository.rows == {}
    assert repository.qc_results == []
    assert repository.statuses == ["failed"]
    assert repository.failures[0][0] == "NON_FINITE_FLOW"


def test_qc_failures_are_advisory_and_flag_inserted_rows(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(tmp_path, max_flow_m3s=1.0)
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown",
        "time,seg_a,seg_b\n2026-05-01T00:00:00Z,-86400,172800\n".encode("utf-8"),
    )

    result = parser.parse_run("run_001")

    assert result.qc_passed is False
    assert repository.statuses == ["parsed"]
    assert {row.quality_flag for row in repository.rows.values()} == {"qc_warning"}
    qc = repository.qc_results[-1]
    assert qc.severity == "warning"
    assert qc.checks_json["non_negative"]["failed_count"] == 1
    assert qc.checks_json["range_check"]["outlier_count"] == 1


def test_reparse_upserts_existing_timeseries_rows(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(
        tmp_path,
        segments=(RiverSegmentOrder("seg_a", "rivnet_v1", 1),),
    )
    key = "runs/run_001/output/demo.rivqdown"
    store.write_bytes_atomic(key, "time,seg_a\n2026-05-01T00:00:00Z,86400\n".encode("utf-8"))
    parser.parse_run("run_001")

    store.write_bytes_atomic(key, "time,seg_a\n2026-05-01T00:00:00Z,172800\n".encode("utf-8"))
    parser.parse_run("run_001")

    assert len(repository.rows) == 1
    assert next(iter(repository.rows.values())).value == pytest.approx(2.0)
    assert repository.statuses == ["parsed", "parsed"]


def test_reparse_replaces_stale_timeseries_window(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(
        tmp_path,
        segments=(RiverSegmentOrder("seg_a", "rivnet_v1", 1),),
    )
    key = "runs/run_001/output/demo.rivqdown"
    store.write_bytes_atomic(key, "time,seg_a\n2026-05-01T00:00:00Z,86400\n".encode("utf-8"))
    parser.parse_run("run_001")

    store.write_bytes_atomic(key, "time,seg_a\n2026-05-01T01:00:00Z,172800\n".encode("utf-8"))
    parser.parse_run("run_001")

    assert len(repository.rows) == 1
    assert _row_key("seg_a", "2026-05-01T00:00:00Z") not in repository.rows
    assert repository.rows[_row_key("seg_a", "2026-05-01T01:00:00Z")].value == pytest.approx(2.0)


def test_output_parser_from_env_requires_database_url_without_db_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", raising=False)
    monkeypatch.delenv("NHMS_OUTPUT_PARSER_DB_FREE", raising=False)

    with pytest.raises(OutputParsingError) as exc_info:
        OutputParser.from_env()

    assert exc_info.value.error_code == "DATABASE_URL_MISSING"


def test_output_parser_db_free_writes_object_store_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "object-store"
    workspace_root = tmp_path / "workspace"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")

    model_package = object_root / "models" / "model_001" / "v1" / "package"
    model_package.mkdir(parents=True)
    (model_package / "demo.sp.riv").write_text(
        "2\t6\n"
        "Index\tDown\tType\tSlope\tLength\tBC\n"
        "1\t2\t2\t0.1\t100\t0\n"
        "2\t0\t2\t0.2\t200\t0\n"
        "1\t2\n"
        "Index\tDepth\n"
        "1\t6\n",
        encoding="utf-8",
    )
    output_dir = object_root / "runs" / "run_001" / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "demo.rivqdown").write_text(
        "time,seg_a,seg_b\n2026-05-01T00:00:00Z,86400,172800\n",
        encoding="utf-8",
    )
    manifest = {
        "run_id": "run_001",
        "run_type": "forecast",
        "source_id": "gfs",
        "cycle_time": "2026-05-01T00:00:00Z",
        "start_time": "2026-05-01T00:00:00Z",
        "model": {
            "model_id": "model_001",
            "basin_version_id": "basin_v1",
            "river_network_version_id": "rivnet_v1",
            "model_package_uri": "s3://nhms/models/model_001/v1/package/",
            "project_name": "demo",
        },
        "outputs": {
            "output_uri": "s3://nhms/runs/run_001/output/",
        },
        "identity": {"cycle_id": "gfs_2026050100"},
    }
    manifest_path = object_root / "runs" / "run_001" / "input" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    parser = OutputParser.from_env()
    result = parser.parse_run("run_001")

    assert result.status == "parsed"
    assert result.rows_written == 2
    q_down_path = object_root / "runs" / "run_001" / "output" / "parsed" / "q_down.jsonl"
    rows = [line for line in q_down_path.read_text(encoding="utf-8").splitlines() if line]
    payloads = [json.loads(line) for line in rows]
    assert [row["river_segment_id"] for row in payloads] == [
        "model_001_shud_riv_000001",
        "model_001_shud_riv_000002",
    ]
    assert payloads[0]["value"] == pytest.approx(1.0)
    parse_result = json.loads(
        (object_root / "runs" / "run_001" / "output" / "parsed" / "parse_result.json").read_text(encoding="utf-8")
    )
    assert parse_result["status"] == "parsed"
    assert parse_result["rows_written"] == 2


def test_compressed_chunk_guard_error_sets_dedicated_error_code(tmp_path: Path) -> None:
    """E3: ``CompressedChunkGuardError`` from the guard becomes
    ``OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED`` on ``hydro_run.error_code`` and
    the exception re-raises so the CLI (and callers) see the structured
    exception rather than a generic runtime bucket.
    """
    from packages.common.timescale_write_guard import CompressedChunkGuardError

    store, parser, repository = _build_parser(tmp_path)
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown",
        ("time,seg_a,seg_b\n2026-05-01T00:00:00Z,86400,172800\n").encode("utf-8"),
    )

    original_upsert = repository.upsert_river_timeseries

    def _raise_guard(rows: Any, *, batch_size: int) -> None:
        # Preserve batch_size handshake so the assertion in the fake still fires.
        del rows, batch_size
        raise CompressedChunkGuardError(
            "guard raised: chunk _hyper_1_1_chunk in hydro.river_timeseries"
        )

    repository.upsert_river_timeseries = _raise_guard  # type: ignore[method-assign]

    with pytest.raises(CompressedChunkGuardError):
        parser.parse_run("run_001")

    assert repository.statuses == ["failed"]
    assert repository.failures[0][0] == "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED"
    assert "hydro.river_timeseries" in repository.failures[0][1]
    # Restore for hygiene in case the fixture is reused.
    repository.upsert_river_timeseries = original_upsert  # type: ignore[method-assign]


def test_s3_output_uri_must_match_configured_bucket_and_prefix(tmp_path: Path) -> None:
    store, parser, repository = _build_parser(tmp_path, object_store_prefix="s3://nhms/prod")
    store.write_bytes_atomic(
        "runs/run_001/output/demo.rivqdown",
        "time,seg_a,seg_b\n2026-05-01T00:00:00Z,86400,172800\n".encode("utf-8"),
    )
    repository.context = HydroRunContext(
        **{
            **repository.context.__dict__,
            "output_uri": "s3://nhms/prod/runs/run_001/output/",
        }
    )

    result = parser.parse_run("run_001")

    assert result.status == "parsed"
    assert repository.statuses == ["parsed"]

    for bad_uri in ("s3://other/prod/runs/run_001/output/", "s3://nhms/dev/runs/run_001/output/"):
        repository.context = HydroRunContext(
            **{
                **repository.context.__dict__,
                "output_uri": bad_uri,
            }
        )
        repository.statuses.clear()
        with pytest.raises(OutputParsingError) as exc_info:
            parser.parse_run("run_001")
        assert exc_info.value.error_code == "OUTPUT_URI_INVALID"
        assert repository.statuses == ["failed"]


def _build_parser(
    tmp_path: Path,
    *,
    max_flow_m3s: float = 100_000.0,
    object_store_prefix: str = "s3://nhms",
    segments: tuple[RiverSegmentOrder, ...] = (
        RiverSegmentOrder("seg_a", "rivnet_v1", 1),
        RiverSegmentOrder("seg_b", "rivnet_v1", 2),
    ),
) -> tuple[LocalObjectStore, OutputParser, FakeOutputRepository]:
    object_root = tmp_path / "object-store"
    store = LocalObjectStore(object_root, object_store_prefix)
    context = HydroRunContext(
        run_id="run_001",
        model_id="model_001",
        basin_version_id="basin_v1",
        river_network_version_id="rivnet_v1",
        source_id="gfs",
        cycle_id="gfs_2026050100",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        start_time=_dt("2026-05-01T00:00:00Z"),
        output_uri=f"{object_store_prefix.rstrip('/')}/runs/run_001/output/",
    )
    repository = FakeOutputRepository(context=context, segments=segments)
    parser = OutputParser(
        config=OutputParserConfig(
            object_store_root=object_root,
            object_store_prefix=object_store_prefix,
            max_flow_m3s=max_flow_m3s,
            batch_size=2,
        ),
        repository=repository,
        object_store=store,
    )
    return store, parser, repository


def _row_key(segment_id: str, valid_time: str) -> tuple[str, str, str, str, datetime]:
    return ("run_001", "rivnet_v1", segment_id, "q_down", _dt(valid_time))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
