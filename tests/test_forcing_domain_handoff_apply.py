from __future__ import annotations

import ast
import copy
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from packages.common import forcing_domain_handoff_apply as apply_module
from packages.common.forcing_domain_handoff import (
    FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD,
    REASON_PAYLOAD_CHECKSUM_MISMATCH,
    REASON_TEMPORAL_FIELD_MISSING,
    parse_forcing_domain_handoff_path,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "forcing_domain_handoff"
COMPLETE_RUN_ID = "fcst_gfs_2026062012_basins_qhh_shud"
EXPECTED_COUNTS = {
    "met.forcing_version": 1,
    "met.met_station": 2,
    "met.forcing_station_timeseries": 8,
    "met.interp_weight": 4,
}


@pytest.fixture(autouse=True)
def _patch_execute_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apply_module, "execute_values", _fake_execute_values)


def _manifest_path(case_root: Path) -> Path:
    return case_root / "object-store" / "runs" / COMPLETE_RUN_ID / "input" / "forcing_domain_handoff.json"


def _object_store_root(case_root: Path) -> Path:
    return case_root / "object-store"


def _complete_case_root() -> Path:
    return FIXTURE_ROOT / "complete"


def _copy_complete_case(tmp_path: Path) -> Path:
    target = tmp_path / "complete"
    shutil.copytree(_complete_case_root(), target)
    return target


def _parse_complete() -> dict[str, Any]:
    return parse_forcing_domain_handoff_path(
        _manifest_path(_complete_case_root()),
        object_store_root=_object_store_root(_complete_case_root()),
    )


def _parse_case(case_root: Path) -> dict[str, Any]:
    return parse_forcing_domain_handoff_path(
        _manifest_path(case_root),
        object_store_root=_object_store_root(case_root),
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _payload_path(case_root: Path, filename: str) -> Path:
    return (
        case_root
        / "object-store"
        / "forcing"
        / "gfs"
        / "2026062012"
        / "basins_qhh_v2026_06"
        / "basins_qhh_shud"
        / "payloads"
        / filename
    )


def test_path_apply_writes_four_target_tables_with_row_count_evidence() -> None:
    connection = _FakeConnection()

    report = apply_module.apply_forcing_domain_handoff_path(
        _manifest_path(_complete_case_root()),
        object_store_root=_object_store_root(_complete_case_root()),
        connection=connection,
    )

    assert report["status"] == "applied"
    assert report["available"] is True
    assert report["writes_performed"] is True
    assert report["row_counts"] == EXPECTED_COUNTS
    assert report["mode"] == apply_module.APPLY_MODE
    assert report["identity"]["run_id"] == COMPLETE_RUN_ID
    assert report["identity"]["source_id"] == "gfs"
    assert report["identity"]["forcing_version_id"] == "forc_gfs_2026062012_basins_qhh_shud"
    assert report["parser_evidence"]["forcing_version"][FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD] == (
        "7d4251776311e114cb3fe1a3a832abf88200297c2af4f8d571fa0a90877ab7f5"
    )
    assert report["apply_evidence"]["coordinate_sources"] == {"longitude_latitude": 2}
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert {table: len(rows) for table, rows in connection.tables.items()} == EXPECTED_COUNTS
    assert connection.tables["met.forcing_version"][0]["checksum"] == (
        "7d4251776311e114cb3fe1a3a832abf88200297c2af4f8d571fa0a90877ab7f5"
    )
    assert connection.tables["met.forcing_version"][0]["source_id"] == "gfs"
    assert {row["source_id"] for row in connection.tables["met.interp_weight"]} == {"gfs"}
    assert connection.tables["met.met_station"][0]["geom"] == {
        "type": "Point",
        "srid": 4490,
        "coordinates": [100.125, 38.25],
    }
    assert any("ST_SetSRID(ST_MakePoint" in statement for _, statement, _ in connection.executions)


def test_apply_from_parser_envelope_is_idempotent_for_reapply() -> None:
    envelope = _parse_complete()
    connection = _FakeConnection()

    first = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)
    second = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert first["status"] == "applied"
    assert second["status"] == "applied"
    assert second["row_counts"] == EXPECTED_COUNTS
    assert connection.commits == 2
    assert {table: len(rows) for table, rows in connection.tables.items()} == EXPECTED_COUNTS


def test_existing_placeholder_forcing_version_is_completed_by_apply() -> None:
    envelope = _parse_complete()
    placeholder = copy.deepcopy(envelope["parsed"]["met.forcing_version"][0])
    placeholder["source_id"] = "gfs"
    placeholder["end_time"] = "2026-06-20T16:00:00Z"
    placeholder["forcing_package_uri"] = f"{placeholder['forcing_package_uri']}/"
    placeholder["station_count"] = 0
    placeholder["checksum"] = None
    placeholder["lineage_json"] = {"seed": "node27_ingest_run"}
    connection = _FakeConnection()
    connection.tables["met.forcing_version"].append(placeholder)

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "applied"
    assert len(connection.tables["met.forcing_version"]) == 1
    assert connection.tables["met.forcing_version"][0]["start_time"] == envelope["parsed"]["met.forcing_version"][0][
        "start_time"
    ]
    assert connection.tables["met.forcing_version"][0]["end_time"] == envelope["parsed"]["met.forcing_version"][0][
        "end_time"
    ]
    assert connection.tables["met.forcing_version"][0]["forcing_package_uri"] == envelope["parsed"][
        "met.forcing_version"
    ][0]["forcing_package_uri"]
    assert connection.tables["met.forcing_version"][0]["station_count"] == 2
    assert connection.tables["met.forcing_version"][0]["checksum"] == (
        "7d4251776311e114cb3fe1a3a832abf88200297c2af4f8d571fa0a90877ab7f5"
    )


def test_existing_seed_forcing_version_with_same_checksum_allows_handoff_time_window() -> None:
    envelope = _parse_complete()
    seed = copy.deepcopy(envelope["parsed"]["met.forcing_version"][0])
    seed["source_id"] = "gfs"
    seed["end_time"] = "2026-06-20T18:00:00Z"
    seed["forcing_package_uri"] = f"{seed['forcing_package_uri']}/"
    seed["lineage_json"] = {
        "seed": "node27_ingest_run",
        "quality_flag": "station_forcing_unavailable",
    }
    connection = _FakeConnection()
    connection.tables["met.forcing_version"].append(seed)

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "applied"
    forcing_version = connection.tables["met.forcing_version"][0]
    assert forcing_version["start_time"] == envelope["parsed"]["met.forcing_version"][0]["start_time"]
    assert forcing_version["end_time"] == envelope["parsed"]["met.forcing_version"][0]["end_time"]
    assert forcing_version["lineage_json"]["mode"] == apply_module.APPLY_MODE
    assert len(connection.tables["met.forcing_station_timeseries"]) == EXPECTED_COUNTS[
        "met.forcing_station_timeseries"
    ]


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("missing_cycle_time", REASON_TEMPORAL_FIELD_MISSING),
        ("payload_checksum_mismatch", REASON_PAYLOAD_CHECKSUM_MISMATCH),
    ],
)
def test_parser_unavailable_missing_field_or_checksum_mismatch_does_not_open_db_cursor(
    tmp_path: Path,
    mutator: str,
    expected_code: str,
) -> None:
    case_root = _copy_complete_case(tmp_path)
    if mutator == "missing_cycle_time":
        handoff = _read_json(_manifest_path(case_root))
        del handoff["cycle_time"]
        _write_json(_manifest_path(case_root), handoff)
    else:
        timeseries_path = _payload_path(case_root, "station_timeseries.json")
        timeseries_path.write_text(timeseries_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    envelope = _parse_case(case_root)
    connection = _FakeConnection()

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "unavailable"
    assert report["available"] is False
    assert report["writes_performed"] is False
    assert report["row_counts"] == {}
    assert {reason["code"] for reason in report["unavailable_reasons"]} == {expected_code}
    assert connection.executions == []
    assert connection.commits == 0
    assert connection.rollbacks == 0


def test_station_geometry_only_rows_use_srid_4490_makepoint_coordinates() -> None:
    envelope = copy.deepcopy(_parse_complete())
    station_rows = envelope["parsed"]["met.met_station"]
    for row in station_rows:
        longitude = row.pop("longitude")
        latitude = row.pop("latitude")
        row["geometry"] = {"type": "Point", "coordinates": [longitude, latitude]}
    connection = _FakeConnection()

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "applied"
    assert report["apply_evidence"]["coordinate_sources"] == {"geometry": 2}
    assert [row["geom"]["srid"] for row in connection.tables["met.met_station"]] == [4490, 4490]
    assert connection.tables["met.met_station"][1]["geom"]["coordinates"] == [100.25, 38.375]


def test_caller_owned_cursor_uses_savepoint_without_commit_or_rollback() -> None:
    connection = _FakeConnection()
    cursor = connection.cursor()

    report = apply_module.apply_forcing_domain_handoff(_parse_complete(), cursor=cursor)

    assert report["status"] == "applied"
    assert report["apply_evidence"]["transaction"] == "caller_owned"
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert {table: len(rows) for table, rows in connection.state.items()} == EXPECTED_COUNTS
    connection.commit()
    assert {table: len(rows) for table, rows in connection.tables.items()} == EXPECTED_COUNTS


def test_caller_owned_cursor_failure_rolls_back_to_savepoint() -> None:
    connection = _FakeConnection(fail_after_stage="interp_weight")
    cursor = connection.cursor()

    report = apply_module.apply_forcing_domain_handoff(_parse_complete(), cursor=cursor)

    assert report["status"] == "failed"
    assert report["writes_performed"] is False
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert _all_tables_empty(connection.state)
    connection.commit()
    assert _all_tables_empty(connection.tables)


def test_lon_lat_and_geojson_point_mismatch_fails_closed_before_writes() -> None:
    envelope = copy.deepcopy(_parse_complete())
    envelope["parsed"]["met.met_station"][0]["geometry"] = {
        "type": "Point",
        "coordinates": [101.125, 38.25],
    }
    connection = _FakeConnection()

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "failed"
    assert report["available"] is False
    assert report["row_counts"] == {}
    assert {reason["code"] for reason in report["unavailable_reasons"]} == {
        apply_module.REASON_APPLY_STATION_COORDINATE_MISMATCH
    }
    assert connection.executions == []
    assert _all_tables_empty(connection.tables)


def test_existing_global_station_conflict_rolls_back_without_overwrite() -> None:
    envelope = _parse_complete()
    connection = _FakeConnection()
    connection.tables["met.met_station"].append(
        {
            "station_id": "qhh_forc_001",
            "basin_version_id": "other_basin_version",
            "station_name": "foreign station",
            "longitude": 100.125,
            "latitude": 38.25,
            "elevation_m": 3280.0,
            "station_role": "forcing_grid",
            "active_flag": True,
            "properties_json": {"source": "other"},
            "geom": {"type": "Point", "srid": 4490, "coordinates": [100.125, 38.25]},
        }
    )

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "failed"
    assert {reason["code"] for reason in report["unavailable_reasons"]} == {
        apply_module.REASON_APPLY_STATION_CONFLICT
    }
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert len(connection.tables["met.met_station"]) == 1
    assert connection.tables["met.met_station"][0]["basin_version_id"] == "other_basin_version"
    assert connection.tables["met.forcing_version"] == []


def test_existing_station_with_richer_metadata_is_preserved() -> None:
    envelope = _parse_complete()
    existing_station = copy.deepcopy(envelope["parsed"]["met.met_station"][0])
    existing_station["properties_json"] = {
        **existing_station["properties_json"],
        "seed": "qhh_production_bootstrap",
        "source_file": "/tmp/qhh/input/qhh/qhh.tsd.forc",
    }
    existing_station["geom"] = {
        "type": "Point",
        "srid": 4490,
        "coordinates": [existing_station["longitude"], existing_station["latitude"]],
    }
    connection = _FakeConnection()
    connection.tables["met.met_station"].append(existing_station)

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "applied"
    assert len(connection.tables["met.met_station"]) == 2
    assert connection.tables["met.met_station"][0]["properties_json"]["seed"] == "qhh_production_bootstrap"
    assert (
        connection.tables["met.met_station"][0]["properties_json"]["source_file"]
        == "/tmp/qhh/input/qhh/qhh.tsd.forc"
    )


def test_apply_preserves_existing_active_flag_true_when_payload_carries_false() -> None:
    """§D2 flag ownership: ingest apply MUST preserve existing `active_flag` on upsert.

    Failure scenario the older textual `inspect.getsource` SQL-shape lock (Plane 3
    in `test_direct_grid_variant_registration.py::test_producer_preserves_registration_flag_across_planes`)
    cannot catch: someone reformats the SQL past the string check, or semantically
    reintroduces `active_flag` into the ON CONFLICT identity predicate — the textual
    match misses it; this functional apply test catches it.

    Post-#965 the runtime producer's file-plane emits `active_flag=false` per
    `station_inventory.json` row. Post-Change 8 cutover, the DB row for the same
    station identity has `active_flag=true`. The ingest apply MUST (a) succeed
    (NOT raise `REASON_APPLY_STATION_CONFLICT`), and (b) leave the existing DB
    row's `active_flag` untouched — the ON CONFLICT DO UPDATE sentinel
    `station_id = met.met_station.station_id` is a no-op, and `active_flag` is
    intentionally EXCLUDED from the identity predicate so preserve applies
    uniformly across both existing-True and existing-False rows.

    OpenSpec §1.4 evidence key: apply-plane-preserve-active-flag-on-payload-false.
    Cross-reference: `test_direct_grid_variant_registration.py::test_producer_preserves_registration_flag_across_planes`
    exercises registration + producer planes with a textual SQL-shape check on the
    ingest apply module; this test locks the ingest apply plane FUNCTIONALLY.
    """
    envelope = copy.deepcopy(_parse_complete())
    # Post-#965 file-plane payload: the producer emits inactive.
    for row in envelope["parsed"]["met.met_station"]:
        row["active_flag"] = False

    connection = _FakeConnection()
    # Station 0 (qhh_forc_001): post-Change 8 cutover state — flipped to True in DB.
    existing_true = copy.deepcopy(envelope["parsed"]["met.met_station"][0])
    existing_true["active_flag"] = True
    existing_true["geom"] = {
        "type": "Point",
        "srid": 4490,
        "coordinates": [existing_true["longitude"], existing_true["latitude"]],
    }
    connection.tables["met.met_station"].append(existing_true)
    # Station 1 (qhh_forc_002): pre-cutover state — still False in DB. Paired
    # assertion for the "existing False + payload False -> still False" leg.
    existing_false = copy.deepcopy(envelope["parsed"]["met.met_station"][1])
    existing_false["active_flag"] = False
    existing_false["geom"] = {
        "type": "Point",
        "srid": 4490,
        "coordinates": [existing_false["longitude"], existing_false["latitude"]],
    }
    connection.tables["met.met_station"].append(existing_false)

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    # (a) Applied, not REASON_APPLY_STATION_CONFLICT — flag mismatch alone must
    # NOT be treated as an identity conflict.
    assert report["status"] == "applied"
    assert report["row_counts"] == EXPECTED_COUNTS
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert len(connection.tables["met.met_station"]) == 2

    stored = {row["station_id"]: row for row in connection.tables["met.met_station"]}
    # (b) Preserve-on-update sentinel: existing True stays True even though the
    # payload carries False (the primary regression: no `false`->`false` overwrite
    # of a legitimately-flipped row).
    assert stored["qhh_forc_001"]["active_flag"] is True
    # Paired assertion: existing False + payload False stays False (uniform
    # preserve, no accidental `true` leak from the preserve branch).
    assert stored["qhh_forc_002"]["active_flag"] is False


def test_station_upsert_returning_shortfall_rolls_back_without_overwrite() -> None:
    envelope = _parse_complete()
    existing_station = copy.deepcopy(envelope["parsed"]["met.met_station"][0])
    existing_station["geom"] = {
        "type": "Point",
        "srid": 4490,
        "coordinates": [existing_station["longitude"], existing_station["latitude"]],
    }
    connection = _FakeConnection(force_station_upsert_conflict_after_select=True)
    connection.tables["met.met_station"].append(existing_station)

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "failed"
    assert {reason["code"] for reason in report["unavailable_reasons"]} == {
        apply_module.REASON_APPLY_STATION_CONFLICT
    }
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert len(connection.tables["met.met_station"]) == 1
    assert connection.tables["met.met_station"][0]["station_name"] == existing_station["station_name"]


def test_direct_grid_constraints_from_parser_rows_are_preserved_in_apply_evidence() -> None:
    envelope = copy.deepcopy(_parse_complete())
    for row in envelope["parsed"]["met.interp_weight"]:
        row["method"] = "direct_grid"
        row["weight"] = 1.0
        row["grid_signature"] = "direct-grid-signature"
    connection = _FakeConnection()

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "applied"
    assert report["apply_evidence"]["direct_grid_rows"] == 4
    assert {row["method"] for row in connection.tables["met.interp_weight"]} == {"direct_grid"}
    assert {row["grid_signature"] for row in connection.tables["met.interp_weight"]} == {
        "direct-grid-signature"
    }
    assert any("pg_advisory_xact_lock(hashtextextended" in statement for _, statement, _ in connection.executions)


def test_direct_grid_method_case_is_canonicalized_before_insert() -> None:
    envelope = copy.deepcopy(_parse_complete())
    for row in envelope["parsed"]["met.interp_weight"]:
        row["method"] = "DIRECT_GRID"
        row["weight"] = 1.0
        row["grid_signature"] = "direct-grid-signature"
    connection = _FakeConnection()

    report = apply_module.apply_forcing_domain_handoff(envelope, connection=connection)

    assert report["status"] == "applied"
    assert {row["method"] for row in connection.tables["met.interp_weight"]} == {"direct_grid"}


def test_compressed_chunk_write_error_produces_dedicated_reason_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2: A ``CompressedChunkWriteError`` during ``_replace_forcing_station_timeseries``
    surfaces via :data:`REASON_APPLY_COMPRESSED_CHUNK_BLOCKED`, not the generic
    SQL-failure bucket. Operators route on this code to the runbook decompress
    procedure. Both transaction-ownership branches roll back exactly once.
    """
    from packages.common.timescale_write_guard import CompressedChunkWriteError

    def _raise_compressed_chunk(cursor: Any, forcing_version_id: str, rows: Any) -> None:
        raise CompressedChunkWriteError(
            chunk_schema="_timescaledb_internal",
            chunk_name="_hyper_42_1_chunk",
            hypertable_schema="met",
            hypertable_name="forcing_station_timeseries",
        )

    monkeypatch.setattr(
        apply_module,
        "_replace_forcing_station_timeseries",
        _raise_compressed_chunk,
    )

    # (a) owns_transaction=True: connection.rollback() branch.
    connection = _FakeConnection()
    report = apply_module.apply_forcing_domain_handoff(_parse_complete(), connection=connection)
    assert report["status"] == "failed"
    assert report["available"] is False
    assert report["writes_performed"] is False
    assert len(report["unavailable_reasons"]) == 1
    reason = report["unavailable_reasons"][0]
    assert reason["code"] == apply_module.REASON_APPLY_COMPRESSED_CHUNK_BLOCKED
    assert reason["exception_type"] == "CompressedChunkWriteError"
    assert "_hyper_42_1_chunk" in reason["detail"]
    assert connection.rollbacks == 1
    assert connection.commits == 0

    # (b) caller_owned cursor: savepoint rollback branch.
    caller_connection = _FakeConnection()
    caller_cursor = caller_connection.cursor()
    caller_report = apply_module.apply_forcing_domain_handoff(
        _parse_complete(), cursor=caller_cursor
    )
    assert caller_report["status"] == "failed"
    caller_reason = caller_report["unavailable_reasons"][0]
    assert caller_reason["code"] == apply_module.REASON_APPLY_COMPRESSED_CHUNK_BLOCKED
    # Caller retains ownership: helper MUST NOT commit / rollback the connection.
    assert caller_connection.commits == 0
    assert caller_connection.rollbacks == 0
    # Savepoint was released via ROLLBACK TO SAVEPOINT + RELEASE — verify at
    # least one such statement fired in the caller_owned branch.
    savepoint_rollbacks = [
        stmt
        for _, stmt, _ in caller_connection.executions
        if "rollback to savepoint" in stmt.lower()
    ]
    assert len(savepoint_rollbacks) == 1


@pytest.mark.parametrize(
    "stage",
    ["forcing_version", "met_station", "station_timeseries", "interp_weight"],
)
def test_sql_failure_after_each_stage_rolls_back_and_reports_no_readiness(stage: str) -> None:
    connection = _FakeConnection(fail_after_stage=stage)

    report = apply_module.apply_forcing_domain_handoff(_parse_complete(), connection=connection)

    assert report["status"] == "failed"
    assert report["available"] is False
    assert report["ready"] is False
    assert report["writes_performed"] is False
    assert report["row_counts"] == {}
    assert {reason["code"] for reason in report["unavailable_reasons"]} == {
        apply_module.REASON_APPLY_SQL_FAILURE
    }
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert _all_tables_empty(connection.tables)


def test_failure_reports_are_credential_safe_for_parser_and_sql_errors() -> None:
    parser_envelope = {
        "available": False,
        "status": "unavailable",
        "unavailable_reasons": [
            {
                "code": "HANDOFF_FIELD_MISSING",
                "detail": "postgresql://user:secret@example.test/db?token=parser-token",
            }
        ],
        "evidence": {"credential_url": "https://user:pass@example.test/path?token=parser-token"},
        "parsed": {},
    }

    parser_report = apply_module.apply_forcing_domain_handoff(parser_envelope, connection=_FakeConnection())
    sql_report = apply_module.apply_forcing_domain_handoff(
        _parse_complete(),
        connection=_FakeConnection(
            fail_after_stage="forcing_version",
            failure_message="postgresql://user:secret@example.test/db?token=sql-token",
        ),
    )

    serialized = json.dumps([parser_report, sql_report], sort_keys=True)
    assert "secret" not in serialized
    assert "parser-token" not in serialized
    assert "sql-token" not in serialized
    assert "[redacted]" in serialized or "postgresql://example.test/db" in serialized


def test_apply_scope_does_not_depend_on_node27_autopipeline_policy_file() -> None:
    module_source = Path(apply_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(module_source)
    imported_modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert all("node27_autopipeline" not in module_name for module_name in imported_modules)
    assert "node27_autopipeline" not in module_source


def _all_tables_empty(tables: dict[str, list[dict[str, Any]]]) -> bool:
    return all(not rows for rows in tables.values())


class _FakeConnection:
    def __init__(
        self,
        *,
        fail_after_stage: str | None = None,
        failure_message: str = "simulated SQL failure",
        force_station_upsert_conflict_after_select: bool = False,
    ) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "met.forcing_version": [],
            "met.met_station": [],
            "met.forcing_station_timeseries": [],
            "met.interp_weight": [],
        }
        self._transaction_tables: dict[str, list[dict[str, Any]]] | None = None
        self._savepoints: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.fail_after_stage = fail_after_stage
        self.failure_message = failure_message
        self.force_station_upsert_conflict_after_select = force_station_upsert_conflict_after_select
        self.commits = 0
        self.rollbacks = 0
        self.executions: list[tuple[str, str, tuple[Any, ...]]] = []

    def cursor(self) -> "_FakeCursor":
        self._transaction_tables = copy.deepcopy(self.tables)
        return _FakeCursor(self)

    def commit(self) -> None:
        assert self._transaction_tables is not None
        self.tables = self._transaction_tables
        self._transaction_tables = None
        self._savepoints.clear()
        self.commits += 1

    def rollback(self) -> None:
        self._transaction_tables = None
        self._savepoints.clear()
        self.rollbacks += 1

    @property
    def state(self) -> dict[str, list[dict[str, Any]]]:
        assert self._transaction_tables is not None
        return self._transaction_tables

    def maybe_fail(self, stage: str) -> None:
        if self.fail_after_stage == stage:
            raise RuntimeError(self.failure_message)


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self._fetchone: Any = None
        self._fetchall: list[Any] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.connection.executions.append(("execute", statement, tuple(parameters)))
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("set local statement_timeout"):
            # Compressed-chunk write guard bounds its own catalog lookup with a
            # transaction-scoped SET LOCAL. The fake connection has no such
            # concept; accept it as a no-op so the guard runs to its SELECT.
            return
        if normalized.startswith("select chunk_schema, chunk_name from timescaledb_information.chunks"):
            # Guard's catalog lookup. Fixture has no compressed chunks — return
            # no row so the guard passes and the wrapped DELETE + INSERT run
            # byte-identical to the pre-guard path.
            self._fetchone = None
            self._fetchall = []
            return
        if normalized.startswith("savepoint "):
            name = normalized.split()[1]
            self.connection._savepoints[name] = copy.deepcopy(self.connection.state)
            return
        if normalized.startswith("rollback to savepoint "):
            name = normalized.split()[3]
            self.connection._transaction_tables = copy.deepcopy(self.connection._savepoints[name])
            return
        if normalized.startswith("release savepoint "):
            name = normalized.split()[2]
            self.connection._savepoints.pop(name, None)
            return
        if normalized.startswith("select pg_advisory_xact_lock"):
            self._fetchone = {"pg_advisory_xact_lock": None}
            return
        if normalized.startswith("select station_id") and "from met.met_station" in normalized:
            station_ids = set(parameters[0])
            self._fetchall = [
                _station_select_row(row)
                for row in self.connection.state["met.met_station"]
                if row["station_id"] in station_ids
            ]
            if self.connection.force_station_upsert_conflict_after_select and self._fetchall:
                row = _find_row(self.connection.state["met.met_station"], "station_id", self._fetchall[0]["station_id"])
                assert row is not None
                row["station_name"] = "concurrent conflicting station"
                self.connection.force_station_upsert_conflict_after_select = False
            return
        if "insert into met.forcing_version" in normalized:
            self._upsert_forcing_version(parameters)
            self.connection.maybe_fail("forcing_version")
            return
        if normalized.startswith("delete from met.forcing_station_timeseries"):
            forcing_version_id = parameters[0]
            self.connection.state["met.forcing_station_timeseries"] = [
                row
                for row in self.connection.state["met.forcing_station_timeseries"]
                if row["forcing_version_id"] != forcing_version_id
            ]
            return
        if normalized.startswith("delete from met.interp_weight"):
            source_id, grid_id, model_id = parameters
            self.connection.state["met.interp_weight"] = [
                row
                for row in self.connection.state["met.interp_weight"]
                if (row["source_id"], row["grid_id"], row["model_id"]) != (source_id, grid_id, model_id)
            ]
            return
        if normalized.startswith("select count(*) as rows from met.forcing_version"):
            forcing_version_id = parameters[0]
            self._fetchone = {
                "rows": sum(
                    1
                    for row in self.connection.state["met.forcing_version"]
                    if row["forcing_version_id"] == forcing_version_id
                )
            }
            return
        if normalized.startswith("select count(*) as rows from met.met_station"):
            station_ids = set(parameters[0])
            self._fetchone = {
                "rows": sum(
                    1 for row in self.connection.state["met.met_station"] if row["station_id"] in station_ids
                )
            }
            return
        if normalized.startswith("select count(*) as rows from met.forcing_station_timeseries"):
            forcing_version_id = parameters[0]
            self._fetchone = {
                "rows": sum(
                    1
                    for row in self.connection.state["met.forcing_station_timeseries"]
                    if row["forcing_version_id"] == forcing_version_id
                )
            }
            return
        if normalized.startswith("select count(*) as rows from met.interp_weight"):
            source_id, grid_id, model_id = parameters
            self._fetchone = {
                "rows": sum(
                    1
                    for row in self.connection.state["met.interp_weight"]
                    if (row["source_id"], row["grid_id"], row["model_id"]) == (source_id, grid_id, model_id)
                )
            }
            return
        raise AssertionError(f"unhandled SQL: {statement}")

    def fetchone(self) -> Any:
        return self._fetchone

    def fetchall(self) -> list[Any]:
        return self._fetchall

    def _upsert_forcing_version(self, parameters: tuple[Any, ...]) -> None:
        keys = (
            "forcing_version_id",
            "model_id",
            "source_id",
            "cycle_time",
            "start_time",
            "end_time",
            "station_count",
            "forcing_package_uri",
            "checksum",
            "lineage_json",
        )
        record = dict(zip(keys, parameters, strict=True))
        record["lineage_json"] = _unwrap_json(record["lineage_json"])
        existing = _find_row(
            self.connection.state["met.forcing_version"],
            "forcing_version_id",
            record["forcing_version_id"],
        )
        if existing is None:
            self.connection.state["met.forcing_version"].append(record)
            self._fetchone = {"forcing_version_id": record["forcing_version_id"]}
            return
        identity_compatible = all(
            existing[key] == record[key]
            for key in (
                "model_id",
                "source_id",
                "cycle_time",
            )
        )
        existing_uri = str(existing.get("forcing_package_uri") or "").rstrip("/")
        record_uri = str(record.get("forcing_package_uri") or "").rstrip("/")
        identity_compatible = identity_compatible and existing_uri == record_uri
        placeholder_compatible = existing.get("checksum") is None
        finalized_compatible = existing.get("checksum") == record["checksum"]
        compatible = identity_compatible and (placeholder_compatible or finalized_compatible)
        if not compatible:
            self._fetchone = None
            return
        existing["start_time"] = record["start_time"]
        existing["end_time"] = record["end_time"]
        existing["station_count"] = record["station_count"]
        existing["forcing_package_uri"] = record["forcing_package_uri"]
        existing["checksum"] = record["checksum"]
        existing["lineage_json"] = record["lineage_json"]
        self._fetchone = {"forcing_version_id": record["forcing_version_id"]}


def _fake_execute_values(
    cursor: _FakeCursor,
    statement: str,
    rows: list[tuple[Any, ...]],
    **kwargs: Any,
) -> list[tuple[str]] | None:
    row_list = list(rows)
    recorded_statement = f"{statement}\n{kwargs.get('template', '')}"
    cursor.connection.executions.append(("execute_values", recorded_statement, tuple(row_list)))
    normalized = " ".join(statement.lower().split())
    if "insert into met.met_station" in normalized:
        returned = _upsert_fake_stations(cursor.connection.state["met.met_station"], row_list)
        cursor.connection.maybe_fail("met_station")
        return returned if kwargs.get("fetch") else None
    if "insert into met.forcing_station_timeseries" in normalized:
        keys = apply_module.FORCING_STATION_TIMESERIES_COLUMNS
        cursor.connection.state["met.forcing_station_timeseries"].extend(
            dict(zip(keys, row, strict=True)) for row in row_list
        )
        cursor.connection.maybe_fail("station_timeseries")
        return None
    if "insert into met.interp_weight" in normalized:
        keys = apply_module.INTERP_WEIGHT_COLUMNS
        cursor.connection.state["met.interp_weight"].extend(dict(zip(keys, row, strict=True)) for row in row_list)
        cursor.connection.maybe_fail("interp_weight")
        return None
    raise AssertionError(f"unhandled execute_values SQL: {statement}")


def _upsert_fake_stations(table: list[dict[str, Any]], rows: list[tuple[Any, ...]]) -> list[tuple[str]]:
    returned: list[tuple[str]] = []
    # §D2 flag ownership (§1.4): the production INSERT template now lands `active_flag`
    # as a literal `false` and drops the field from the row tuple entirely. The fake
    # models the same shape — 8 keys — and stamps `active_flag=False` on the fresh
    # record after the zip so existing test assertions on stored `active_flag` still
    # see the SQL literal, while a DO UPDATE preserves the existing row's flag.
    keys = (
        "station_id",
        "basin_version_id",
        "station_name",
        "longitude",
        "latitude",
        "elevation_m",
        "station_role",
        "properties_json",
    )
    for row in rows:
        record = dict(zip(keys, row, strict=True))
        record["properties_json"] = _unwrap_json(record["properties_json"])
        record["active_flag"] = False
        record["geom"] = {
            "type": "Point",
            "srid": 4490,
            "coordinates": [record["longitude"], record["latitude"]],
        }
        existing = _find_row(table, "station_id", record["station_id"])
        if existing is None:
            table.append(record)
            returned.append((record["station_id"],))
            continue
        if _fake_station_compatible(existing, record):
            returned.append((record["station_id"],))
    return returned


def _fake_station_compatible(existing: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    # §D2: `active_flag` is intentionally NOT part of the identity compatibility
    # predicate — flag ownership is registration/cutover, not identity drift.
    existing_select = _station_select_row(existing)
    return (
        existing_select["basin_version_id"] == record["basin_version_id"]
        and existing_select["station_name"] == record["station_name"]
        and existing_select["longitude"] == record["longitude"]
        and existing_select["latitude"] == record["latitude"]
        and existing_select["elevation_m"] == record["elevation_m"]
        and existing_select["station_role"] == record["station_role"]
    )


def _station_select_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if "longitude" in row and "latitude" in row:
        longitude = row["longitude"]
        latitude = row["latitude"]
    else:
        longitude, latitude = row["geom"]["coordinates"]
    return {
        "station_id": row["station_id"],
        "basin_version_id": row["basin_version_id"],
        "station_name": row["station_name"],
        "longitude": longitude,
        "latitude": latitude,
        "elevation_m": row["elevation_m"],
        "station_role": row["station_role"],
        "active_flag": row["active_flag"],
        "properties_json": row["properties_json"],
    }


def _find_row(rows: list[dict[str, Any]], key: str, value: Any) -> dict[str, Any] | None:
    return next((row for row in rows if row.get(key) == value), None)


def _unwrap_json(value: Any) -> Any:
    return value.adapted if hasattr(value, "adapted") else value
