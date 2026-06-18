from __future__ import annotations

import json
import re
import sys
import types
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import scripts.audit_return_period_indexes as audit_script
from scripts.audit_return_period_indexes import (
    INDEX_INVENTORY_SQL,
    INDEX_USAGE_SQL,
    ROOT_RELATION_SIZE_SQL,
    TIMESCALE_CHUNK_INDEX_SIZE_SQL,
    TIMESCALE_CHUNK_INDEX_USAGE_SQL,
    TIMESCALE_CHUNK_SIZE_SQL,
    ProbeInputs,
    ReturnPeriodIndexAuditError,
    build_report,
    classify_indexes,
    collect_catalog_evidence,
    generate_hot_path_probes,
    generate_manual_maintenance_sql,
    render_report_json,
    write_output_file,
)


def test_null_partial_indexes_are_drop_candidates_without_executing_generated_ddl() -> None:
    connection = _FakeConnection(
        {
            ROOT_RELATION_SIZE_SQL: [{"table_bytes": 10, "indexes_bytes": 20, "total_bytes": 30}],
            INDEX_INVENTORY_SQL: [
                _index_row(
                    "return_period_result_null_return_period_run_idx",
                    "CREATE INDEX return_period_result_null_return_period_run_idx "
                    "ON flood.return_period_result (run_id) WHERE return_period IS NULL",
                    predicate="return_period IS NULL",
                    is_partial=True,
                ),
                _index_row(
                    "return_period_result_null_warning_level_run_idx",
                    "CREATE INDEX return_period_result_null_warning_level_run_idx "
                    "ON flood.return_period_result (run_id) WHERE warning_level IS NULL",
                    predicate="warning_level IS NULL",
                    is_partial=True,
                ),
            ],
            INDEX_USAGE_SQL: [{"index_name": "return_period_result_null_return_period_run_idx", "idx_scan": 0}],
            TIMESCALE_CHUNK_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_USAGE_SQL: [],
        }
    )

    catalog = collect_catalog_evidence(connection)
    report = build_report(catalog, connection_mode="readonly", manual_artifact_requested=True)
    classifications = {item["index_name"]: item for item in report["classifications"]}
    manual_sql = generate_manual_maintenance_sql(report["classifications"])

    assert classifications["return_period_result_null_return_period_run_idx"]["decision"] == "investigate"
    assert classifications["return_period_result_null_return_period_run_idx"]["operator_candidate"] == "drop"
    assert classifications["return_period_result_null_warning_level_run_idx"]["operator_candidate"] == "drop"
    assert report["execution_guardrails"]["destructive_ddl_executed"] is False
    assert report["execution_guardrails"]["apply_mode_supported"] is False
    assert not any("DROP INDEX" in sql for sql in connection.executed_sql)
    assert "-- DROP INDEX IF EXISTS flood.\"return_period_result_null_return_period_run_idx\";" in manual_sql
    assert "\nDROP INDEX" not in manual_sql


def test_known_indexes_map_to_migrations_and_hot_paths() -> None:
    rows = [
        _index_row("return_period_result_summary_idx"),
        _index_row("return_period_result_ranking_idx"),
        _index_row("return_period_result_valid_time_ranking_idx"),
        _index_row("return_period_result_timeline_idx"),
        _index_row("return_period_result_map_idx"),
        _index_row("return_period_result_valid_time_discovery_idx"),
        _index_row("return_period_result_mvt_selected_identity_lookup_idx"),
        _index_row("return_period_result_mvt_selected_identity_valid_time_discovery_idx"),
        _index_row("return_period_result_run_quality_idx"),
    ]

    classified = {item["index_name"]: item for item in classify_indexes(rows)}

    assert classified["return_period_result_summary_idx"]["migration"] == "000015"
    assert classified["return_period_result_summary_idx"]["hot_paths"] == ["flood-alert summary"]
    assert classified["return_period_result_ranking_idx"]["hot_paths"] == ["ranking/segments"]
    assert classified["return_period_result_timeline_idx"]["hot_paths"] == ["timeline"]
    assert classified["return_period_result_map_idx"]["hot_paths"] == ["GeoJSON fallback tile"]
    assert classified["return_period_result_valid_time_discovery_idx"]["migration"] == "000020"
    assert "valid-time discovery" in classified["return_period_result_valid_time_discovery_idx"]["hot_paths"]
    assert classified["return_period_result_mvt_selected_identity_lookup_idx"]["migration"] == "000021"
    assert classified["return_period_result_mvt_selected_identity_lookup_idx"]["hot_paths"] == ["MVT selected identity"]
    assert classified["return_period_result_mvt_selected_identity_valid_time_discovery_idx"]["migration"] == "000021"
    assert "valid-time discovery" in classified[
        "return_period_result_mvt_selected_identity_valid_time_discovery_idx"
    ]["hot_paths"]
    assert classified["return_period_result_run_quality_idx"]["migration"] == "000031"
    assert classified["return_period_result_run_quality_idx"]["decision"] == "investigate"
    assert "TilePublisher readiness" in classified["return_period_result_run_quality_idx"]["hot_paths"]
    assert "run_product_quality" in classified["return_period_result_run_quality_idx"]["replacement"]


def test_hot_path_probes_are_parameterized_for_all_documented_surfaces() -> None:
    probes = generate_hot_path_probes(
        ProbeInputs(
            run_id="run-secret-should-not-be-in-sql",
            duration="6h",
            valid_time="2026-06-18T12:00:00Z",
            basin_version_id="basin-v1",
            river_network_version_id="network-v1",
            segment_id="seg-1",
            min_lon=91.0,
            min_lat=31.0,
            max_lon=92.0,
            max_lat=32.0,
            limit=25,
        )
    )

    by_name = {probe["name"]: probe for probe in probes}

    assert set(by_name) == {
        "flood-alert-summary-peak",
        "flood-alert-summary-valid-time",
        "ranking-peak",
        "ranking-valid-time",
        "segments-peak",
        "segments-valid-time",
        "timeline",
        "geojson-fallback-tile",
        "mvt-selected-identity",
        "valid-time-discovery-selected",
        "valid-time-discovery-unselected",
        "tilepublisher-run-readiness",
        "latest-ready-run-quality",
    }
    for probe in probes:
        assert "EXPLAIN (ANALYZE, BUFFERS)" in probe["sql"]
        assert ":run_id" in probe["sql"] or probe["name"] == "valid-time-discovery-unselected"
        assert "run-secret-should-not-be-in-sql" not in probe["sql"]
    assert ":duration" in by_name["geojson-fallback-tile"]["sql"]
    assert ":valid_time" in by_name["mvt-selected-identity"]["sql"]
    assert ":basin_version_id" in by_name["valid-time-discovery-selected"]["sql"]
    assert ":river_network_version_id" in by_name["timeline"]["sql"]
    assert ":segment_id" in by_name["timeline"]["sql"]
    assert ":min_lon" in by_name["geojson-fallback-tile"]["sql"]
    assert "r.max_over_window = true" in by_name["tilepublisher-run-readiness"]["sql"]
    assert "r.return_period IS NOT NULL" in by_name["tilepublisher-run-readiness"]["sql"]
    assert "r.warning_level IS NOT NULL" in by_name["tilepublisher-run-readiness"]["sql"]
    assert "flood.run_product_quality" in by_name["latest-ready-run-quality"]["sql"]


def test_hot_path_probes_include_required_sql_fragments_for_each_surface() -> None:
    by_name = {probe["name"]: probe for probe in generate_hot_path_probes()}
    expected_fragments = {
        "flood-alert-summary-peak": [
            "warning_level IS NOT NULL",
            "max_over_window = :max_over_window",
            "quality_flag = ANY(:usable_flags)",
        ],
        "flood-alert-summary-valid-time": [
            "valid_time = :valid_time",
            "max_over_window = false",
            "warning_level IS NOT NULL",
        ],
        "ranking-peak": [
            "r.max_over_window = :max_over_window",
            "ORDER BY r.return_period DESC NULLS LAST",
        ],
        "ranking-valid-time": [
            "r.valid_time = :valid_time",
            "r.max_over_window = false",
            "ORDER BY r.return_period DESC NULLS LAST",
        ],
        "segments-peak": [
            "r.max_over_window = :max_over_window",
            "LIMIT :limit OFFSET :offset",
        ],
        "segments-valid-time": [
            "r.valid_time = :valid_time",
            "r.max_over_window = false",
            "LIMIT :limit OFFSET :offset",
        ],
        "timeline": [
            "river_segment_id = :segment_id",
            "river_network_version_id = :river_network_version_id",
            "ORDER BY valid_time",
        ],
        "geojson-fallback-tile": [
            "r.duration = :duration",
            "r.valid_time = :valid_time",
            "ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat",
        ],
        "mvt-selected-identity": [
            "r.basin_version_id = :basin_version_id",
            "r.river_network_version_id = :river_network_version_id",
            "r.max_over_window = false",
        ],
        "valid-time-discovery-selected": [
            "run_id = :run_id",
            "basin_version_id = :basin_version_id",
            "ORDER BY valid_time DESC",
        ],
        "valid-time-discovery-unselected": [
            "duration = :duration",
            "max_over_window = false",
            "ORDER BY valid_time DESC",
        ],
        "tilepublisher-run-readiness": [
            "r.run_id = :run_id",
            "r.max_over_window = true",
            "SUM(CASE WHEN r.return_period IS NOT NULL THEN 1 ELSE 0 END)",
            "SUM(CASE WHEN r.warning_level IS NOT NULL THEN 1 ELSE 0 END)",
        ],
        "latest-ready-run-quality": [
            "JOIN flood.run_product_quality product_quality",
            "product_quality.quality_state = 'ready'",
        ],
    }

    for name, fragments in expected_fragments.items():
        sql = by_name[name]["sql"]
        for fragment in fragments:
            assert fragment in sql, name


def test_probe_placeholders_are_bound_per_probe() -> None:
    placeholder_re = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")

    for probe in generate_hot_path_probes():
        placeholders = set(placeholder_re.findall(probe["sql"]))
        assert placeholders == set(probe["required_bindings"])
        assert placeholders <= set(probe["bindings"])
        assert placeholders <= set(probe["sample_bindings"])


def test_valid_time_branch_probes_do_not_use_optional_or_collapse() -> None:
    by_name = {probe["name"]: probe for probe in generate_hot_path_probes()}
    for name in ("flood-alert-summary-valid-time", "ranking-valid-time", "segments-valid-time"):
        sql = by_name[name]["sql"]
        assert "valid_time = :valid_time" in sql
        assert "max_over_window = false" in sql
        assert ":valid_time IS NULL" not in sql


def test_connection_modes_never_enable_destructive_execution() -> None:
    catalog = _catalog_with_indexes([_index_row("return_period_result_summary_idx")])

    readonly_report = build_report(catalog, connection_mode="readonly", manual_artifact_requested=False)
    writer_report = build_report(catalog, connection_mode="writer", manual_artifact_requested=False)

    assert readonly_report["execution_guardrails"]["destructive_ddl_executed"] is False
    assert readonly_report["execution_guardrails"]["manual_artifact_requested"] is False
    assert "Readonly/audit" in readonly_report["execution_guardrails"]["writer_mode_note"]
    assert writer_report["execution_guardrails"]["destructive_ddl_executed"] is False
    assert writer_report["execution_guardrails"]["apply_mode_supported"] is False
    assert writer_report["execution_guardrails"]["manual_artifact_requested"] is False
    assert "do not bypass approval" in writer_report["execution_guardrails"]["writer_mode_note"]


def test_output_path_safety_rejects_existing_paths_and_cleans_partial_write(tmp_path: Path) -> None:
    existing = tmp_path / "report.json"
    existing.write_text("old\n", encoding="utf-8")

    with pytest.raises(ReturnPeriodIndexAuditError) as existing_error:
        write_output_file(existing, "new\n")
    assert existing_error.value.error_code == "OUTPUT_EXISTS"
    assert existing.read_text(encoding="utf-8") == "old\n"

    target = tmp_path / "partial.json"

    def failing_writer(path: Path, content: str) -> None:
        path.write_text(content[:3], encoding="utf-8")
        raise OSError("simulated disk failure password=supersecret")

    with pytest.raises(ReturnPeriodIndexAuditError) as write_error:
        write_output_file(target, '{"status":"success"}\n', writer=failing_writer)
    assert write_error.value.error_code == "OUTPUT_WRITE_FAILED"
    assert "supersecret" not in str(write_error.value)
    assert "[redacted]" in str(write_error.value)
    assert not target.exists()
    assert list(tmp_path.glob(".partial.json.tmp-*")) == []


def test_output_exists_error_redacts_credential_shaped_path(tmp_path: Path) -> None:
    secret_dir = tmp_path / "postgresql://operator:supersecret@db.example/nhms"
    existing = secret_dir / "report.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("old\n", encoding="utf-8")

    with pytest.raises(ReturnPeriodIndexAuditError) as error:
        write_output_file(existing, "new\n")

    assert error.value.error_code == "OUTPUT_EXISTS"
    assert "supersecret" not in str(error.value)
    assert "operator" not in str(error.value)
    assert "[redacted]" in str(error.value)
    assert existing.read_text(encoding="utf-8") == "old\n"


def test_output_parent_creation_failure_is_redacted_and_wrapped(tmp_path: Path) -> None:
    parent_file = tmp_path / "postgresql://operator:supersecret@db.example"
    parent_file.parent.mkdir(parents=True, exist_ok=True)
    parent_file.write_text("not a directory\n", encoding="utf-8")
    target = parent_file / "report.json"

    with pytest.raises(ReturnPeriodIndexAuditError) as error:
        write_output_file(target, "{}\n")

    assert error.value.error_code == "OUTPUT_WRITE_FAILED"
    assert "supersecret" not in str(error.value)
    assert "operator" not in str(error.value)
    assert "[redacted]" in str(error.value)
    assert not target.exists()


def test_cli_write_failure_stderr_redacts_credentials(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret_dir = tmp_path / "postgresql://operator:supersecret@db.example/nhms"
    report_path = secret_dir / "report.json"
    original_writer = audit_script.write_output_file
    connection = _FakeConnection(
        {
            ROOT_RELATION_SIZE_SQL: [{"table_bytes": 100, "indexes_bytes": 50, "total_bytes": 150}],
            INDEX_INVENTORY_SQL: [_index_row("return_period_result_summary_idx")],
            INDEX_USAGE_SQL: [{"index_name": "return_period_result_summary_idx", "idx_scan": 42}],
            TIMESCALE_CHUNK_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_USAGE_SQL: [],
        }
    )
    fake_psycopg2 = types.SimpleNamespace(connect=lambda *args, **kwargs: connection)
    fake_extras = types.SimpleNamespace(RealDictCursor=object)

    def failing_write(path: Path, content: str, *, overwrite: bool = False) -> None:
        original_writer(
            path,
            content,
            overwrite=overwrite,
            writer=lambda temp_path, payload: (_ for _ in ()).throw(
                OSError(f"cannot write {temp_path} password=supersecret")
            ),
        )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
        monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
        monkeypatch.setattr(audit_script, "write_output_file", failing_write)
        with pytest.raises(SystemExit) as exit_error:
            audit_script._run_cli(
                [
                    "--database-url",
                    "postgresql://operator:supersecret@db.example:5432/nhms",
                    "--report-out",
                    str(report_path),
                ]
            )

    captured = capsys.readouterr()
    assert exit_error.value.code == 2
    assert "OUTPUT_WRITE_FAILED" in captured.err
    assert "supersecret" not in captured.err
    assert "operator" not in captured.err
    assert "[redacted]" in captured.err


@pytest.mark.parametrize("overwrite_arg", [[], ["--overwrite"]])
def test_cli_rejects_same_report_and_manual_sql_path_before_db_access(
    overwrite_arg: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "same.sql"
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DB access should not happen"))
    )
    fake_extras = types.SimpleNamespace(RealDictCursor=object)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
        monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
        with pytest.raises(SystemExit) as exit_error:
            audit_script._run_cli(
                [
                    "--database-url",
                    "postgresql://operator:supersecret@db.example:5432/nhms",
                    "--report-out",
                    str(output),
                    "--manual-sql-out",
                    str(output),
                    *overwrite_arg,
                ]
            )

    captured = capsys.readouterr()
    assert exit_error.value.code == 2
    assert "OUTPUT_PATH_CONFLICT" in captured.err
    assert "same.sql" in captured.err
    assert "supersecret" not in captured.err
    assert not output.exists()


def test_timescale_metadata_failure_keeps_root_table_evidence() -> None:
    connection = _FakeConnection(
        {
            ROOT_RELATION_SIZE_SQL: [{"table_bytes": 100, "indexes_bytes": 50, "total_bytes": 150}],
            INDEX_INVENTORY_SQL: [_index_row("return_period_result_summary_idx")],
            INDEX_USAGE_SQL: [{"index_name": "return_period_result_summary_idx", "idx_scan": 42}],
            TIMESCALE_CHUNK_SIZE_SQL: RuntimeError("timescaledb_information.chunks missing password=supersecret"),
            TIMESCALE_CHUNK_INDEX_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_USAGE_SQL: [],
        }
    )

    catalog = collect_catalog_evidence(connection)

    assert catalog["root_relation"]["available"] is True
    assert catalog["root_relation"]["rows"][0]["table_bytes"] == 100
    assert catalog["index_inventory"]["rows"][0]["indexrelname"] == "return_period_result_summary_idx"
    assert catalog["timescale_chunks"]["available"] is False
    assert "supersecret" not in catalog["timescale_chunks"]["unavailable_reason"]
    assert catalog["timescale_chunk_indexes"]["available"] is True
    assert connection.rollback_count == 1


def test_timescale_chunk_sections_include_usage_and_truncation_metadata() -> None:
    connection = _FakeConnection(
        {
            ROOT_RELATION_SIZE_SQL: [{"table_bytes": 100, "indexes_bytes": 50, "total_bytes": 150}],
            INDEX_INVENTORY_SQL: [_index_row("return_period_result_summary_idx")],
            INDEX_USAGE_SQL: [{"index_name": "return_period_result_summary_idx", "idx_scan": 42}],
            TIMESCALE_CHUNK_SIZE_SQL: [
                {
                    "chunk_schema": "_timescaledb_internal",
                    "chunk_name": "_hyper_1_1_chunk",
                    "chunk_total_bytes": 200,
                    "audit_total_rows": 250,
                }
            ],
            TIMESCALE_CHUNK_INDEX_SIZE_SQL: [
                {
                    "chunk_schema": "_timescaledb_internal",
                    "chunk_name": "_hyper_1_1_chunk",
                    "chunk_index_name": "_hyper_1_1_chunk_idx",
                    "chunk_index_bytes": 80,
                    "audit_total_rows": 1,
                }
            ],
            TIMESCALE_CHUNK_INDEX_USAGE_SQL: [
                {
                    "chunk_schema": "_timescaledb_internal",
                    "chunk_name": "_hyper_1_1_chunk",
                    "chunk_index_name": "_hyper_1_1_chunk_idx",
                    "idx_scan": 7,
                    "idx_tup_read": 11,
                    "idx_tup_fetch": 13,
                    "audit_total_rows": 600,
                }
            ],
        }
    )

    catalog = collect_catalog_evidence(connection)

    assert catalog["timescale_chunks"]["total_rows"] == 250
    assert catalog["timescale_chunks"]["observed_rows"] == 1
    assert catalog["timescale_chunks"]["row_limit"] == 200
    assert catalog["timescale_chunks"]["truncated"] is True
    assert catalog["timescale_chunk_indexes"]["total_rows"] == 1
    assert catalog["timescale_chunk_indexes"]["truncated"] is False
    assert catalog["timescale_chunk_index_usage"]["available"] is True
    assert catalog["timescale_chunk_index_usage"]["rows"][0]["idx_scan"] == 7
    assert catalog["timescale_chunk_index_usage"]["total_rows"] == 600
    assert catalog["timescale_chunk_index_usage"]["truncated"] is True


def test_empty_live_target_evidence_fails_closed_and_writes_no_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_out = tmp_path / "report.json"
    manual_out = tmp_path / "manual.sql"
    connection = _FakeConnection(
        {
            ROOT_RELATION_SIZE_SQL: [],
            INDEX_INVENTORY_SQL: [],
            INDEX_USAGE_SQL: [],
            TIMESCALE_CHUNK_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_USAGE_SQL: [],
        }
    )
    fake_psycopg2 = types.SimpleNamespace(connect=lambda *args, **kwargs: connection)
    fake_extras = types.SimpleNamespace(RealDictCursor=object)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
        monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
        with pytest.raises(SystemExit) as exit_error:
            audit_script._run_cli(
                [
                    "--database-url",
                    "postgresql://operator:supersecret@db.example:5432/nhms",
                    "--report-out",
                    str(report_out),
                    "--manual-sql-out",
                    str(manual_out),
                ]
            )

    captured = capsys.readouterr()
    assert exit_error.value.code == 2
    assert "LIVE_DB_EVIDENCE_UNAVAILABLE" in captured.err
    assert "root relation" in captured.err
    assert "supersecret" not in captured.err
    assert not report_out.exists()
    assert not manual_out.exists()


def test_live_database_connection_failure_is_mandatory_and_writes_no_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_out = tmp_path / "report.json"
    manual_out = tmp_path / "manual.sql"
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("could not connect password=supersecret")
        )
    )
    fake_extras = types.SimpleNamespace(RealDictCursor=object)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
        monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
        with pytest.raises(SystemExit) as exit_error:
            audit_script._run_cli(
                [
                    "--database-url",
                    "postgresql://operator:supersecret@db.example:5432/nhms",
                    "--report-out",
                    str(report_out),
                    "--manual-sql-out",
                    str(manual_out),
                ]
            )

    captured = capsys.readouterr()
    assert exit_error.value.code == 2
    assert "LIVE_DB_EVIDENCE_UNAVAILABLE" in captured.err
    assert "supersecret" not in captured.err
    assert "postgresql://operator" not in captured.err
    assert not report_out.exists()
    assert not manual_out.exists()


def test_missing_database_url_fails_closed_and_writes_no_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_out = tmp_path / "report.json"
    manual_out = tmp_path / "manual.sql"

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(SystemExit) as exit_error:
            audit_script._run_cli(
                [
                    "--report-out",
                    str(report_out),
                    "--manual-sql-out",
                    str(manual_out),
                ]
            )

    captured = capsys.readouterr()
    assert exit_error.value.code == 2
    assert "LIVE_DB_EVIDENCE_UNAVAILABLE" in captured.err
    assert "DATABASE_URL is required" in captured.err
    assert not report_out.exists()
    assert not manual_out.exists()


def test_live_database_mandatory_inventory_failure_is_not_degraded_to_success() -> None:
    connection = _FakeConnection(
        {
            ROOT_RELATION_SIZE_SQL: [{"table_bytes": 100, "indexes_bytes": 50, "total_bytes": 150}],
            INDEX_INVENTORY_SQL: RuntimeError("inventory failed password=supersecret"),
            INDEX_USAGE_SQL: [],
            TIMESCALE_CHUNK_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_USAGE_SQL: [],
        }
    )

    with pytest.raises(RuntimeError) as error:
        collect_catalog_evidence(connection)

    assert "supersecret" in str(error.value)


def test_live_database_mandatory_inventory_failure_cli_is_redacted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_out = tmp_path / "report.json"
    connection = _FakeConnection(
        {
            ROOT_RELATION_SIZE_SQL: [{"table_bytes": 100, "indexes_bytes": 50, "total_bytes": 150}],
            INDEX_INVENTORY_SQL: RuntimeError("inventory failed password=supersecret"),
            INDEX_USAGE_SQL: [],
            TIMESCALE_CHUNK_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_SIZE_SQL: [],
            TIMESCALE_CHUNK_INDEX_USAGE_SQL: [],
        }
    )
    fake_psycopg2 = types.SimpleNamespace(connect=lambda *args, **kwargs: connection)
    fake_extras = types.SimpleNamespace(RealDictCursor=object)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
        monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
        with pytest.raises(SystemExit) as exit_error:
            audit_script._run_cli(
                [
                    "--database-url",
                    "postgresql://operator:supersecret@db.example:5432/nhms",
                    "--report-out",
                    str(report_out),
                ]
            )

    captured = capsys.readouterr()
    assert exit_error.value.code == 2
    assert "LIVE_DB_EVIDENCE_UNAVAILABLE" in captured.err
    assert "supersecret" not in captured.err
    assert not report_out.exists()


def test_generated_manual_sql_has_operator_guardrails_and_no_credentials() -> None:
    sql = generate_manual_maintenance_sql(
        [
            {
                "index_name": "return_period_result_null_return_period_run_idx",
                "decision": "investigate",
                "operator_candidate": "drop",
            }
        ]
    )

    assert "DO NOT AUTO-EXECUTE" in sql
    assert "lock_timeout" in sql
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "ROLLBACK" in sql
    assert "BEFORE evidence" in sql
    assert "AFTER evidence" in sql
    assert "EXPLAIN (ANALYZE, BUFFERS)" in sql
    assert "pg_repack" in sql
    assert "supersecret" not in sql
    assert "postgresql://" not in sql
    assert "-- DROP INDEX IF EXISTS flood.\"return_period_result_null_return_period_run_idx\";" in sql
    assert "\nDROP INDEX" not in sql


def test_manual_sql_neutralizes_control_character_drop_candidate_names() -> None:
    malicious_name = "x\nDROP TABLE flood.return_period_result;--"

    sql = generate_manual_maintenance_sql(
        [
            {
                "index_name": malicious_name,
                "decision": "investigate",
                "operator_candidate": "drop",
            }
        ]
    )

    assert "-- DROP INDEX IF EXISTS" not in sql
    assert "Skipped unsafe DROP candidate name" in sql
    assert r"x\nDROP TABLE flood.return_period_result;--" in sql
    assert "\nDROP TABLE flood.return_period_result" not in sql
    for line in sql.splitlines():
        assert not line.startswith("DROP TABLE")
        assert not line.startswith("DROP INDEX")


def test_manual_sql_before_after_evidence_includes_timescale_chunk_index_risks() -> None:
    sql = generate_manual_maintenance_sql([])

    assert sql.count("Capture BEFORE evidence") == 1
    assert sql.count("Capture AFTER evidence") == 1
    assert sql.count("timescaledb_information.chunks") >= 4
    assert sql.count("pg_stat_all_indexes") >= 2
    assert "Timescale chunk size evidence" in sql
    assert "Timescale chunk-index size evidence" in sql
    assert "Timescale chunk-index usage evidence" in sql
    assert "using the same queries from step 1" in sql


def test_manual_sql_does_not_emit_static_drop_candidates_without_audited_inventory() -> None:
    unavailable_sql = generate_manual_maintenance_sql(None)
    empty_inventory_sql = generate_manual_maintenance_sql([])

    assert "index inventory evidence is unavailable" in unavailable_sql
    assert "No audited NULL partial DROP candidates" in empty_inventory_sql
    assert "return_period_result_null_return_period_run_idx" not in unavailable_sql
    assert "return_period_result_null_warning_level_run_idx" not in unavailable_sql
    assert "-- DROP INDEX IF EXISTS" not in unavailable_sql
    assert "-- DROP INDEX IF EXISTS" not in empty_inventory_sql


def test_report_redacts_database_url_and_secret_shaped_evidence() -> None:
    catalog = _catalog_with_indexes([_index_row("return_period_result_summary_idx")])
    catalog["timescale_chunks"] = {
        "available": False,
        "rows": [],
        "unavailable_reason": "failed with password=supersecret",
        "sql": TIMESCALE_CHUNK_SIZE_SQL,
    }

    report = build_report(
        catalog,
        connection_mode="maintenance",
        database_url="postgresql://operator:supersecret@db.example:5432/nhms?sslpassword=topsecret",
    )
    payload = render_report_json(report)
    parsed = json.loads(payload)

    assert parsed["database"]["url"] == "postgresql://db.example:5432/nhms"
    assert "supersecret" not in payload
    assert "topsecret" not in payload
    assert "sslpassword" not in payload
    assert "[redacted]" in payload


def test_report_json_normalizes_catalog_scalars() -> None:
    catalog = _catalog_with_indexes([_index_row("return_period_result_summary_idx")])
    catalog["timescale_chunks"] = {
        "available": True,
        "rows": [
            {
                "chunk_name": "_hyper_1_1_chunk",
                "range_start": datetime(2026, 6, 18, 0, 0, tzinfo=UTC),
                "range_end": datetime(2026, 6, 19, 0, 0, tzinfo=UTC),
                "chunk_total_bytes": Decimal("123.5"),
                "audit_total_rows": Decimal("1"),
            }
        ],
        "sql": TIMESCALE_CHUNK_SIZE_SQL,
        "total_rows": Decimal("1"),
        "observed_rows": 1,
        "row_limit": 200,
        "truncated": False,
    }

    parsed = json.loads(render_report_json(build_report(catalog, connection_mode="readonly")))
    row = parsed["evidence"]["timescale_chunks"]["rows"][0]

    assert row["range_start"] == "2026-06-18T00:00:00+00:00"
    assert row["range_end"] == "2026-06-19T00:00:00+00:00"
    assert row["chunk_total_bytes"] == 123.5
    assert parsed["evidence"]["timescale_chunks"]["total_rows"] == 1


def _index_row(
    name: str,
    indexdef: str | None = None,
    *,
    predicate: str | None = None,
    is_partial: bool = False,
) -> dict[str, Any]:
    return {
        "index_name": f"flood.{name}",
        "indexrelname": name,
        "indexdef": indexdef or f"CREATE INDEX {name} ON flood.return_period_result (run_id)",
        "is_primary": name == "return_period_result_pkey",
        "is_unique": name == "return_period_result_pkey",
        "is_valid": True,
        "is_ready": True,
        "is_partial": is_partial,
        "predicate": predicate,
        "index_bytes": 1024,
        "index_size": "1024 bytes",
    }


def _catalog_with_indexes(indexes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "root_relation": {"available": True, "rows": [{"total_bytes": 1}], "sql": ROOT_RELATION_SIZE_SQL},
        "index_inventory": {"available": True, "rows": indexes, "sql": INDEX_INVENTORY_SQL},
        "index_usage": {"available": True, "rows": [], "sql": INDEX_USAGE_SQL},
        "timescale_chunks": {
            "available": True,
            "rows": [],
            "sql": TIMESCALE_CHUNK_SIZE_SQL,
            "total_rows": 0,
            "observed_rows": 0,
            "row_limit": 200,
            "truncated": False,
        },
        "timescale_chunk_indexes": {
            "available": True,
            "rows": [],
            "sql": TIMESCALE_CHUNK_INDEX_SIZE_SQL,
            "total_rows": 0,
            "observed_rows": 0,
            "row_limit": 500,
            "truncated": False,
        },
        "timescale_chunk_index_usage": {
            "available": True,
            "rows": [],
            "sql": TIMESCALE_CHUNK_INDEX_USAGE_SQL,
            "total_rows": 0,
            "observed_rows": 0,
            "row_limit": 500,
            "truncated": False,
        },
    }


class _FakeConnection:
    def __init__(self, responses: dict[str, Any]):
        self._cursor = _FakeCursor(self, responses)
        self.executed_sql: list[str] = []
        self.rollback_count = 0

    def cursor(self) -> "_FakeCursor":
        return self._cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def rollback(self) -> None:
        self.rollback_count += 1


class _FakeCursor:
    def __init__(self, connection: _FakeConnection, responses: dict[str, Any]):
        self.connection = connection
        self._responses = responses
        self._current: Any = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.connection.executed_sql.append(sql)
        response = self._responses[sql]
        if isinstance(response, Exception):
            raise response
        self._current = response

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._current)
