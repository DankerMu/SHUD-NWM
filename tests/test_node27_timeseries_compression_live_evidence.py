"""Focused contract tests for issue #1069's independent live verifier."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from apps.api.routes.hydro_display import _postgis_tile_params
from scripts import node27_timeseries_compression_benchmark as benchmark
from scripts import node27_timeseries_compression_live_evidence as evidence
from services.tiles.mvt import postgis_tile_sql

ROOT = Path(__file__).resolve().parents[1]
RECEIPT_SCHEMA = json.loads(
    (ROOT / "schemas/timeseries_compression_receipt.schema.json").read_text(encoding="utf-8")
)
EVIDENCE_SCHEMA = json.loads(
    (ROOT / "schemas/timeseries_compression_live_evidence.schema.json").read_text(
        encoding="utf-8"
    )
)
HEAD = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
).stdout.strip()
VERIFIER_HEAD = "89abcdef0123456789abcdef0123456789abcdef"
IDENTITY = {
    "hypertable_schema": "hydro",
    "hypertable_name": "river_timeseries",
    "chunk_schema": "_timescaledb_internal",
    "chunk_name": "_hyper_3_7_chunk",
    "range_start": "2026-05-28T00:00:00Z",
    "range_end": "2026-06-04T00:00:00Z",
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


def _invocation(
    *,
    kind: str,
    started_at: str,
    finished_at: str,
    bindings: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "argv": evidence.INVOCATION_ARGV[kind],
        "timeout_seconds": 900,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": 0,
        "mutation_head_sha": HEAD,
        "artifact_bindings": bindings,
        **evidence._invocation_execution_identity(kind),
    }


def _pg_restore_record(dump_sha256: str) -> dict[str, Any]:
    stdout = b"TABLE hydro river_timeseries\nTABLE met forcing_station_timeseries\n"
    return {
        "dump_sha256": dump_sha256,
        "argv": ["/usr/bin/pg_restore", "--list", "<descriptor-bound-dump>"],
        "exit_code": 0,
        "tool_version": "pg_restore (PostgreSQL) 15.2",
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stdout_bytes": len(stdout),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "stderr_bytes": 0,
        "entries": [
            "TABLE hydro river_timeseries",
            "TABLE met forcing_station_timeseries",
        ],
    }


@pytest.fixture(autouse=True)
def _descriptor_bound_pg_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evidence,
        "_run_pg_restore_list",
        lambda identity: _pg_restore_record(identity.sha256),
    )
    monkeypatch.setattr(
        evidence,
        "_git_blob_bytes",
        lambda _head, relative_path, _label: (ROOT / relative_path).read_bytes(),
    )


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
        "before_bytes": 4_115_734_528,
        "after_bytes": 134_119_424 if enforce else None,
        "mutation_state": "committed" if enforce else "not_applicable",
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
                "before_bytes": 4_115_734_528,
                "after_bytes": 134_119_424 if enforce else None,
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
        "captured_at": "2026-07-15T12:05:02Z" if post else "2026-07-15T12:00:24Z",
        "snapshot_id": "sizes-post" if post else "sizes-pre",
        "phase": "post-enforce" if post else "pre-enforce",
        "mutation_head_sha": HEAD,
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
                            "origin_chunk_name": "_hyper_3_7_chunk",
                            "schema": "_timescaledb_internal",
                            "name": "compress_hyper_7_15_chunk",
                            "bytes": 134_119_424,
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
    payload: Any = [{"valid_time": "2026-05-29T00:00:00Z", "value": 1.25}]
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
                "captured_at": (
                    f"2026-07-15T12:{'11' if name == 'mvt' else '10'}:0{index}Z"
                    if after
                    else f"2026-07-15T12:00:{12 + (5 if name == 'mvt' else 0) + index}Z"
                ),
                "stage": stage,
                "sessions": [],
                "material_load_stable": True,
            }
            for index, stage in enumerate(
                [
                    "before_cold",
                    "after_cold",
                    "before_measurements",
                    "mid_measurements",
                    "after_result",
                ]
            )
        ],
        "execution_bounds": {
            "statement_timeout_ms": 60_000,
            "lock_timeout_ms": 5_000,
            "phase_timeout_seconds": 900,
            "started_at": (
                "2026-07-15T12:11:00Z" if name == "mvt" else "2026-07-15T12:10:00Z"
            ) if after else (
                "2026-07-15T12:00:17Z" if name == "mvt" else "2026-07-15T12:00:12Z"
            ),
            "finished_at": (
                "2026-07-15T12:11:04Z" if name == "mvt" else "2026-07-15T12:10:04Z"
            ) if after else (
                "2026-07-15T12:00:21Z" if name == "mvt" else "2026-07-15T12:00:16Z"
            ),
        },
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
        "repo_remote_identity": "DankerMu/SHUD-NWM",
        "mutation_head_sha": HEAD,
        "worktree_clean": True,
        "database_identity": database_identity,
        "database_identity_probe": {
            "captured_at": "2026-07-15T11:49:59Z",
            "query": (
                "SELECT current_database() AS dbname, "
                "current_setting('server_version') AS postgres_version, "
                "extversion AS timescaledb_version FROM pg_extension "
                "WHERE extname = 'timescaledb'"
            ),
            "row": database_identity,
        },
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
        "prior_autopipe_state": {
            "timer": {
                "enabled": "enabled",
                "active": "active",
                "sub": "waiting",
                "result": "success",
            },
            "service": {
                "enabled": "static",
                "active": "inactive",
                "sub": "dead",
                "result": "success",
            },
        },
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
    candidate = {**IDENTITY, "is_compressed": False, "before_bytes": 4_115_734_528}
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
        "cutoff": "2026-07-08T12:00:10Z",
        "free_bytes": 500_000_000_000,
        "candidates": [candidate, deferred],
        "selected": [candidate],
    }
    pre_enforce_selection = {
        **post_dry_selection,
        "observed_at": "2026-07-15T12:00:22Z",
        "cutoff": "2026-07-08T12:00:22Z",
    }
    curve_source = ROOT / "packages/common/forecast_store.py"
    mvt_source = ROOT / "services/tiles/mvt.py"
    route_source = ROOT / "apps/api/routes/hydro_display.py"
    curve_query, curve_names, curve_parameters = benchmark._curve_query_and_binding(
        basin_version_id="basin-v1",
        river_segment_id="model_reach_000001",
        river_network_version_id="network-v1",
        issue_time=datetime(2026, 5, 28, tzinfo=UTC),
        end_time=datetime(2026, 6, 4, tzinfo=UTC),
        scenario="gfs",
    )
    mvt_request = {
        "run_id": "run-1",
        "basin_version_id": "basin-v1",
        "river_network_version_id": "network-v1",
        "valid_time": "2026-05-29T00:00:00Z",
        "z": 9,
        "x": 420,
        "y": 210,
    }
    mvt_query = postgis_tile_sql("hydro")
    mvt_binding = benchmark._json_value(
        _postgis_tile_params(
            {
                "run_id": mvt_request["run_id"],
                "basin_version_id": mvt_request["basin_version_id"],
                "river_network_version_id": mvt_request["river_network_version_id"],
                "variable": "q_down",
                "valid_time": datetime(2026, 5, 29, tzinfo=UTC),
            },
            z=9,
            x=420,
            y=210,
        )
    )
    benchmarks = {
        "queries": [
            {
                "name": name,
                "request": (
                    {
                        "basin_version_id": "basin-v1",
                        "river_segment_id": "model_reach_000001",
                        "river_network_version_id": "network-v1",
                        "issue_time": "2026-05-28T00:00:00Z",
                        "end_time": "2026-06-04T00:00:00Z",
                        "scenario": "gfs",
                    }
                    if name == "curve"
                    else mvt_request
                ),
                "source_refs": (
                    [_file_ref(curve_source)]
                    if name == "curve"
                    else [_file_ref(mvt_source), _file_ref(route_source)]
                ),
                "query_sha256": hashlib.sha256(
                    (curve_query if name == "curve" else mvt_query).encode()
                ).hexdigest(),
                "query_text": curve_query if name == "curve" else mvt_query,
                "binding": (
                    {
                        "parameter_names": curve_names,
                        "bound_parameters": benchmark._json_value(curve_parameters),
                    }
                    if name == "curve"
                    else mvt_binding
                ),
                "before": _phase(name, [10, 11, 12, 13, 14, 15, 16], after=False),
                "after": _phase(name, [12, 13, 14, 15, 16, 17, 18], after=True),
            }
            for name in ("curve", "mvt")
        ]
    }
    repo_service = ROOT / "infra/systemd/nhms-node27-timeseries-compression.service"
    repo_timer = ROOT / "infra/systemd/nhms-node27-timeseries-compression.timer"
    installed_service = tmp_path / "installed-compression.service"
    installed_timer = tmp_path / "installed-compression.timer"
    installed_service.write_bytes(repo_service.read_bytes())
    installed_timer.write_bytes(repo_timer.read_bytes())
    final_units: dict[str, Any] = {}
    for unit_name in evidence.EXPECTED_UNITS:
        journal = tmp_path / f"final-{unit_name}.journal.log"
        journal.write_text("bounded final journal evidence\n", encoding="utf-8")
        if unit_name == "nhms-node27-autopipe.timer":
            enabled, active, sub = "enabled", "active", "waiting"
        elif unit_name.endswith(".timer"):
            enabled, active, sub = "enabled", "inactive", "dead"
        else:
            enabled, active, sub = "static", "inactive", "dead"
        final_units[unit_name] = {
            "enabled": enabled,
            "active": active,
            "sub": sub,
            "result": "success",
            "main_pid": 0,
            "journal": _file_ref(journal),
        }
    cleanup = {
        "captured_at": "2026-07-15T12:20:01Z",
        "window_started_at": "2026-07-15T11:40:00Z",
        "window_finished_at": "2026-07-15T12:20:00Z",
        "repo_units": {
            "service": _file_ref(repo_service),
            "timer": _file_ref(repo_timer),
        },
        "installed_units": {
            "service": _file_ref(installed_service),
            "timer": _file_ref(installed_timer),
        },
        "resolved_exec_start": [
            "/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh",
            "--enforce",
        ],
        "final_units": final_units,
        "compression_service_activations": [],
    }
    recovery_preflight = {
        **preflight,
        "captured_at": "2026-07-15T11:40:00Z",
        "database_identity_probe": {
            **preflight["database_identity_probe"],
            "captured_at": "2026-07-15T11:39:59Z",
        },
        "target": IDENTITY,
        "free_bytes": 500_000_000_000,
        "before_compressed": True,
        "before_row_count": 12_345_678,
    }
    recovery_receipt = {
        "started_at": "2026-07-15T11:41:00Z",
        "finished_at": "2026-07-15T11:45:00Z",
        "node": "node-27",
        "mutation_head_sha": HEAD,
        "database_identity": database_identity,
        "target": IDENTITY,
        "exit_code": 0,
        "decompress_return_relation": "_timescaledb_internal._hyper_3_7_chunk",
        "after_compressed": False,
        "after_row_count": 12_345_678,
    }
    catalog = _catalog()
    recovery_preflight_ref = _json_ref(
        tmp_path, "recovery-preflight.json", recovery_preflight
    )
    recovery_receipt_ref = _json_ref(
        tmp_path, "recovery-receipt.json", recovery_receipt
    )
    recovery_invocation_ref = _json_ref(
        tmp_path,
        "recovery-invocation.json",
        _invocation(
            kind="recovery_decompress",
            started_at="2026-07-15T11:41:00Z",
            finished_at="2026-07-15T11:45:00Z",
            bindings={
                "receipt_sha256": recovery_receipt_ref["sha256"],
                "target": IDENTITY,
            },
        ),
    )
    catalog_first_ref = _json_ref(
        tmp_path,
        "catalog-first.json",
        {
            "captured_at": "2026-07-15T11:31:00Z",
            "snapshot_id": "catalog-first",
            "phase": "after-first-apply",
            "mutation_head_sha": HEAD,
            "catalog": catalog,
        },
    )
    catalog_second_ref = _json_ref(
        tmp_path,
        "catalog-second.json",
        {
            "captured_at": "2026-07-15T11:32:00Z",
            "snapshot_id": "catalog-second",
            "phase": "after-second-apply",
            "mutation_head_sha": HEAD,
            "catalog": catalog,
        },
    )
    migration_ref = _file_ref(migration)
    migration_first_invocation_ref = _json_ref(
        tmp_path,
        "migration-first-invocation.json",
        _invocation(
            kind="migration_apply",
            started_at="2026-07-15T11:30:00Z",
            finished_at="2026-07-15T11:31:00Z",
            bindings={
                "migration_sha256": migration_ref["sha256"],
                "catalog_sha256": catalog_first_ref["sha256"],
            },
        ),
    )
    migration_second_invocation_ref = _json_ref(
        tmp_path,
        "migration-second-invocation.json",
        _invocation(
            kind="migration_apply",
            started_at="2026-07-15T11:31:01Z",
            finished_at="2026-07-15T11:32:00Z",
            bindings={
                "migration_sha256": migration_ref["sha256"],
                "catalog_sha256": catalog_second_ref["sha256"],
            },
        ),
    )
    dry_ref = _json_ref(tmp_path, "dry.json", _receipt(enforce=False))
    enforce_ref = _json_ref(tmp_path, "enforce.json", _receipt(enforce=True))
    dry_invocation_ref = _json_ref(
        tmp_path,
        "dry-invocation.json",
        _invocation(
            kind="compression_dry_run",
            started_at="2026-07-15T11:59:50Z",
            finished_at="2026-07-15T12:00:00Z",
            bindings={"receipt_sha256": dry_ref["sha256"]},
        ),
    )
    enforce_invocation_ref = _json_ref(
        tmp_path,
        "enforce-invocation.json",
        _invocation(
            kind="compression_enforce",
            started_at="2026-07-15T12:00:25Z",
            finished_at="2026-07-15T12:05:01Z",
            bindings={"receipt_sha256": enforce_ref["sha256"]},
        ),
    )
    catalog_post_ref = _json_ref(
        tmp_path,
        "catalog-post.json",
        {
            "captured_at": "2026-07-15T12:05:03Z",
            "snapshot_id": "catalog-post",
            "mutation_head_sha": HEAD,
            "catalog": catalog,
            "compressed_chunk_identities": [IDENTITY],
        },
    )
    invocation_refs = [
        migration_first_invocation_ref,
        migration_second_invocation_ref,
        recovery_invocation_ref,
        dry_invocation_ref,
        enforce_invocation_ref,
    ]
    execution_journal = tmp_path / "execution-audit.log"
    execution_journal.write_text(
        "\n".join(
            [
                f"kind={kind} invocation_sha256={ref['sha256']}"
                for kind, ref in zip(
                    [
                        "migration_apply",
                        "migration_apply",
                        "recovery_decompress",
                        "compression_dry_run",
                        "compression_enforce",
                    ],
                    invocation_refs,
                    strict=True,
                )
            ]
            + ["direct_db_mutation_statements=0", ""]
        ),
        encoding="utf-8",
    )
    execution_audit_ref = _json_ref(
        tmp_path,
        "execution-audit.json",
        {
            "captured_at": "2026-07-15T12:21:01Z",
            "window_started_at": "2026-07-15T11:19:00Z",
            "window_finished_at": "2026-07-15T12:21:00Z",
            "mutation_head_sha": HEAD,
            "audit_source": "pgaudit+systemd-journal",
            "complete": True,
            "namespace_counts": {
                "migration_apply": 2,
                "recovery_decompress": 1,
                "compression_dry_run": 1,
                "compression_enforce": 1,
            },
            "invocation_refs": invocation_refs,
            "direct_db_mutation_statements": [],
            "journal": _file_ref(execution_journal),
        },
    )
    return {
        "schema_version": "3.0",
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
            "replay_decompression": True,
            "decompress_invocations": 1,
            "migration_invocations": 2,
            "dry_run_invocations": 1,
            "direct_db_bypass_invocations": 0,
            "repo_path": "/home/nwm/NWM",
            "remote_identity": "DankerMu/SHUD-NWM",
            "reviewed_mutation_sha": HEAD,
            "reviewed_remote_ref": "refs/remotes/origin/feat/issue-1069-live-compression",
        },
        "execution": {"audit": execution_audit_ref},
        "recovery": {
            "preflight": recovery_preflight_ref,
            "receipt": recovery_receipt_ref,
            "invocation": recovery_invocation_ref,
        },
        "preflight": {
            "evidence": _json_ref(tmp_path, "preflight.json", preflight),
            "schema_dump": _file_ref(schema_dump),
            "schema_dump_list": _json_ref(
                tmp_path,
                "schema-dump-list.json",
                {
                    "captured_at": "2026-07-15T11:20:00Z",
                    "snapshot_id": "schema-dump-list",
                    "mutation_head_sha": HEAD,
                    **_pg_restore_record(_file_ref(schema_dump)["sha256"]),
                },
            ),
            "catalog_before": _json_ref(
                tmp_path,
                "catalog-before.json",
                {
                    "captured_at": "2026-07-15T11:25:00Z",
                    "snapshot_id": "catalog-before",
                    "phase": "pre-migration",
                    "mutation_head_sha": HEAD,
                    "catalog": {
                        "hypertables": {
                            "hydro.river_timeseries": False,
                            "met.forcing_station_timeseries": False,
                        },
                        "compression_settings": [],
                        "policy_jobs": [],
                    },
                },
            ),
        },
        "migration": {
            "migration_file": migration_ref,
            "first_invocation": migration_first_invocation_ref,
            "catalog_after_first": catalog_first_ref,
            "second_invocation": migration_second_invocation_ref,
            "catalog_after_second": catalog_second_ref,
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
            "dry_run": dry_ref,
            "dry_run_invocation": dry_invocation_ref,
            "enforce": enforce_ref,
            "enforce_invocation": enforce_invocation_ref,
        },
        "sizes": {
            "pre": _json_ref(tmp_path, "sizes-pre.json", _sizes(post=False)),
            "post": _json_ref(tmp_path, "sizes-post.json", _sizes(post=True)),
        },
        "catalog": {"post": catalog_post_ref},
        "benchmarks": {
            "evidence": _json_ref(tmp_path, "benchmarks.json", benchmarks),
        },
        "cleanup": {"evidence": _json_ref(tmp_path, "cleanup.json", cleanup)},
        "out_of_scope": {
            "retention_mutated": False,
            "drill_run": False,
            "node22_touched": False,
            "decompress_run": True,
            "role_mutated": False,
        },
    }


def test_verifier_recomputes_complete_terminal_envelope(tmp_path: Path) -> None:
    terminal = evidence.verify_bundle(
        _bundle(tmp_path), receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
    )
    jsonschema.validate(terminal, EVIDENCE_SCHEMA)
    assert terminal["verdict"] == "PASS_TASK_4_5"
    assert terminal["recovery"]["authorized"] is True
    assert terminal["recovery"]["row_parity"] is True
    assert terminal["recovery"]["target"] == IDENTITY
    assert terminal["out_of_scope"]["decompress_run"] is True
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
    with pytest.raises(evidence.EvidenceError, match="artifact association|bound-1 semantics"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_verifier_rejects_schema_valid_receipt_with_bad_arithmetic(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    receipt = _receipt(enforce=True)
    receipt["per_table_totals"]["hydro.river_timeseries"]["before_bytes"] = 1
    bundle["receipts"]["enforce"] = _json_ref(tmp_path, "bad-enforce.json", receipt)
    invocation = _read_ref(bundle["receipts"]["enforce_invocation"])
    invocation["artifact_bindings"]["receipt_sha256"] = bundle["receipts"]["enforce"][
        "sha256"
    ]
    bundle["receipts"]["enforce_invocation"] = _json_ref(
        tmp_path, "bad-enforce-invocation.json", invocation
    )
    with pytest.raises(evidence.EvidenceError, match="arithmetic"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize(
    "missing", ["recovery", "preflight", "selection", "receipts", "benchmarks", "cleanup"]
)
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
    with pytest.raises(evidence.EvidenceError, match="public production owner|query hash mismatch"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def _read_ref(ref: dict[str, Any]) -> dict[str, Any]:
    return json.loads(Path(ref["path"]).read_text(encoding="utf-8"))


@pytest.mark.parametrize("missing", ["preflight", "receipt"])
def test_recovery_requires_two_artifacts(tmp_path: Path, missing: str) -> None:
    bundle = _bundle(tmp_path)
    del bundle["recovery"][missing]
    with pytest.raises(evidence.EvidenceError, match="recovery keys differ"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_recovery_rejects_tampered_artifact(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    Path(bundle["recovery"]["receipt"]["path"]).write_text("{}\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError, match="byte count or sha256 mismatch"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize(
    ("artifact_name", "missing"),
    [
        ("preflight", "captured_at"),
        ("preflight", "worktree_clean"),
        ("preflight", "units"),
        ("preflight", "before_row_count"),
        ("receipt", "finished_at"),
        ("receipt", "decompress_return_relation"),
    ],
)
def test_recovery_rejects_required_field_omission(
    tmp_path: Path, artifact_name: str, missing: str
) -> None:
    bundle = _bundle(tmp_path)
    raw = _read_ref(bundle["recovery"][artifact_name])
    del raw[missing]
    bundle["recovery"][artifact_name] = _json_ref(
        tmp_path, f"recovery-{artifact_name}-no-{missing}.json", raw
    )
    with pytest.raises(evidence.EvidenceError, match="keys differ"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_recovery_rejects_nonquiescent_safety_preflight(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["recovery"]["preflight"])
    preflight["autopipe_quiescent"] = False
    bundle["recovery"]["preflight"] = _json_ref(
        tmp_path, "recovery-nonquiescent.json", preflight
    )
    with pytest.raises(evidence.EvidenceError, match="mutation-head boundary"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize(
    ("field", "timestamp"),
    [
        ("started_at", "2026-07-15T11:39:59Z"),
        ("finished_at", "2026-07-15T11:50:01Z"),
    ],
)
def test_recovery_rejects_invalid_chronology(
    tmp_path: Path, field: str, timestamp: str
) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["recovery"]["receipt"])
    receipt[field] = timestamp
    bundle["recovery"]["receipt"] = _json_ref(
        tmp_path, f"recovery-time-{field}.json", receipt
    )
    with pytest.raises(evidence.EvidenceError, match="chronology"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize("artifact_name", ["preflight", "receipt"])
def test_recovery_rejects_mutation_head_drift(
    tmp_path: Path, artifact_name: str
) -> None:
    bundle = _bundle(tmp_path)
    raw = _read_ref(bundle["recovery"][artifact_name])
    raw["mutation_head_sha"] = "f" * 40
    bundle["recovery"][artifact_name] = _json_ref(
        tmp_path, f"recovery-head-{artifact_name}.json", raw
    )
    with pytest.raises(evidence.EvidenceError, match="boundary"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize("artifact_name", ["preflight", "receipt"])
def test_recovery_rejects_target_drift(tmp_path: Path, artifact_name: str) -> None:
    bundle = _bundle(tmp_path)
    raw = _read_ref(bundle["recovery"][artifact_name])
    raw["target"]["chunk_name"] = "_hyper_3_other_chunk"
    bundle["recovery"][artifact_name] = _json_ref(
        tmp_path, f"recovery-target-{artifact_name}.json", raw
    )
    with pytest.raises(evidence.EvidenceError, match="exact authorized chunk"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_recovery_rejects_row_parity_failure(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["recovery"]["receipt"])
    receipt["after_row_count"] += 1
    bundle["recovery"]["receipt"] = _json_ref(
        tmp_path, "recovery-row-drift.json", receipt
    )
    with pytest.raises(evidence.EvidenceError, match="row parity"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize(
    ("artifact_name", "field", "value", "message"),
    [
        ("preflight", "before_compressed", False, "compressed-to-decompressed"),
        ("receipt", "after_compressed", True, "compressed-to-decompressed"),
        ("receipt", "exit_code", 1, "exact target relation"),
        (
            "receipt",
            "decompress_return_relation",
            "_timescaledb_internal._hyper_3_other_chunk",
            "exact target relation",
        ),
    ],
)
def test_recovery_rejects_false_state_or_result(
    tmp_path: Path,
    artifact_name: str,
    field: str,
    value: Any,
    message: str,
) -> None:
    bundle = _bundle(tmp_path)
    raw = _read_ref(bundle["recovery"][artifact_name])
    raw[field] = value
    bundle["recovery"][artifact_name] = _json_ref(
        tmp_path, f"recovery-{artifact_name}-{field}.json", raw
    )
    with pytest.raises(evidence.EvidenceError, match=message):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_recovery_rejects_insufficient_free_space(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["recovery"]["preflight"])
    preflight["free_bytes"] = evidence.MIN_FREE_BYTES - 1
    bundle["recovery"]["preflight"] = _json_ref(
        tmp_path, "recovery-low-space.json", preflight
    )
    with pytest.raises(evidence.EvidenceError, match="below 300 GiB"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_selection_must_reselect_exact_recovered_target(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    for observation in ("post_dry_run", "pre_enforce"):
        snapshot = _read_ref(bundle["selection"][observation])
        snapshot["candidates"][0]["chunk_name"] = "_hyper_3_70_chunk"
        snapshot["selected"][0]["chunk_name"] = "_hyper_3_70_chunk"
        bundle["selection"][observation] = _json_ref(
            tmp_path, f"selection-{observation}-other-target.json", snapshot
        )
    with pytest.raises(evidence.EvidenceError, match="exact recovered chunk"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize("receipt_name", ["dry_run", "enforce"])
def test_replay_receipts_must_reselect_exact_recovered_target(
    tmp_path: Path, receipt_name: str
) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["receipts"][receipt_name])
    receipt["selected"][0]["chunk_name"] = "_hyper_3_other_chunk"
    bundle["receipts"][receipt_name] = _json_ref(
        tmp_path, f"{receipt_name}-other-target.json", receipt
    )
    with pytest.raises(evidence.EvidenceError, match="artifact association|selected tuples differ"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_recovery_authorization_and_truth_flag_are_required(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle["authorization"]["replay_decompression"] = False
    with pytest.raises(evidence.EvidenceError, match="authorization differs"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )
    bundle = _bundle(tmp_path)
    bundle["out_of_scope"]["decompress_run"] = False
    with pytest.raises(evidence.EvidenceError, match="out_of_scope"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


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
    selection["cutoff"] = "2026-07-08T11:59:00Z"
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


def test_selector_cutoff_is_derived_and_strict(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    snapshot = _read_ref(bundle["selection"]["pre_enforce"])
    snapshot["cutoff"] = "2026-07-08T12:00:21Z"
    bundle["selection"]["pre_enforce"] = _json_ref(
        tmp_path, "selection-future-cutoff.json", snapshot
    )
    with pytest.raises(evidence.EvidenceError, match="observed_at minus"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


@pytest.mark.parametrize("failure", ["reused", "nonzero", "timeout"])
def test_execution_ledgers_reject_false_migration_or_timeout_proof(
    tmp_path: Path, failure: str
) -> None:
    bundle = _bundle(tmp_path)
    if failure == "reused":
        bundle["migration"]["second_invocation"] = bundle["migration"]["first_invocation"]
    else:
        key = "first_invocation" if failure == "nonzero" else "second_invocation"
        invocation = _read_ref(bundle["migration"][key])
        if failure == "nonzero":
            invocation["exit_code"] = 1
        else:
            invocation["timeout_seconds"] = 901
        bundle["migration"][key] = _json_ref(
            tmp_path, f"migration-{failure}.json", invocation
        )
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_dry_run_totals_are_recomputed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["receipts"]["dry_run"])
    receipt["per_table_totals"]["hydro.river_timeseries"]["chunks_compressed"] = 1
    bundle["receipts"]["dry_run"] = _json_ref(tmp_path, "dry-bad-totals.json", receipt)
    invocation = _read_ref(bundle["receipts"]["dry_run_invocation"])
    invocation["artifact_bindings"]["receipt_sha256"] = bundle["receipts"]["dry_run"][
        "sha256"
    ]
    bundle["receipts"]["dry_run_invocation"] = _json_ref(
        tmp_path, "dry-bad-totals-invocation.json", invocation
    )
    with pytest.raises(evidence.EvidenceError, match="dry-run per_table_totals"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_decoy_plan_cannot_split_provider_from_selected_relation(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        document["queries"][0]["after"]["measurements"][0]["plan"] = {
            "Node Type": "Append",
            "Plans": [
                {"Node Type": "Custom Scan", "Custom Plan Provider": "DecompressChunk"},
                {"Node Type": "Index Scan", "Relation Name": IDENTITY["chunk_name"]},
            ],
        }

    bundle = _mutated_benchmark_bundle(tmp_path, mutate)
    with pytest.raises(evidence.EvidenceError, match="after measurement 0"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_preexisting_selected_relation_is_not_a_transition(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    pre = _read_ref(bundle["sizes"]["pre"])
    pre["tables"]["hydro.river_timeseries"]["compressed_relations"] = [
        {
            "origin_chunk_schema": IDENTITY["chunk_schema"],
            "origin_chunk_name": IDENTITY["chunk_name"],
            "schema": "_timescaledb_internal",
            "name": "compress_hyper_7_15_chunk",
            "bytes": 134_119_424,
        }
    ]
    pre["tables"]["hydro.river_timeseries"]["compressed_chunks"] = 1
    bundle["sizes"]["pre"] = _json_ref(tmp_path, "sizes-preexisting.json", pre)
    with pytest.raises(evidence.EvidenceError, match="already existed"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_dump_magic_and_cleanup_execstart_are_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    dump = tmp_path / "fake.dump"
    dump.write_bytes(b"random bytes")
    bundle["preflight"]["schema_dump"] = _file_ref(dump)
    listing = _read_ref(bundle["preflight"]["schema_dump_list"])
    listing["dump_sha256"] = bundle["preflight"]["schema_dump"]["sha256"]
    bundle["preflight"]["schema_dump_list"] = _json_ref(
        tmp_path, "fake-dump-list.json", listing
    )
    monkeypatch.setattr(
        evidence,
        "_run_pg_restore_list",
        lambda _identity: (_ for _ in ()).throw(
            evidence.EvidenceError("pinned pg_restore inspection failed")
        ),
    )
    with pytest.raises(evidence.EvidenceError, match="pg_restore"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )

    monkeypatch.setattr(
        evidence,
        "_run_pg_restore_list",
        lambda identity: _pg_restore_record(identity.sha256),
    )
    bundle = _bundle(tmp_path)
    cleanup = _read_ref(bundle["cleanup"]["evidence"])
    cleanup["resolved_exec_start"].remove("--enforce")
    bundle["cleanup"]["evidence"] = _json_ref(tmp_path, "cleanup-no-enforce.json", cleanup)
    with pytest.raises(evidence.EvidenceError, match="--enforce"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_evidence_reader_rejects_symlink_oversize_depth_and_credentials(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    original = Path(bundle["receipts"]["dry_run"]["path"])
    link = tmp_path / "dry-link.json"
    link.symlink_to(original)
    bundle["receipts"]["dry_run"] = {
        **bundle["receipts"]["dry_run"],
        "path": str(link),
    }
    with pytest.raises(evidence.EvidenceError, match="unsafe|symlink"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )

    bundle = _bundle(tmp_path)
    bundle["benchmarks"]["evidence"]["bytes"] = evidence.MAX_JSON_ARTIFACT_BYTES + 1
    with pytest.raises(evidence.EvidenceError, match="byte ceiling"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )

    bundle = _bundle(tmp_path)
    nested: Any = {"leaf": True}
    for _ in range(evidence.MAX_PLAN_DEPTH + 2):
        nested = {"next": nested}
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "too-deep.json", nested)
    with pytest.raises(evidence.EvidenceError, match="depth ceiling"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )

    bundle = _bundle(tmp_path)
    cleanup = _read_ref(bundle["cleanup"]["evidence"])
    cleanup["api_key"] = "must-never-be-echoed"
    bundle["cleanup"]["evidence"] = _json_ref(tmp_path, "cleanup-secret.json", cleanup)
    with pytest.raises(evidence.EvidenceError, match="forbidden credential field"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_weak_schema_and_wrong_request_range_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(evidence.EvidenceError, match="canonical verifier checkout schema"):
        evidence.verify_bundle(
            _bundle(tmp_path), receipt_schema={}, verifier_head_sha=VERIFIER_HEAD
        )

    def mutate(document: dict[str, Any]) -> None:
        document["queries"][1]["request"]["valid_time"] = "2026-06-05T00:00:00Z"

    bundle = _mutated_benchmark_bundle(tmp_path, mutate)
    with pytest.raises(evidence.EvidenceError, match="public production owner|selected chunk range"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_global_chronology_rejects_benchmark_before_post_dry(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    document = _read_ref(bundle["benchmarks"]["evidence"])
    for query_index, query in enumerate(document["queries"]):
        phase = query["before"]
        offset = query_index * 5
        phase["execution_bounds"]["started_at"] = f"2026-07-15T11:55:0{offset}Z"
        phase["execution_bounds"]["finished_at"] = f"2026-07-15T11:55:0{offset + 4}Z"
        for index, activity in enumerate(phase["activity_samples"]):
            activity["captured_at"] = f"2026-07-15T11:55:0{offset + index}Z"
    bundle["benchmarks"]["evidence"] = _json_ref(
        tmp_path, "benchmark-reversed.json", document
    )
    with pytest.raises(evidence.EvidenceError, match="global chronology"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_v3_terminal_retains_provenance_and_v2_cannot_qualify(tmp_path: Path) -> None:
    terminal = evidence.verify_bundle(
        _bundle(tmp_path), receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
    )
    assert terminal["qualifies_task_4_5"] is True
    assert terminal["preflight"]["schema_dump_list"]
    assert terminal["recovery"]["invocation"]
    assert terminal["migration"]["first_invocation"]
    assert terminal["migration"]["second_invocation"]
    assert terminal["receipts"]["dry_run_invocation"]
    assert terminal["receipts"]["enforce_invocation"]
    missing = json.loads(json.dumps(terminal))
    del missing["receipts"]["enforce_invocation"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing, EVIDENCE_SCHEMA)

    legacy = json.loads(
        (ROOT / "docs/runbooks/receipts/tier-node27-timeseries-storage/"
        "timeseries-compression/terminal-replay-20260715T114625Z.json").read_text()
    )
    legacy["qualifies_task_4_5"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(legacy, EVIDENCE_SCHEMA)


@pytest.mark.parametrize("hardlink", [False, True])
def test_terminal_output_alias_preserves_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hardlink: bool
) -> None:
    bundle = _bundle(tmp_path)
    bundle_path = tmp_path / "bundle-alias.json"
    bundle_path.write_bytes(_canonical(bundle))
    input_path = Path(bundle["receipts"]["enforce"]["path"])
    original = input_path.read_bytes()
    output = input_path
    if hardlink:
        output = tmp_path / "terminal-hardlink.json"
        output.hardlink_to(input_path)
    monkeypatch.setattr(evidence, "_current_verifier_head", lambda: VERIFIER_HEAD)
    assert evidence.main(["--bundle-path", str(bundle_path), "--output-path", str(output)]) == 1
    assert input_path.read_bytes() == original


def test_terminal_failure_replaces_stale_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _bundle(tmp_path)
    bundle["authorization"]["bound"] = 2
    bundle_path = tmp_path / "bad-bundle.json"
    bundle_path.write_bytes(_canonical(bundle))
    output = tmp_path / "terminal.json"
    output.write_text('{"verdict":"PASS_TASK_4_5"}\n', encoding="utf-8")
    monkeypatch.setattr(evidence, "_current_verifier_head", lambda: VERIFIER_HEAD)
    assert evidence.main(["--bundle-path", str(bundle_path), "--output-path", str(output)]) == 1
    marker = json.loads(output.read_text(encoding="utf-8"))
    assert marker["qualifies_task_4_5"] is False
    assert marker["outcome"] == "failed"
    jsonschema.validate(marker, EVIDENCE_SCHEMA)


@pytest.mark.parametrize(
    "plan",
    [
        {
            "Node Type": "Custom Scan",
            "Custom Plan Provider": "DecompressChunk",
            "Relation Name": f"prefix_{IDENTITY['chunk_name']}",
        },
        {
            "Node Type": "Custom Scan",
            "Custom Plan Provider": "DecompressChunk",
            "Filter": f"Relation Name: {IDENTITY['chunk_name']}",
        },
        {
            "Node Type": "Custom Scan",
            "Custom Plan Provider": "DecompressChunk",
            "Plans": [
                {"Node Type": "Index Scan", "Relation Name": IDENTITY["chunk_name"]}
            ],
        },
    ],
)
def test_plan_suffix_filter_and_child_decoys_fail(
    tmp_path: Path, plan: dict[str, Any]
) -> None:
    bundle = _bundle(tmp_path)
    document = _read_ref(bundle["benchmarks"]["evidence"])
    document["queries"][0]["after"]["measurements"][0]["plan"] = plan
    bundle["benchmarks"]["evidence"] = _json_ref(
        tmp_path, "plan-decoy-v2.json", document
    )
    with pytest.raises(evidence.EvidenceError, match="lacks selected DecompressChunk"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_snapshot_bijection_rejects_cross_table_sibling_reuse(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    post = _read_ref(bundle["sizes"]["post"])
    copied = dict(post["tables"]["hydro.river_timeseries"]["compressed_relations"][0])
    copied["origin_chunk_name"] = "_hyper_2_20_chunk"
    post["tables"]["met.forcing_station_timeseries"]["compressed_chunks"] = 1
    post["tables"]["met.forcing_station_timeseries"]["compressed_relations"] = [copied]
    bundle["sizes"]["post"] = _json_ref(tmp_path, "cross-table-sibling.json", post)
    with pytest.raises(evidence.EvidenceError, match="bijection"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_repo_path_and_remote_lineage_are_pinned(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["preflight"]["evidence"])
    preflight["repo_path"] = "/tmp/unrelated"
    bundle["preflight"]["evidence"] = _json_ref(tmp_path, "wrong-repo.json", preflight)
    with pytest.raises(evidence.EvidenceError, match="mutation-head boundary"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )

    bundle = _bundle(tmp_path)
    bundle["authorization"]["remote_identity"] = "attacker/repo"
    with pytest.raises(evidence.EvidenceError, match="authorization differs"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_execution_audit_rejects_extra_or_direct_db_invocation(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    audit = _read_ref(bundle["execution"]["audit"])
    audit["direct_db_mutation_statements"] = ["compress_chunk"]
    bundle["execution"]["audit"] = _json_ref(tmp_path, "direct-db.json", audit)
    with pytest.raises(evidence.EvidenceError, match="direct-DB"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_text_journal_secret_assignment_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    cleanup = _read_ref(bundle["cleanup"]["evidence"])
    journal = tmp_path / "secret-journal.log"
    journal.write_text("status=ok token=never-print-this\n", encoding="utf-8")
    cleanup["final_units"]["nhms-node27-autopipe.service"]["journal"] = _file_ref(journal)
    bundle["cleanup"]["evidence"] = _json_ref(tmp_path, "secret-journal.json", cleanup)
    with pytest.raises(evidence.EvidenceError, match="credential") as caught:
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )
    assert "never-print-this" not in str(caught.value)


def test_curve_window_starting_at_selected_exclusive_end_is_rejected(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    document = _read_ref(bundle["benchmarks"]["evidence"])
    query = document["queries"][0]
    issue_time = datetime(2026, 6, 4, tzinfo=UTC)
    end_time = datetime(2026, 6, 11, tzinfo=UTC)
    query_text, names, parameters = benchmark._curve_query_and_binding(
        basin_version_id=query["request"]["basin_version_id"],
        river_segment_id=query["request"]["river_segment_id"],
        river_network_version_id=query["request"]["river_network_version_id"],
        issue_time=issue_time,
        end_time=end_time,
        scenario=query["request"]["scenario"],
    )
    query["request"]["issue_time"] = "2026-06-04T00:00:00Z"
    query["request"]["end_time"] = "2026-06-11T00:00:00Z"
    query["query_text"] = query_text
    query["query_sha256"] = hashlib.sha256(query_text.encode()).hexdigest()
    query["binding"] = {
        "parameter_names": names,
        "bound_parameters": benchmark._json_value(parameters),
    }
    bundle["benchmarks"]["evidence"] = _json_ref(
        tmp_path, "exclusive-end-curve.json", document
    )
    with pytest.raises(evidence.EvidenceError, match="selected chunk range"):
        evidence.verify_bundle(
            bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD
        )


def test_retained_reference_change_after_publish_replaces_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    bundle_path = tmp_path / "bundle-retained.json"
    bundle_path.write_bytes(_canonical(bundle))
    output = tmp_path / "terminal.json"
    retained = Path(bundle["receipts"]["dry_run"]["path"])
    original_publish = evidence.atomic_write_bytes_no_follow
    changed = False

    def publish(path: Path, payload: bytes, **kwargs: Any) -> None:
        nonlocal changed
        original_publish(path, payload, **kwargs)
        if path == output and not changed:
            retained.write_text("{}\n", encoding="utf-8")
            changed = True

    monkeypatch.setattr(evidence, "atomic_write_bytes_no_follow", publish)
    monkeypatch.setattr(evidence, "_current_verifier_head", lambda: VERIFIER_HEAD)
    assert evidence.main(["--bundle-path", str(bundle_path), "--output-path", str(output)]) == 1
    marker = json.loads(output.read_text(encoding="utf-8"))
    assert marker["qualifies_task_4_5"] is False
