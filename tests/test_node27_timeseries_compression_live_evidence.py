"""Focused contract tests for issue #1069's independent live verifier."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import node27_timeseries_compression_live_evidence as evidence

ROOT = Path(__file__).resolve().parents[1]
RECEIPT_SCHEMA = json.loads(
    (ROOT / "schemas/timeseries_compression_receipt.schema.json").read_text(encoding="utf-8")
)
EVIDENCE_SCHEMA = json.loads(
    (ROOT / "schemas/timeseries_compression_live_evidence.schema.json").read_text(
        encoding="utf-8"
    )
)
HEAD = "0123456789abcdef0123456789abcdef01234567"
VERIFIER_HEAD = "89abcdef0123456789abcdef0123456789abcdef"
IDENTITY = {
    "hypertable_schema": "hydro",
    "hypertable_name": "river_timeseries",
    "chunk_schema": "_timescaledb_internal",
    "chunk_name": "_hyper_1_42_chunk",
    "range_start": "2026-05-01T00:00:00Z",
    "range_end": "2026-05-08T00:00:00Z",
}


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _json_ref(tmp_path: Path, name: str, value: Any) -> dict[str, Any]:
    path = tmp_path / name
    raw = _canonical(value)
    path.write_bytes(raw)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _file_ref(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _catalog() -> dict[str, Any]:
    def row(
        schema: str,
        table: str,
        column: str,
        segment: int | None,
        order: int | None,
    ) -> dict[str, Any]:
        return {
            "hypertable_schema": schema,
            "hypertable_name": table,
            "attname": column,
            "segmentby_column_index": segment,
            "orderby_column_index": order,
            "orderby_asc": True if order else None,
            "orderby_nullsfirst": False if order else None,
        }

    return {
        "hypertables": {
            "hydro.river_timeseries": True,
            "met.forcing_station_timeseries": True,
        },
        "compression_settings": [
            row("hydro", "river_timeseries", "run_id", 1, None),
            row("hydro", "river_timeseries", "river_network_version_id", 2, None),
            row("hydro", "river_timeseries", "river_segment_id", 3, None),
            row("hydro", "river_timeseries", "variable", None, 1),
            row("hydro", "river_timeseries", "valid_time", None, 2),
            row("met", "forcing_station_timeseries", "forcing_version_id", 1, None),
            row("met", "forcing_station_timeseries", "station_id", 2, None),
            row("met", "forcing_station_timeseries", "variable", None, 1),
            row("met", "forcing_station_timeseries", "valid_time", None, 2),
        ],
        "policy_jobs": [],
    }


def _receipt(*, enforce: bool) -> dict[str, Any]:
    selected = {
        **IDENTITY,
        "before_bytes": 4_294_967_296,
        "after_bytes": 1_073_741_824 if enforce else None,
    }
    return {
        "schema_version": "2.0",
        "head_sha": HEAD,
        "generated_at": "2026-07-15T12:05:00Z" if enforce else "2026-07-15T12:00:00Z",
        "now_utc": "2026-07-15T12:00:30Z" if enforce else "2026-07-15T11:59:55Z",
        "lag_seconds": 604800,
        "per_tick_bound": 1,
        "mode": "enforce" if enforce else "dry-run",
        "outcome": "clean",
        "selected": [selected],
        "deferred": [
            {
                "hypertable_schema": "met",
                "hypertable_name": "forcing_station_timeseries",
                "chunk_schema": "_timescaledb_internal",
                "chunk_name": "_hyper_2_20_chunk",
                "range_start": "2026-05-02T00:00:00Z",
                "range_end": "2026-05-09T00:00:00Z",
                "before_bytes": 0,
                "after_bytes": None,
                "defer_reason": "per-tick bound reached",
            }
        ],
        "skipped": [],
        "per_table_totals": {
            "hydro.river_timeseries": {
                "before_bytes": 4_294_967_296,
                "after_bytes": 1_073_741_824 if enforce else None,
                "chunks_compressed": 1 if enforce else 0,
            },
            "met.forcing_station_timeseries": {
                "before_bytes": 0,
                "after_bytes": None,
                "chunks_compressed": 0,
            },
        },
    }


def _sizes(*, post: bool) -> dict[str, Any]:
    return {
        "tables": {
            "hydro.river_timeseries": {
                "hypertable_size": 90_000_000_000 if post else 94_000_000_000,
                "parent_relation_size": 8192,
                "compressed_chunks": 1 if post else 0,
                "uncompressed_chunks": 9 if post else 10,
                "compressed_relations": (
                    [
                        {
                            "origin_chunk_schema": "_timescaledb_internal",
                            "origin_chunk_name": "_hyper_1_42_chunk",
                            "schema": "_timescaledb_internal",
                            "name": "compress_hyper_1_42_chunk",
                            "bytes": 1_073_741_824,
                        }
                    ]
                    if post
                    else []
                ),
            },
            "met.forcing_station_timeseries": {
                "hypertable_size": 48_000_000_000,
                "parent_relation_size": 8192,
                "compressed_chunks": 0,
                "uncompressed_chunks": 10,
                "compressed_relations": [],
            },
        }
    }


def _measurement(*, name: str, after: bool, execution_ms: float, read_blocks: int = 0) -> dict[str, Any]:
    plan: dict[str, Any] = {"Node Type": "Index Scan", "Relation Name": "river_timeseries"}
    if after:
        plan = {
            "Node Type": "Custom Scan",
            "Custom Plan Provider": "DecompressChunk",
            "Relation Name": IDENTITY["chunk_name"],
            "Query": name,
        }
    return {
        "plan": plan,
        "planning_ms": 1.0,
        "execution_ms": execution_ms,
        "shared_hit_blocks": 10,
        "shared_read_blocks": read_blocks,
    }


def _phase(name: str, samples: list[float], *, after: bool) -> dict[str, Any]:
    payload: Any = [{"valid_time": "2026-05-02T00:00:00Z", "value": 1.25}]
    if name == "mvt":
        payload = "deadbeef"
        raw = bytes.fromhex(payload)
        rows = 1
    else:
        raw = _canonical(payload)
        rows = len(payload)
    return {
        "result_sha256": hashlib.sha256(raw).hexdigest(),
        "rows": rows,
        "bytes": len(raw),
        "result_payload": payload,
        "cache_class": "warm-cache",
        "cold": _measurement(name=name, after=after, execution_ms=samples[0] + 20),
        "warmups": [
            _measurement(name=name, after=after, execution_ms=samples[0] + 5),
            _measurement(name=name, after=after, execution_ms=samples[0] + 2),
        ],
        "measurements": [
            _measurement(name=name, after=after, execution_ms=sample) for sample in samples
        ],
        "activity_samples": [
            {
                "captured_at": "2026-07-15T12:10:00Z" if after else "2026-07-15T11:55:00Z",
                "active_sessions": 1,
                "material_load_stable": True,
            }
        ],
    }


def _bundle(tmp_path: Path) -> dict[str, Any]:
    database_identity = {
        "dbname": "nhms",
        "instance": "node27-primary-pg15",
        "postgres_version": "15.2",
        "timescaledb_version": "2.10.2",
    }
    role = {
        "current_user": "nhms",
        "rolsuper": True,
        "rolcreaterole": True,
        "rolcreatedb": True,
        "owns_hydro_river_timeseries": True,
        "owns_met_forcing_station_timeseries": True,
        "execute_compress_chunk_regclass_boolean": True,
        "role_created": False,
        "grant_executed": False,
        "role_mutated": False,
    }
    preflight = {
        "captured_at": "2026-07-15T11:50:00Z",
        "node": "node-27",
        "repo_path": "/home/nwm/NWM",
        "mutation_head_sha": HEAD,
        "worktree_clean": True,
        "database_identity": database_identity,
        "container_state": {
            "name": "nhms-db",
            "container_id": "container-123",
            "image": "timescale/timescaledb:2.10.2-pg15",
            "status": "running",
            "running": True,
        },
        "role": role,
        "env_mode": "0600",
        "write_guards_present": True,
        "autopipe_quiescent": True,
        "database_writes_quiescent": True,
        "conflicting_locks_absent": True,
        "units": {},
    }
    for unit_name in evidence.EXPECTED_UNITS:
        journal = tmp_path / f"{unit_name}.journal.log"
        journal.write_text("bounded journal evidence\n", encoding="utf-8")
        preflight["units"][unit_name] = {
            "enabled": "enabled" if unit_name.endswith(".timer") else "static",
            "active": "inactive",
            "sub": "dead",
            "result": "success",
            "main_pid": 0,
            "journal": _file_ref(journal),
        }
    schema_dump = tmp_path / "schema.dump"
    schema_dump.write_bytes(b"PGDMP fixture forensic schema\n")
    migration = ROOT / "db/migrations/000047_hypertable_compression_settings.sql"
    candidate = {**IDENTITY, "is_compressed": False, "before_bytes": 4_294_967_296}
    deferred = {
        "hypertable_schema": "met",
        "hypertable_name": "forcing_station_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": "_hyper_2_20_chunk",
        "range_start": "2026-05-02T00:00:00Z",
        "range_end": "2026-05-09T00:00:00Z",
        "is_compressed": False,
        "before_bytes": 2_147_483_648,
    }
    post_dry_selection = {
        "observed_at": "2026-07-15T12:00:10Z",
        "cutoff": "2026-07-08T12:00:00Z",
        "free_bytes": 500_000_000_000,
        "candidates": [candidate, deferred],
        "selected": [candidate],
    }
    pre_enforce_selection = {
        **post_dry_selection,
        "observed_at": "2026-07-15T12:00:20Z",
    }
    curve_source = ROOT / "packages/common/forecast_store.py"
    mvt_source = ROOT / "services/tiles/mvt.py"
    route_source = ROOT / "apps/api/routes/hydro_display.py"
    benchmarks = {
        "queries": [
            {
                "name": name,
                "source_refs": (
                    [_file_ref(curve_source)]
                    if name == "curve"
                    else [_file_ref(mvt_source), _file_ref(route_source)]
                ),
                "query_sha256": hashlib.sha256(
                    (
                        "SELECT rt.valid_time FROM hydro.river_timeseries rt "
                        "JOIN hydro.hydro_run h ON h.run_id=rt.run_id "
                        "WHERE rt.basin_version_id=%s AND rt.river_segment_id=%s "
                        "AND rt.river_network_version_id=%s AND rt.variable = 'q_down' "
                        "AND h.run_type = 'forecast' AND h.cycle_time=%s "
                        "AND rt.valid_time BETWEEN %s AND %s"
                        if name == "curve"
                        else "WITH bounds AS (SELECT ST_TileEnvelope(:z,:x,:y)) "
                        "SELECT * FROM hydro.river_timeseries ts "
                        "JOIN core.river_segment rs ON rs.river_segment_id=ts.river_segment_id "
                        "WHERE ts.run_id=:run_id AND ts.basin_version_id=:basin_version_id "
                        "AND ts.river_network_version_id=:river_network_version_id "
                        "AND ts.variable=:variable AND ts.valid_time=:valid_time"
                    ).encode()
                ).hexdigest(),
                "query_text": (
                    "SELECT rt.valid_time FROM hydro.river_timeseries rt "
                    "JOIN hydro.hydro_run h ON h.run_id=rt.run_id "
                    "WHERE rt.basin_version_id=%s AND rt.river_segment_id=%s "
                    "AND rt.river_network_version_id=%s AND rt.variable = 'q_down' "
                    "AND h.run_type = 'forecast' AND h.cycle_time=%s "
                    "AND rt.valid_time BETWEEN %s AND %s"
                    if name == "curve"
                    else "WITH bounds AS (SELECT ST_TileEnvelope(:z,:x,:y)) "
                    "SELECT * FROM hydro.river_timeseries ts "
                    "JOIN core.river_segment rs ON rs.river_segment_id=ts.river_segment_id "
                    "WHERE ts.run_id=:run_id AND ts.basin_version_id=:basin_version_id "
                    "AND ts.river_network_version_id=:river_network_version_id "
                    "AND ts.variable=:variable AND ts.valid_time=:valid_time"
                ),
                "binding": (
                    {
                        "parameter_names": [
                            "basin_version_id",
                            "river_segment_id",
                            "river_network_version_id",
                            "issue_time",
                            "start_time",
                            "end_time",
                        ],
                        "bound_parameters": [
                            "basin-v1",
                            "segment-1",
                            "network-v1",
                            "2026-05-01T00:00:00Z",
                            "2026-05-01T00:00:00Z",
                            "2026-05-08T00:00:00Z",
                        ],
                    }
                    if name == "curve"
                    else {
                        "run_id": "run-1",
                        "basin_version_id": "basin-v1",
                        "river_network_version_id": "network-v1",
                        "variable": "q_down",
                        "valid_time": "2026-05-02T00:00:00Z",
                        "z": 9,
                        "x": 420,
                        "y": 210,
                        "feature_limit": 10_000,
                        "feature_coordinate_limit": 50_000,
                        "collection_coordinate_limit": 50_000,
                        "max_coordinate_dimensions": 3,
                        "extent": 4096,
                        "buffer": 64,
                        "simplification_tolerance_m": (
                            (40_075_016.68557849 / float(1 << 9)) / 4096.0
                        )
                        / 2.0,
                    }
                ),
                "before": _phase(name, [10, 11, 12, 13, 14, 15, 16], after=False),
                "after": _phase(name, [12, 13, 14, 15, 16, 17, 18], after=True),
            }
            for name in ("curve", "mvt")
        ]
    }
    cleanup = {
        "autopipe_timer_restored": True,
        "compression_timer_enabled": True,
        "compression_timer_active": False,
        "compression_service_active": False,
        "compression_service_activation_count": 0,
        "installed_service_matches_repo": True,
        "installed_timer_matches_repo": True,
    }
    catalog = _catalog()
    return {
        "schema_version": "2.0",
        "issue": 1069,
        "generated_at": "2026-07-15T12:00:00Z",
        "node": "node-27",
        "mutation_head_sha": HEAD,
        "verifier_head_sha": VERIFIER_HEAD,
        "database_identity": database_identity,
        "authorization": {
            "lag_seconds": 604800,
            "bound": 1,
            "max_selected_bytes": 8_589_934_592,
            "min_free_bytes": 322_122_547_200,
            "timeout_seconds": 900,
            "enforce_invocations": 1,
        },
        "preflight": {
            "evidence": _json_ref(tmp_path, "preflight.json", preflight),
            "schema_dump": _file_ref(schema_dump),
            "catalog_before": _json_ref(tmp_path, "catalog-before.json", {"before": True}),
            "pg_restore_list_exit_code": 0,
        },
        "migration": {
            "migration_file": _file_ref(migration),
            "first_exit_code": 0,
            "second_exit_code": 0,
            "catalog_after_first": _json_ref(tmp_path, "catalog-first.json", catalog),
            "catalog_after_second": _json_ref(tmp_path, "catalog-second.json", catalog),
        },
        "selection": {
            "post_dry_run": _json_ref(
                tmp_path, "selection-post-dry-run.json", post_dry_selection
            ),
            "pre_enforce": _json_ref(
                tmp_path, "selection-pre-enforce.json", pre_enforce_selection
            ),
        },
        "receipts": {
            "dry_run": _json_ref(tmp_path, "dry.json", _receipt(enforce=False)),
            "enforce": _json_ref(tmp_path, "enforce.json", _receipt(enforce=True)),
        },
        "sizes": {
            "pre": _json_ref(tmp_path, "sizes-pre.json", _sizes(post=False)),
            "post": _json_ref(tmp_path, "sizes-post.json", _sizes(post=True)),
        },
        "catalog": {
            "post": _json_ref(tmp_path, "catalog-post.json", catalog),
            "compressed_chunk_identities": [IDENTITY],
        },
        "benchmarks": {
            "evidence": _json_ref(tmp_path, "benchmarks.json", benchmarks),
        },
        "cleanup": {"evidence": _json_ref(tmp_path, "cleanup.json", cleanup)},
        "out_of_scope": {
            "retention_mutated": False,
            "drill_run": False,
            "node22_touched": False,
            "decompress_run": False,
            "role_mutated": False,
        },
    }


def test_verifier_recomputes_complete_terminal_envelope(tmp_path: Path) -> None:
    terminal = evidence.verify_bundle(
        _bundle(tmp_path), receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
    )
    jsonschema.validate(terminal, EVIDENCE_SCHEMA)
    assert terminal["verdict"] == "PASS_TASK_4_5"
    assert terminal["selection"]["bound"] == 1
    assert terminal["sizes"]["compressed_chunk_count_delta"] == 1
    assert terminal["sizes"]["post_combined_hypertable_size"] < terminal["sizes"][
        "pre_combined_hypertable_size"
    ]
    assert [query["name"] for query in terminal["benchmarks"]["queries"]] == [
        "curve",
        "mvt",
    ]
    curve = terminal["benchmarks"]["queries"][0]
    assert curve["after_capture"]["samples_ms"] == [
        measurement["execution_ms"]
        for measurement in curve["after_capture"]["measurements"]
    ]


def test_verifier_accepts_bounded_post_relation_measurement_drift(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    post = _sizes(post=True)
    post["tables"]["hydro.river_timeseries"]["compressed_relations"][0]["bytes"] += 8192
    bundle["sizes"]["post"] = _json_ref(tmp_path, "sizes-post-drift.json", post)
    terminal = evidence.verify_bundle(
        bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
    )
    assert terminal["verdict"] == "PASS_TASK_4_5"


def test_verifier_rejects_excessive_post_relation_measurement_drift(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    post = _sizes(post=True)
    post["tables"]["hydro.river_timeseries"]["compressed_relations"][0]["bytes"] += (
        evidence.MAX_POST_MEASUREMENT_DRIFT_BYTES + 1
    )
    bundle["sizes"]["post"] = _json_ref(tmp_path, "sizes-post-large-drift.json", post)
    with pytest.raises(evidence.EvidenceError, match="measurement-time drift"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_live_evidence_example_and_required_top_level_contract() -> None:
    example = json.loads(
        (ROOT / "schemas/examples/timeseries_compression_live_evidence.example.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(example, EVIDENCE_SCHEMA)
    for key in EVIDENCE_SCHEMA["required"]:
        candidate = dict(example)
        del candidate[key]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(candidate, EVIDENCE_SCHEMA)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda bundle: bundle["authorization"].__setitem__("bound", 5),
        lambda bundle: bundle.__setitem__("verifier_head_sha", "0" * 40),
        lambda bundle: bundle["out_of_scope"].__setitem__("retention_mutated", True),
        lambda bundle: bundle["migration"].__setitem__("second_exit_code", 1),
    ],
)
def test_verifier_rejects_semantically_inconsistent_bundle(
    tmp_path: Path, mutate: Any
) -> None:
    bundle = _bundle(tmp_path)
    mutate(bundle)
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_verifier_rejects_tampered_artifact(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    Path(bundle["receipts"]["enforce"]["path"]).write_text("{}\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError, match="byte count or sha256 mismatch"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize("schema_version", ["1.0", "2.0"])
def test_verifier_requires_v2_receipts_bound_to_mutation_head(
    tmp_path: Path, schema_version: str
) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["receipts"]["enforce"])
    receipt["schema_version"] = schema_version
    if schema_version == "1.0":
        del receipt["head_sha"]
    else:
        receipt["head_sha"] = "f" * 40
    bundle["receipts"]["enforce"] = _json_ref(
        tmp_path, f"enforce-{schema_version}.json", receipt
    )
    with pytest.raises(evidence.EvidenceError, match="bound-1 semantics"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_verifier_rejects_schema_valid_receipt_with_bad_arithmetic(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    receipt = _receipt(enforce=True)
    receipt["per_table_totals"]["hydro.river_timeseries"]["before_bytes"] = 1
    bundle["receipts"]["enforce"] = _json_ref(tmp_path, "bad-enforce.json", receipt)
    with pytest.raises(evidence.EvidenceError, match="arithmetic"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize("missing", ["preflight", "selection", "receipts", "benchmarks", "cleanup"])
def test_verifier_rejects_required_top_level_omission(tmp_path: Path, missing: str) -> None:
    bundle = _bundle(tmp_path)
    del bundle[missing]
    with pytest.raises(evidence.EvidenceError, match="keys differ"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_verifier_recomputes_query_and_result_hashes(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    benchmark_ref = bundle["benchmarks"]["evidence"]
    benchmark = json.loads(Path(benchmark_ref["path"]).read_text(encoding="utf-8"))
    benchmark["queries"][0]["query_text"] += " -- tampered"
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "bad-benchmark.json", benchmark)
    with pytest.raises(evidence.EvidenceError, match="query hash mismatch"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def _read_ref(ref: dict[str, Any]) -> dict[str, Any]:
    return json.loads(Path(ref["path"]).read_text(encoding="utf-8"))


@pytest.mark.parametrize("missing", ["captured_at", "mutation_head_sha", "container_state", "units"])
def test_preflight_rejects_missing_capture_contract(tmp_path: Path, missing: str) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["preflight"]["evidence"])
    del preflight[missing]
    bundle["preflight"]["evidence"] = _json_ref(tmp_path, f"preflight-no-{missing}.json", preflight)
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_preflight_rejects_posthoc_mutation_head_override(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle["mutation_head_sha"] = "f" * 40
    with pytest.raises(evidence.EvidenceError, match="mutation-head"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize("missing", ["enabled", "active", "sub", "result", "main_pid", "journal"])
def test_preflight_rejects_incomplete_unit_state(tmp_path: Path, missing: str) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["preflight"]["evidence"])
    del preflight["units"]["nhms-node27-autopipe.service"][missing]
    bundle["preflight"]["evidence"] = _json_ref(
        tmp_path, f"preflight-unit-no-{missing}.json", preflight
    )
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_legacy_single_head_and_selection_bundle_fails_closed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle["head_sha"] = bundle.pop("mutation_head_sha")
    bundle["selection"] = {"snapshot": bundle["selection"]["post_dry_run"]}
    with pytest.raises(evidence.EvidenceError, match="keys differ"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_selection_requires_two_distinct_artifacts(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle["selection"]["pre_enforce"] = bundle["selection"]["post_dry_run"]
    with pytest.raises(evidence.EvidenceError, match="distinct observations"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_selection_rejects_incomplete_or_reordered_candidates(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    selection = _read_ref(bundle["selection"]["post_dry_run"])
    selection["candidates"] = selection["candidates"][:1]
    bundle["selection"]["post_dry_run"] = _json_ref(
        tmp_path, "selection-incomplete.json", selection
    )
    with pytest.raises(evidence.EvidenceError, match="complete ordered receipt scope"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_selection_rejects_tuple_drift_between_observations(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    selection = _read_ref(bundle["selection"]["pre_enforce"])
    selection["candidates"][0]["chunk_name"] = "_hyper_1_other_chunk"
    selection["selected"][0]["chunk_name"] = "_hyper_1_other_chunk"
    bundle["selection"]["pre_enforce"] = _json_ref(tmp_path, "selection-drift.json", selection)
    with pytest.raises(evidence.EvidenceError, match="selected tuples differ"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_selection_rejects_pre_enforce_observation_older_than_60_seconds(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    selection = _read_ref(bundle["selection"]["pre_enforce"])
    selection["observed_at"] = "2026-07-15T11:59:00Z"
    bundle["selection"]["pre_enforce"] = _json_ref(tmp_path, "selection-stale.json", selection)
    with pytest.raises(evidence.EvidenceError, match="within 60 seconds"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def _mutated_benchmark_bundle(tmp_path: Path, mutate: Any) -> dict[str, Any]:
    bundle = _bundle(tmp_path)
    benchmark = _read_ref(bundle["benchmarks"]["evidence"])
    mutate(benchmark)
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "benchmark-mutated.json", benchmark)
    return bundle


def test_curve_binding_count_must_match_positional_placeholders(tmp_path: Path) -> None:
    bundle = _mutated_benchmark_bundle(
        tmp_path, lambda benchmark: benchmark["queries"][0]["binding"]["bound_parameters"].pop()
    )
    with pytest.raises(evidence.EvidenceError, match="positional binding"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize("missing", ["variable", "feature_limit", "simplification_tolerance_m"])
def test_mvt_binding_requires_exact_production_params(tmp_path: Path, missing: str) -> None:
    bundle = _mutated_benchmark_bundle(
        tmp_path, lambda benchmark: benchmark["queries"][1]["binding"].pop(missing)
    )
    with pytest.raises(evidence.EvidenceError, match="exact production parameter"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize("field", ["cold", "warmups", "measurements", "activity_samples"])
def test_benchmark_phase_requires_complete_capture(tmp_path: Path, field: str) -> None:
    def mutate(benchmark: dict[str, Any]) -> None:
        phase = benchmark["queries"][0]["after"]
        if field == "warmups":
            phase[field] = phase[field][:1]
        elif field == "measurements":
            phase[field] = phase[field][:6]
        elif field == "activity_samples":
            phase[field] = []
        else:
            del phase[field]

    bundle = _mutated_benchmark_bundle(tmp_path, mutate)
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_every_after_measurement_must_bind_decompress_chunk(tmp_path: Path) -> None:
    def mutate(benchmark: dict[str, Any]) -> None:
        benchmark["queries"][1]["after"]["measurements"][3]["plan"] = {
            "Node Type": "Index Scan",
            "Relation Name": "river_timeseries",
        }

    bundle = _mutated_benchmark_bundle(tmp_path, mutate)
    with pytest.raises(evidence.EvidenceError, match="after measurement 3"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_activity_sample_must_prove_stable_load(tmp_path: Path) -> None:
    def mutate(benchmark: dict[str, Any]) -> None:
        benchmark["queries"][0]["before"]["activity_samples"][0][
            "material_load_stable"
        ] = False

    bundle = _mutated_benchmark_bundle(tmp_path, mutate)
    with pytest.raises(evidence.EvidenceError, match="load drift"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_cli_atomically_replaces_terminal_and_keeps_mode_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(evidence, "_current_verifier_head", lambda: VERIFIER_HEAD)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(_canonical(bundle))
    output = tmp_path / "terminal.json"
    output.write_text('{"verdict":"stale"}\n', encoding="utf-8")
    output.chmod(0o600)
    code = evidence.main(["--bundle-path", str(bundle_path), "--output-path", str(output)])
    assert code == 0
    terminal = json.loads(output.read_text(encoding="utf-8"))
    assert terminal["verdict"] == "PASS_TASK_4_5"
    assert output.stat().st_mode & 0o777 == 0o600


def test_verifier_has_no_mutation_entrypoints() -> None:
    source = (ROOT / "scripts/node27_timeseries_compression_live_evidence.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "psycopg2.connect",
        "compress_chunk(",
        "decompress_chunk(",
        "drop_chunks(",
        "CREATE ROLE",
        "GRANT ",
    ):
        assert forbidden not in source
