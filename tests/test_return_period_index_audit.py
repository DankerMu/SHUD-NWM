from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.audit_return_period_indexes import (
    INDEX_INVENTORY_SQL,
    INDEX_USAGE_SQL,
    ROOT_RELATION_SIZE_SQL,
    TIMESCALE_CHUNK_INDEX_SIZE_SQL,
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
        "flood-alert-summary",
        "ranking-segments",
        "timeline",
        "geojson-fallback-tile",
        "mvt-selected-identity",
        "valid-time-discovery",
        "latest-ready-run-quality",
    }
    for probe in probes:
        assert "EXPLAIN (ANALYZE, BUFFERS)" in probe["sql"]
        assert ":run_id" in probe["sql"] or probe["name"] == "valid-time-discovery"
        assert "run-secret-should-not-be-in-sql" not in probe["sql"]
    assert ":duration" in by_name["geojson-fallback-tile"]["sql"]
    assert ":valid_time" in by_name["mvt-selected-identity"]["sql"]
    assert ":basin_version_id" in by_name["valid-time-discovery"]["sql"]
    assert ":river_network_version_id" in by_name["timeline"]["sql"]
    assert ":segment_id" in by_name["timeline"]["sql"]
    assert ":min_lon" in by_name["geojson-fallback-tile"]["sql"]
    assert "flood.run_product_quality" in by_name["latest-ready-run-quality"]["sql"]


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
    assert not target.exists()
    assert list(tmp_path.glob(".partial.json.tmp-*")) == []


def test_timescale_metadata_failure_keeps_root_table_evidence() -> None:
    connection = _FakeConnection(
        {
            ROOT_RELATION_SIZE_SQL: [{"table_bytes": 100, "indexes_bytes": 50, "total_bytes": 150}],
            INDEX_INVENTORY_SQL: [_index_row("return_period_result_summary_idx")],
            INDEX_USAGE_SQL: [{"index_name": "return_period_result_summary_idx", "idx_scan": 42}],
            TIMESCALE_CHUNK_SIZE_SQL: RuntimeError("timescaledb_information.chunks missing password=supersecret"),
            TIMESCALE_CHUNK_INDEX_SIZE_SQL: [],
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
        "timescale_chunks": {"available": True, "rows": [], "sql": TIMESCALE_CHUNK_SIZE_SQL},
        "timescale_chunk_indexes": {"available": True, "rows": [], "sql": TIMESCALE_CHUNK_INDEX_SIZE_SQL},
    }


class _FakeConnection:
    def __init__(self, responses: dict[str, Any]):
        self._cursor = _FakeCursor(self, responses)
        self.executed_sql: list[str] = []
        self.rollback_count = 0

    def cursor(self) -> "_FakeCursor":
        return self._cursor

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
