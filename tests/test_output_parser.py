from __future__ import annotations

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
