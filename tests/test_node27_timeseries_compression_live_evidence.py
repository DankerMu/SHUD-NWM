"""Focused contract tests for issue #1069's independent live verifier."""

from __future__ import annotations

import atexit
import copy
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import jsonschema
import pytest

from apps.api.routes.hydro_display import _postgis_tile_params
from packages.common import compression_terminal_state as terminal_state
from packages.common.evidence_io import resolve_artifact_closure
from scripts import node27_timeseries_compression_benchmark as benchmark
from scripts import node27_timeseries_compression_live_evidence as evidence
from scripts import node27_timeseries_compression_supervisor as supervisor
from services.tiles.mvt import postgis_tile_sql

ROOT = Path(__file__).resolve().parents[1]
# Captured before the autouse `_descriptor_bound_git_blobs` fixture replaces the
# module attribute, so the real Git-backed producer can be exercised directly.
_REAL_GIT_BLOB_BYTES = evidence._git_blob_bytes
# Captured before the autouse `_owned_provenance_lineage` fixture repoints the
# provenance seam, so the source-level default can be asserted.
_DEFAULT_PROVENANCE_REPO_ROOT = evidence.PROVENANCE_REPO_ROOT
_DEFAULT_VERIFIER_REPO_ROOT = evidence.VERIFIER_REPO_ROOT
RECEIPT_SCHEMA = json.loads((ROOT / "schemas/timeseries_compression_receipt.schema.json").read_text(encoding="utf-8"))
EVIDENCE_SCHEMA = json.loads(
    (ROOT / "schemas/timeseries_compression_live_evidence.schema.json").read_text(encoding="utf-8")
)


def _git(root: Path, *args: str) -> str:
    """Run git against a repository this suite owns, isolated from ambient git state."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "issue-1069 provenance fixture",
            "GIT_AUTHOR_EMAIL": "provenance@example.invalid",
            "GIT_COMMITTER_NAME": "issue-1069 provenance fixture",
            "GIT_COMMITTER_EMAIL": "provenance@example.invalid",
        }
    )
    return subprocess.run(
        ["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _build_provenance_repo() -> tuple[Path, str]:
    """Build the reviewed origin lineage the verifier's provenance contract asks git about.

    `_validate_repository_provenance` asks real git whether the mutation SHA is exactly
    the tip of a reviewed, pushed `refs/remotes/origin/*` ref on the expected remote.
    Asking that of the ambient checkout only answers "yes" when the developer happens to
    be sitting on the pushed tip of one branch, so the suite would report on push state
    rather than on the contract, and would fail outright on a pull_request merge ref or
    once the branch is deleted.  Owning a purpose-built repository makes the SHA, the
    ref and the remote inputs this suite controls, so every lineage outcome -- including
    the negative ones -- can be driven deliberately.
    """
    root = Path(tempfile.mkdtemp(prefix="node27-1069-provenance-"))
    atexit.register(shutil.rmtree, root, True)
    _git(root, "init", "--quiet")
    (root / "reviewed.txt").write_text("reviewed mutation lineage\n", encoding="utf-8")
    _git(root, "add", "reviewed.txt")
    _git(root, "commit", "--quiet", "--message", "reviewed mutation")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "remote", "add", "origin", f"git@github.com:{evidence.EXPECTED_REMOTE_IDENTITY}.git")
    _git(root, "update-ref", evidence.EXPECTED_REVIEWED_REMOTE_REF, head)
    return root, head


PROVENANCE_REPO, HEAD = _build_provenance_repo()
VERIFIER_HEAD = "89abcdef0123456789abcdef0123456789abcdef"
INVOCATION_ID = "1" * 32
IDENTITY = {
    "hypertable_schema": "hydro",
    "hypertable_name": "river_timeseries",
    "chunk_schema": "_timescaledb_internal",
    "chunk_name": "_hyper_3_7_chunk",
    "range_start": "2026-05-28T00:00:00Z",
    "range_end": "2026-06-04T00:00:00Z",
}


def _intent_context() -> dict[str, str]:
    return {
        "schema_version": evidence.QUALIFYING_SCHEMA_VERSION,
        "provenance_state": "bound",
        "run_id": "run-1069",
        "verifier_head_sha": VERIFIER_HEAD,
        "mutation_head_sha": HEAD,
    }


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _json_ref(tmp_path: Path, name: str, value: Any) -> dict[str, Any]:
    path = tmp_path / name
    raw = _canonical(value)
    path.write_bytes(raw)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _file_ref(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _observed(ref: dict[str, Any]) -> dict[str, Any]:
    info = os.stat(ref["path"], follow_symlinks=False)
    return {"artifact": ref, "device": info.st_dev, "inode": info.st_ino}


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
    version_stdout = b"pg_restore (PostgreSQL) 15.2\n"
    return {
        "dump_descriptor_sha256": dump_sha256,
        "container_image_id": "sha256:" + "1" * 64,
        # Anchored to the shared measured contract, never re-hard-coded, so the
        # fixture can never drift from the verifier's own pinned realpath.
        "binary_realpath": evidence.CONTAINER_PG_RESTORE_REALPATH,
        "binary_sha256": "2" * 64,
        "version_argv": ["/usr/bin/docker", "exec", "nhms-db", "/usr/bin/pg_restore", "--version"],
        "list_argv": [
            "/usr/bin/docker",
            "exec",
            "nhms-db",
            "/usr/bin/pg_restore",
            "--list",
            "/var/lib/postgresql/evidence/schema.dump",
        ],
        "exit_code": 0,
        "tool_version": "pg_restore (PostgreSQL) 15.2",
        "version_stdout_sha256": hashlib.sha256(version_stdout).hexdigest(),
        "version_stdout_bytes": len(version_stdout),
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
def _descriptor_bound_git_blobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evidence,
        "_git_blob_bytes",
        lambda _head, relative_path, _label: (ROOT / relative_path).read_bytes(),
    )


@pytest.fixture(autouse=True)
def _owned_provenance_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit the lineage this suite built rather than the ambient checkout's."""
    monkeypatch.setattr(evidence, "PROVENANCE_REPO_ROOT", PROVENANCE_REPO)


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
        "selected_origin_uncompressed_index": None if post else -1,
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
        },
    }


def _measurement(*, name: str, after: bool, execution_ms: float, read_blocks: int = 0) -> dict[str, Any]:
    plan_tree: dict[str, Any] = {
        "Node Type": "Index Scan",
        "Relation Name": "river_timeseries",
        "Schema": "hydro",
        "Alias": "river_timeseries",
        "Shared Hit Blocks": 10,
        "Shared Read Blocks": read_blocks,
    }
    if after:
        plan_tree = {
            "Node Type": "Custom Scan",
            "Custom Plan Provider": "DecompressChunk",
            "Relation Name": IDENTITY["chunk_name"],
            "Schema": "_timescaledb_internal",
            "Alias": "rt_1",
            "Query": name,
            "Shared Hit Blocks": 10,
            "Shared Read Blocks": read_blocks,
        }
    plan = {"Planning Time": 1.0, "Execution Time": execution_ms, "Plan": plan_tree}
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
        "measurements": [_measurement(name=name, after=after, execution_ms=sample) for sample in samples],
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
            "started_at": ("2026-07-15T12:11:00Z" if name == "mvt" else "2026-07-15T12:10:00Z")
            if after
            else ("2026-07-15T12:00:17Z" if name == "mvt" else "2026-07-15T12:00:12Z"),
            "finished_at": ("2026-07-15T12:11:04Z" if name == "mvt" else "2026-07-15T12:10:04Z")
            if after
            else ("2026-07-15T12:00:21Z" if name == "mvt" else "2026-07-15T12:00:16Z"),
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
        "execution_bounds": {
            "before": {
                "started_at": "2026-07-15T12:00:11Z",
                "finished_at": "2026-07-15T12:00:22Z",
                "wall_seconds": 900,
            },
            "after": {
                "started_at": "2026-07-15T12:09:59Z",
                "finished_at": "2026-07-15T12:11:05Z",
                "wall_seconds": 900,
            },
        },
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
                    [_file_ref(curve_source)] if name == "curve" else [_file_ref(mvt_source), _file_ref(route_source)]
                ),
                "query_sha256": hashlib.sha256((curve_query if name == "curve" else mvt_query).encode()).hexdigest(),
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
        ],
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
        "window_started_at": "2026-07-15T11:40:01Z",
        "window_finished_at": "2026-07-15T12:20:00Z",
        "repo_units": {
            "service": _file_ref(repo_service),
            "timer": _file_ref(repo_timer),
        },
        "installed_units": {
            "service": _file_ref(installed_service),
            "timer": _file_ref(installed_timer),
        },
        "installed_unit_paths": {
            "service": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.service",
            "timer": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.timer",
        },
        "resolved_exec_start": [
            "/home/nwm/NWM/.venv/bin/python",
            "/home/nwm/NWM/scripts/node27_timeseries_compression_supervisor.py",
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
    recovery_preflight_ref = _json_ref(tmp_path, "recovery-preflight.json", recovery_preflight)
    recovery_receipt_ref = _json_ref(tmp_path, "recovery-receipt.json", recovery_receipt)
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
            "captured_at": "2026-07-15T11:31:00.500000Z",
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
            "captured_at": "2026-07-15T11:32:00.500000Z",
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
    bundle = {
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
            "sole_db_user_during_window": True,
            "database_audit_proof": False,
            "acceptance_claim": evidence.PASS_CLAIM,
            "repo_path": "/home/nwm/NWM",
            "remote_identity": "DankerMu/SHUD-NWM",
            "reviewed_mutation_sha": HEAD,
            "reviewed_remote_ref": evidence.EXPECTED_REVIEWED_REMOTE_REF,
        },
        "execution": {},
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
                    "catalog": catalog,
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
            "post_dry_run": _json_ref(tmp_path, "selection-post-dry-run.json", post_dry_selection),
            "pre_enforce": _json_ref(tmp_path, "selection-pre-enforce.json", pre_enforce_selection),
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
    benchmark_common = [
        "--curve-basin-version-id",
        "basins_heihe_vbasins",
        "--curve-river-segment-id",
        "basins_heihe_shud_reach_000001",
        "--curve-river-network-version-id",
        "basins_heihe_rivnet_vbasins",
        "--curve-issue-time",
        "2026-05-31T06:00:00Z",
        "--curve-end-time",
        "2026-06-07T06:00:00Z",
        "--curve-scenario",
        "forecast_gfs_deterministic",
        "--mvt-run-id",
        "fcst_gfs_2026053106_basins_heihe_shud",
        "--mvt-basin-version-id",
        "basins_heihe_vbasins",
        "--mvt-river-network-version-id",
        "basins_heihe_rivnet_vbasins",
        "--mvt-valid-time",
        "2026-05-31T06:00:00Z",
        "--mvt-z",
        "9",
        "--mvt-x",
        "399",
        "--mvt-y",
        "189",
    ]
    commands = [
        (
            "pg-dump",
            "pg_dump",
            [
                "/usr/bin/pg_dump",
                "--dbname",
                "nhms",
                "--format=custom",
                "--schema-only",
                "--file",
                bundle["preflight"]["schema_dump"]["path"],
            ],
        ),
        (
            "pg-restore-version",
            "pg_restore_version",
            ["/usr/bin/docker", "exec", "nhms-db", "/usr/bin/pg_restore", "--version"],
        ),
        (
            "pg-restore-list",
            "pg_restore_list",
            [
                "/usr/bin/docker",
                "exec",
                "nhms-db",
                "/usr/bin/pg_restore",
                "--list",
                "/var/lib/postgresql/evidence/schema.dump",
            ],
        ),
        (
            "migration-1",
            "migration_apply",
            [
                "/usr/bin/psql",
                "--dbname",
                "nhms",
                "--no-psqlrc",
                "--set",
                "ON_ERROR_STOP=1",
                "--file",
                "/home/nwm/NWM/db/migrations/000047_hypertable_compression_settings.sql",
            ],
        ),
        (
            "migration-2",
            "migration_apply",
            [
                "/usr/bin/psql",
                "--dbname",
                "nhms",
                "--no-psqlrc",
                "--set",
                "ON_ERROR_STOP=1",
                "--file",
                "/home/nwm/NWM/db/migrations/000047_hypertable_compression_settings.sql",
            ],
        ),
        (
            "decompress",
            "decompress",
            [
                "/home/nwm/NWM/.venv/bin/python",
                "/home/nwm/NWM/scripts/node27_timeseries_decompression_replay.py",
                "--database",
                "nhms",
                "--mutation-head-sha",
                HEAD,
                "--receipt-path",
                str(tmp_path / "recovery-receipt.json"),
                "--hypertable-schema",
                "hydro",
                "--hypertable-name",
                "river_timeseries",
                "--chunk-schema",
                "_timescaledb_internal",
                "--chunk-name",
                "_hyper_3_7_chunk",
                "--range-start",
                "2026-05-28T00:00:00Z",
                "--range-end",
                "2026-06-04T00:00:00Z",
            ],
        ),
        (
            "dry-run",
            "compression_dry_run",
            [
                "/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh",
                "--receipt-path",
                str(tmp_path / "dry.json"),
                "--lock-path",
                "/home/nwm/node27-timeseries-compression-replay/compression.lock",
            ],
        ),
        (
            "benchmark-before",
            "benchmark_before",
            [
                "/home/nwm/NWM/.venv/bin/python",
                "/home/nwm/NWM/scripts/node27_timeseries_compression_benchmark.py",
                "--phase",
                "before",
                "--output",
                str(tmp_path / "benchmark-before.json"),
                *benchmark_common,
            ],
        ),
        (
            "enforce",
            "compression_enforce",
            [
                "/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh",
                "--enforce",
                "--receipt-path",
                str(tmp_path / "enforce.json"),
                "--lock-path",
                "/home/nwm/node27-timeseries-compression-replay/compression.lock",
            ],
        ),
        (
            "benchmark-after",
            "benchmark_after",
            [
                "/home/nwm/NWM/.venv/bin/python",
                "/home/nwm/NWM/scripts/node27_timeseries_compression_benchmark.py",
                "--phase",
                "after",
                "--before-path",
                "/home/nwm/node27-timeseries-compression-replay/benchmark-before.json",
                "--output",
                bundle["benchmarks"]["evidence"]["path"],
                *benchmark_common,
            ],
        ),
    ]
    produced_refs = {
        "preflight_evidence": bundle["preflight"]["evidence"],
        "schema_dump": bundle["preflight"]["schema_dump"],
        "schema_dump_list": bundle["preflight"]["schema_dump_list"],
        "catalog_before": bundle["preflight"]["catalog_before"],
        "catalog_after_first": bundle["migration"]["catalog_after_first"],
        "catalog_after_second": bundle["migration"]["catalog_after_second"],
        "recovery_preflight": bundle["recovery"]["preflight"],
        "recovery_receipt": bundle["recovery"]["receipt"],
        "dry_run_receipt": bundle["receipts"]["dry_run"],
        "post_dry_selection": bundle["selection"]["post_dry_run"],
        "pre_enforce_selection": bundle["selection"]["pre_enforce"],
        "enforce_receipt": bundle["receipts"]["enforce"],
        "sizes_pre": bundle["sizes"]["pre"],
        "sizes_post": bundle["sizes"]["post"],
        "catalog_post": bundle["catalog"]["post"],
        "benchmarks": bundle["benchmarks"]["evidence"],
        "cleanup": bundle["cleanup"]["evidence"],
        "benchmark_before": _json_ref(tmp_path, "benchmark-before.json", {"phase": "before"}),
    }
    association_names = {
        "pg-dump": ["schema_dump"],
        "decompress": ["recovery_receipt"],
        "dry-run": ["dry_run_receipt"],
        "benchmark-before": ["benchmark_before"],
        "enforce": ["enforce_receipt"],
        "benchmark-after": ["benchmarks"],
    }
    planned_commands = [
        {
            "command_id": command_id,
            "kind": kind,
            "argv": argv,
            "artifact_associations": {
                name: produced_refs[name]["path"] for name in association_names.get(command_id, [])
            },
        }
        for command_id, kind, argv in commands
    ]
    dump_tool_association = {
        "dump_sha256": bundle["preflight"]["schema_dump"]["sha256"],
        "container_image_id": "sha256:" + "1" * 64,
        # Anchored to the shared measured contract (matches the dump listing).
        "binary_realpath": evidence.CONTAINER_PG_RESTORE_REALPATH,
        "binary_sha256": "2" * 64,
    }
    mutation_ids = ["migration-1", "migration-2", "decompress", "enforce"]
    captures = [
        {
            "capture_id": f"capture-{kind}",
            "kind": kind,
            "argv": ["/usr/bin/printf", "{}"],
            "output_path": produced_refs[kind]["path"],
        }
        for kind in evidence.EXPECTED_CAPTURE_SEQUENCE
    ]
    checkpoints = [
        {"checkpoint_id": "preflight", "phase": "preflight", "command_id": None},
        {"checkpoint_id": "postflight", "phase": "postflight", "command_id": None},
        {"checkpoint_id": "cleanup", "phase": "cleanup", "command_id": None},
        *[
            {"checkpoint_id": f"{phase}-{command_id}", "phase": phase, "command_id": command_id}
            for command_id in mutation_ids
            for phase in ("before_mutation", "after_mutation")
        ],
    ]
    plan = {
        "plan_version": "1.0",
        "run_plan_id": "",
        "mutation_head_sha": HEAD,
        "reviewed_remote_ref": evidence.EXPECTED_REVIEWED_REMOTE_REF,
        "database": "nhms",
        "repo_path": "/home/nwm/NWM",
        "operator_attestation": {
            "sole_db_user_during_window": True,
            "database_audit_proof": False,
            "trust_limit": "discrete observations; no absolute direct-SQL bypass proof",
        },
        "commands": planned_commands,
        "captures": captures,
        "checkpoints": checkpoints,
    }
    plan["run_plan_id"] = evidence._supervisor_run_plan_id(plan)
    child_times = [
        ("2026-07-15T11:19:01Z", "2026-07-15T11:19:02Z"),
        ("2026-07-15T11:19:03Z", "2026-07-15T11:19:04Z"),
        ("2026-07-15T11:19:05Z", "2026-07-15T11:19:06Z"),
        ("2026-07-15T11:30:00Z", "2026-07-15T11:31:00Z"),
        ("2026-07-15T11:31:01Z", "2026-07-15T11:32:00Z"),
        ("2026-07-15T11:41:00Z", "2026-07-15T11:45:00Z"),
        ("2026-07-15T11:59:50Z", "2026-07-15T12:00:00Z"),
        ("2026-07-15T12:00:11Z", "2026-07-15T12:00:21Z"),
        ("2026-07-15T12:00:25Z", "2026-07-15T12:05:01Z"),
        ("2026-07-15T12:10:00Z", "2026-07-15T12:11:04Z"),
    ]
    events: list[dict[str, Any]] = []
    for index, (command, (started_at, finished_at)) in enumerate(zip(planned_commands, child_times, strict=True)):
        child_stdout = b""
        if command["kind"] == "pg_restore_version":
            child_stdout = b"pg_restore (PostgreSQL) 15.2\n"
        elif command["kind"] == "pg_restore_list":
            child_stdout = b"TABLE hydro river_timeseries\nTABLE met forcing_station_timeseries\n"
        stdout_path = tmp_path / f"child-{index}-stdout.bin"
        stderr_path = tmp_path / f"child-{index}-stderr.bin"
        stdout_path.write_bytes(child_stdout)
        stderr_path.write_bytes(b"")
        events.append(
            {
                "schema_version": "3.0",
                "run_id": "run-1069",
                "run_plan_id": plan["run_plan_id"],
                "invocation_id": INVOCATION_ID,
                "supervisor_pid": 4242,
                "event_id": f"child-{index}",
                "event_type": "child_exit",
                "command_id": command["command_id"],
                "kind": command["kind"],
                "argv": command["argv"],
                "pid": 1000 + index,
                "started_at": started_at,
                "finished_at": finished_at,
                "started_monotonic": datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp(),
                "finished_monotonic": datetime.fromisoformat(finished_at.replace("Z", "+00:00")).timestamp(),
                "exit_code": 0,
                "terminated_by_supervisor": False,
                "possible_mutation": False,
                "stdout": {
                    "bytes": len(child_stdout),
                    "sha256": hashlib.sha256(child_stdout).hexdigest(),
                    "truncated": False,
                    "artifact": _observed(_file_ref(stdout_path)),
                },
                "stderr": {
                    "bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "truncated": False,
                    "artifact": _observed(_file_ref(stderr_path)),
                },
                "mutation_head_sha": HEAD,
                "database": "nhms",
                "artifact_associations": (
                    dump_tool_association
                    if command["kind"] == "pg_restore_version"
                    else {
                        **(dump_tool_association if command["kind"] == "pg_restore_list" else {}),
                        **{
                            name: _observed(produced_refs[name])
                            for name in association_names.get(command["command_id"], [])
                        },
                    }
                ),
            }
        )
    capture_times = {
        "preflight_evidence": ("2026-07-15T11:19:00.100000Z", "2026-07-15T11:19:00.200000Z"),
        "schema_dump_list": ("2026-07-15T11:19:06.100000Z", "2026-07-15T11:19:06.200000Z"),
        "catalog_before": ("2026-07-15T11:19:06.300000Z", "2026-07-15T11:19:06.400000Z"),
        "catalog_after_first": ("2026-07-15T11:31:00.300000Z", "2026-07-15T11:31:00.400000Z"),
        "catalog_after_second": ("2026-07-15T11:32:00.300000Z", "2026-07-15T11:32:00.400000Z"),
        "recovery_preflight": ("2026-07-15T11:40:58Z", "2026-07-15T11:40:58.500000Z"),
        "post_dry_selection": ("2026-07-15T12:00:00.100000Z", "2026-07-15T12:00:00.200000Z"),
        "pre_enforce_selection": ("2026-07-15T12:00:22Z", "2026-07-15T12:00:22.500000Z"),
        "sizes_pre": ("2026-07-15T12:00:23Z", "2026-07-15T12:00:23.500000Z"),
        "sizes_post": ("2026-07-15T12:05:02Z", "2026-07-15T12:05:02.500000Z"),
        "catalog_post": ("2026-07-15T12:05:03Z", "2026-07-15T12:05:03.500000Z"),
        "cleanup": ("2026-07-15T12:20:00Z", "2026-07-15T12:20:01Z"),
    }
    for index, capture in enumerate(captures):
        kind = capture["kind"]
        started_at, finished_at = capture_times[kind]
        raw = Path(produced_refs[kind]["path"]).read_bytes()
        stdout_path = tmp_path / f"capture-{index}-stdout.bin"
        stderr_path = tmp_path / f"capture-{index}-stderr.bin"
        stdout_path.write_bytes(raw)
        stderr_path.write_bytes(b"")
        events.append(
            {
                "schema_version": "3.0",
                "run_id": "run-1069",
                "run_plan_id": plan["run_plan_id"],
                "invocation_id": INVOCATION_ID,
                "supervisor_pid": 4242,
                "event_id": f"capture-{index}",
                "event_type": "capture",
                "capture_id": capture["capture_id"],
                "kind": kind,
                "argv": capture["argv"],
                "pid": 2000 + index,
                "started_at": started_at,
                "finished_at": finished_at,
                "started_monotonic": datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp(),
                "finished_monotonic": datetime.fromisoformat(finished_at.replace("Z", "+00:00")).timestamp(),
                "exit_code": 0,
                "terminated_by_supervisor": False,
                "stdout": {
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "truncated": False,
                    "artifact": _observed(_file_ref(stdout_path)),
                },
                "stderr": {
                    "bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "truncated": False,
                    "artifact": _observed(_file_ref(stderr_path)),
                },
                "artifact_association": _observed(produced_refs[kind]),
            }
        )
    checkpoint_times = {
        "preflight": "2026-07-15T11:19:00Z",
        "postflight": "2026-07-15T12:11:05Z",
        "cleanup": "2026-07-15T12:21:00Z",
        "before_mutation-migration-1": "2026-07-15T11:29:59Z",
        "after_mutation-migration-1": "2026-07-15T11:31:00.250000Z",
        "before_mutation-migration-2": "2026-07-15T11:31:00.750000Z",
        "after_mutation-migration-2": "2026-07-15T11:32:00.250000Z",
        "before_mutation-decompress": "2026-07-15T11:40:59Z",
        "after_mutation-decompress": "2026-07-15T11:45:00.250000Z",
        "before_mutation-enforce": "2026-07-15T12:00:24.500000Z",
        "after_mutation-enforce": "2026-07-15T12:05:01.500000Z",
    }
    for checkpoint in checkpoints:
        checkpoint_id = checkpoint["checkpoint_id"]
        captured_at = checkpoint_times[checkpoint_id]
        activity_ref = _json_ref(tmp_path, f"{checkpoint_id}-activity.json", {"sessions": []})
        locks_ref = _json_ref(tmp_path, f"{checkpoint_id}-locks.json", {"conflicts": []})
        checkpoint_catalog_ref = _json_ref(tmp_path, f"{checkpoint_id}-catalog.json", catalog)
        show_ref = _json_ref(
            tmp_path,
            f"{checkpoint_id}-show.json",
            {
                "recurring": {
                    "FragmentPath": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.service",
                    "ActiveState": "inactive",
                    "SubState": "dead",
                    "MainPID": 0,
                    "InvocationID": "",
                    # MEASURED node-27 contract (#1069 gap G6): the inactive
                    # recurring unit renders its unset start timestamp as "n/a".
                    "ExecMainStartTimestamp": evidence.SYSTEMD_UNSET_TIMESTAMP,
                    "ExecMainStartTimestampMonotonic": 0,
                },
                "replay": {
                    "FragmentPath": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression-replay.service",
                    "ActiveState": "activating",
                    "SubState": "start",
                    "MainPID": 4242,
                    "InvocationID": INVOCATION_ID,
                    "ExecMainStartTimestamp": "Tue 2026-07-15 11:18:59 UTC",
                    "ExecMainStartTimestampMonotonic": 1000000,
                },
            },
        )
        journal_path = tmp_path / f"{checkpoint_id}-journal.log"
        journal_path.write_text("cursor-bounded replay observation\n-- cursor: placeholder\n", encoding="utf-8")
        events.append(
            {
                "schema_version": "3.0",
                "run_id": "run-1069",
                "run_plan_id": plan["run_plan_id"],
                "invocation_id": INVOCATION_ID,
                "supervisor_pid": 4242,
                "event_id": f"checkpoint-{checkpoint_id}",
                "event_type": "checkpoint",
                **checkpoint,
                "captured_at": captured_at,
                "monotonic": datetime.fromisoformat(captured_at.replace("Z", "+00:00")).timestamp(),
                "journal_start_cursor": "placeholder",
                "journal_end_cursor": "placeholder",
                "database_activity": _observed(activity_ref),
                "relation_locks": _observed(locks_ref),
                "catalog": _observed(checkpoint_catalog_ref),
                "systemd_show": _observed(show_ref),
                "journal": _observed(_file_ref(journal_path)),
            }
        )
    events.sort(key=lambda event: event.get("started_monotonic", event.get("monotonic")))
    previous_cursor = "cursor-run-start"
    checkpoint_serial = 0
    for event in events:
        if event["event_type"] != "checkpoint":
            continue
        checkpoint_serial += 1
        end_cursor = f"cursor-{checkpoint_serial}"
        event["journal_start_cursor"] = previous_cursor
        event["journal_end_cursor"] = end_cursor
        journal_path = Path(event["journal"]["artifact"]["path"])
        journal_path.write_text(
            json.dumps(
                {
                    "_SYSTEMD_UNIT": "user@1000.service",
                    "_SYSTEMD_USER_UNIT": "nhms-node27-timeseries-compression-replay.service",
                    "_SYSTEMD_INVOCATION_ID": INVOCATION_ID,
                    "MESSAGE": f"replay supervisor checkpoint {checkpoint_serial}",
                },
                sort_keys=True,
            )
            + f"\n-- cursor: {end_cursor}\n"
        )
        event["journal"] = _observed(_file_ref(journal_path))
        previous_cursor = end_cursor
    ledger_path = tmp_path / "supervisor-ledger.jsonl"
    ledger_path.write_bytes(b"".join(_canonical(event) for event in events))
    bundle["execution"] = {
        "run_plan": _json_ref(tmp_path, "run-plan.json", plan),
        "ledger": _file_ref(ledger_path),
    }
    return bundle


def test_verifier_and_supervisor_agree_on_the_reviewed_authorization_pin() -> None:
    """The verifier re-declares the pin rather than importing the supervisor's, so that it
    stays an independent oracle -- which means a drift between the two would silently make
    real supervisor evidence unverifiable."""
    assert evidence.EXPECTED_REVIEWED_REMOTE_REF == supervisor.EXPECTED_REVIEWED_REMOTE_REF
    assert evidence.EXPECTED_REMOTE_IDENTITY == supervisor.EXPECTED_REMOTE_IDENTITY
    assert evidence.EXPECTED_REPO_PATH == supervisor.EXPECTED_REPO


def _lineage_repo(tmp_path: Path, *, remote_url: str | None) -> tuple[Path, str]:
    """Build a checkout with one reviewed commit and, optionally, an origin remote."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "--quiet")
    (root / "reviewed.txt").write_text("reviewed mutation lineage\n", encoding="utf-8")
    _git(root, "add", "reviewed.txt")
    _git(root, "commit", "--quiet", "--message", "reviewed mutation")
    if remote_url is not None:
        _git(root, "remote", "add", "origin", remote_url)
    return root, _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "remote_url",
    [
        "git@github.com:DankerMu/SHUD-NWM.git",
        "git@github.com:DankerMu/SHUD-NWM",
        "https://github.com/DankerMu/SHUD-NWM.git",
        "https://github.com/DankerMu/SHUD-NWM",
    ],
)
def test_repository_provenance_accepts_the_reviewed_pushed_origin_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_url: str
) -> None:
    root, head = _lineage_repo(tmp_path, remote_url=remote_url)
    _git(root, "update-ref", evidence.EXPECTED_REVIEWED_REMOTE_REF, head)
    monkeypatch.setattr(evidence, "PROVENANCE_REPO_ROOT", root)

    evidence._validate_repository_provenance(
        mutation_head_sha=head, reviewed_remote_ref=evidence.EXPECTED_REVIEWED_REMOTE_REF
    )


def test_repo_root_seams_default_to_the_checkout() -> None:
    # A live verifier must audit its own ambient checkout; the seams exist only so
    # tests can repoint them. If a default ever drifts off REPO_ROOT, a real run
    # would audit the wrong repository.
    assert _DEFAULT_PROVENANCE_REPO_ROOT == evidence.REPO_ROOT
    assert _DEFAULT_VERIFIER_REPO_ROOT == evidence.REPO_ROOT


def test_git_blob_bytes_reads_the_reviewed_blob_from_the_mutation_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, head = _lineage_repo(tmp_path, remote_url=None)
    monkeypatch.setattr(evidence, "REPO_ROOT", root)
    assert _REAL_GIT_BLOB_BYTES(head, "reviewed.txt", "reviewed file") == b"reviewed mutation lineage\n"


def test_git_blob_bytes_rejects_a_path_absent_at_the_mutation_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, head = _lineage_repo(tmp_path, remote_url=None)
    monkeypatch.setattr(evidence, "REPO_ROOT", root)
    with pytest.raises(evidence.EvidenceError, match="cannot be bound to mutation SHA"):
        _REAL_GIT_BLOB_BYTES(head, "never-committed.txt", "missing blob")


def test_current_verifier_head_binds_a_clean_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, head = _lineage_repo(tmp_path, remote_url=None)
    monkeypatch.setattr(evidence, "VERIFIER_REPO_ROOT", root)
    assert evidence._current_verifier_head() == head


def test_current_verifier_head_refuses_a_dirty_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _head = _lineage_repo(tmp_path, remote_url=None)
    (root / "reviewed.txt").write_text("tampered after commit\n", encoding="utf-8")
    monkeypatch.setattr(evidence, "VERIFIER_REPO_ROOT", root)
    with pytest.raises(evidence.EvidenceError, match="differs from verifier_head_sha"):
        evidence._current_verifier_head()


_NON_ORIGIN_REFS = (
    "refs/heads/reviewed",
    "refs/remotes/upstream/reviewed",
    "refs/tags/reviewed",
    "refs/remotes/originx/reviewed",
)


@pytest.mark.parametrize("reviewed_remote_ref", [*_NON_ORIGIN_REFS, "HEAD", ""])
def test_repository_provenance_requires_an_origin_remote_tracking_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reviewed_remote_ref: str
) -> None:
    """A ref outside `refs/remotes/origin/` is refused even when it resolves to exactly the
    mutation SHA, so unpushed local lineage can never stand in for reviewed lineage."""
    root, head = _lineage_repo(
        tmp_path, remote_url=f"git@github.com:{evidence.EXPECTED_REMOTE_IDENTITY}.git"
    )
    for ref in _NON_ORIGIN_REFS:
        _git(root, "update-ref", ref, head)
    monkeypatch.setattr(evidence, "PROVENANCE_REPO_ROOT", root)

    with pytest.raises(evidence.EvidenceError, match="not an origin remote-tracking ref"):
        evidence._validate_repository_provenance(
            mutation_head_sha=head, reviewed_remote_ref=reviewed_remote_ref
        )


def test_repository_provenance_refuses_a_reviewed_ref_git_cannot_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewed branch may never have been pushed, or may have been deleted."""
    root, head = _lineage_repo(tmp_path, remote_url=f"git@github.com:{evidence.EXPECTED_REMOTE_IDENTITY}.git")
    monkeypatch.setattr(evidence, "PROVENANCE_REPO_ROOT", root)

    with pytest.raises(evidence.EvidenceError, match="not the authorization-pinned origin lineage"):
        evidence._validate_repository_provenance(
            mutation_head_sha=head, reviewed_remote_ref=evidence.EXPECTED_REVIEWED_REMOTE_REF
        )


def test_repository_provenance_refuses_a_mutation_sha_the_reviewed_ref_never_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence built on a commit that was never pushed to the reviewed ref cannot qualify."""
    root, reviewed_head = _lineage_repo(tmp_path, remote_url=f"git@github.com:{evidence.EXPECTED_REMOTE_IDENTITY}.git")
    _git(root, "update-ref", evidence.EXPECTED_REVIEWED_REMOTE_REF, reviewed_head)
    (root / "unreviewed.txt").write_text("local-only mutation\n", encoding="utf-8")
    _git(root, "add", "unreviewed.txt")
    _git(root, "commit", "--quiet", "--message", "unreviewed local mutation")
    unreviewed_head = _git(root, "rev-parse", "HEAD")
    assert unreviewed_head != reviewed_head
    monkeypatch.setattr(evidence, "PROVENANCE_REPO_ROOT", root)

    with pytest.raises(evidence.EvidenceError, match="not the authorization-pinned origin lineage"):
        evidence._validate_repository_provenance(
            mutation_head_sha=unreviewed_head, reviewed_remote_ref=evidence.EXPECTED_REVIEWED_REMOTE_REF
        )


@pytest.mark.parametrize(
    "remote_url",
    [
        "git@github.com:attacker/SHUD-NWM.git",
        "https://github.com/attacker/SHUD-NWM.git",
        "git@github.com:DankerMu/OTHER-REPO.git",
        "git@example.com:DankerMu/SHUD-NWM.git",
        "/tmp/local-mirror",
        None,
    ],
)
def test_repository_provenance_refuses_a_foreign_or_absent_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_url: str | None
) -> None:
    """Reviewed lineage only counts when it is pushed to the expected GitHub remote."""
    root, head = _lineage_repo(tmp_path, remote_url=remote_url)
    _git(root, "update-ref", evidence.EXPECTED_REVIEWED_REMOTE_REF, head)
    monkeypatch.setattr(evidence, "PROVENANCE_REPO_ROOT", root)

    with pytest.raises(evidence.EvidenceError, match="not the authorization-pinned origin lineage"):
        evidence._validate_repository_provenance(
            mutation_head_sha=head, reviewed_remote_ref=evidence.EXPECTED_REVIEWED_REMOTE_REF
        )


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/{evidence.EXPECTED_REMOTE_IDENTITY}.git",
        f"http://github.com/{evidence.EXPECTED_REMOTE_IDENTITY}.git",
        f"git@github.com:{evidence.EXPECTED_REMOTE_IDENTITY}.git",
        f"ssh://git@github.com/{evidence.EXPECTED_REMOTE_IDENTITY}.git",
    ],
)
def test_remote_identity_accepts_only_github_host_anchored_forms(url: str) -> None:
    """Every accepted remote form must anchor github.com as the actual host."""
    assert evidence._remote_identity(url) == evidence.EXPECTED_REMOTE_IDENTITY


@pytest.mark.parametrize(
    "url",
    [
        # github.com as a path segment behind a hostile authority.
        f"https://evil.com/github.com/{evidence.EXPECTED_REMOTE_IDENTITY}.git",
        # github.com as a look-alike host prefix.
        f"https://github.com.evil.com/{evidence.EXPECTED_REMOTE_IDENTITY}.git",
        # A different host that merely ends in the same registrable string.
        f"https://notgithub.com/{evidence.EXPECTED_REMOTE_IDENTITY}.git",
    ],
)
def test_remote_identity_rejects_hosts_that_are_not_github(url: str) -> None:
    """A substring match would let a foreign origin masquerade as the reviewed remote."""
    assert evidence._remote_identity(url) != evidence.EXPECTED_REMOTE_IDENTITY
    assert evidence._remote_identity(url) == ""


def test_verify_bundle_refuses_evidence_whose_mutation_sha_left_the_reviewed_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lineage gate is wired into the bundle path, not merely importable."""
    root, _ = _lineage_repo(tmp_path, remote_url=f"git@github.com:{evidence.EXPECTED_REMOTE_IDENTITY}.git")
    monkeypatch.setattr(evidence, "PROVENANCE_REPO_ROOT", root)

    with pytest.raises(evidence.EvidenceError, match="not the authorization-pinned origin lineage"):
        evidence.verify_bundle(_bundle(tmp_path), receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_verifier_recomputes_complete_terminal_envelope(tmp_path: Path) -> None:
    terminal = evidence.verify_bundle(_bundle(tmp_path), receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    jsonschema.validate(terminal, EVIDENCE_SCHEMA)
    assert terminal["verdict"] == "PASS_TASK_4_5"
    assert terminal["execution"]["namespace_counts"]["replay_supervisor_activation"] == 1
    assert terminal["execution"]["namespace_counts"]["compression_service_activation"] == 0
    assert terminal["recovery"]["authorized"] is True
    assert terminal["recovery"]["row_parity"] is True
    assert terminal["recovery"]["target"] == IDENTITY
    assert terminal["out_of_scope"]["decompress_run"] is True
    assert terminal["selection"]["bound"] == 1
    assert terminal["sizes"]["compressed_chunk_count_delta"] == 1
    assert terminal["sizes"]["post_combined_hypertable_size"] < terminal["sizes"]["pre_combined_hypertable_size"]
    assert [query["name"] for query in terminal["benchmarks"]["queries"]] == ["curve", "mvt"]
    curve = terminal["benchmarks"]["queries"][0]
    assert curve["after_capture"]["samples_ms"] == [
        measurement["execution_ms"] for measurement in curve["after_capture"]["measurements"]
    ]


# --- SC-F1: trust-boundary regression lock ------------------------------------------
# The trust boundary the user personally decided (round-3 audit-contract-decision) is
# enforced redundantly by schema `const` (schema:60-62,78-81) and verifier exact
# equality (live_evidence.py:1009-1016). These tests fail if either the schema pins are
# relaxed to permit an overclaim, or the verifier stops binding the run-plan attestation.

_TRUST_BOUNDARY_TERMINAL_OVERCLAIMS = {
    # authorization side (schema:60-62)
    "authorization.database_audit_proof=true": (
        lambda terminal: terminal["authorization"].__setitem__("database_audit_proof", True)
    ),
    "authorization.acceptance_claim-strengthened": (
        lambda terminal: terminal["authorization"].__setitem__(
            "acceptance_claim", "database-level proof no other session could mutate"
        )
    ),
    "authorization.sole_db_user_during_window=false": (
        lambda terminal: terminal["authorization"].__setitem__("sole_db_user_during_window", False)
    ),
    # execution side (schema:78-81)
    "execution.claim-strengthened": (
        lambda terminal: terminal["execution"].__setitem__(
            "claim", "database-level proof no other session could mutate"
        )
    ),
    "execution.database_audit_proof=true": (
        lambda terminal: terminal["execution"].__setitem__("database_audit_proof", True)
    ),
    "execution.sole_db_user_attested=false": (
        lambda terminal: terminal["execution"].__setitem__("sole_db_user_attested", False)
    ),
    "execution.trust_limit-weakened": (
        lambda terminal: terminal["execution"].__setitem__(
            "trust_limit", "absolute direct-SQL bypass proof obtained"
        )
    ),
}


@pytest.mark.parametrize("overclaim", sorted(_TRUST_BOUNDARY_TERMINAL_OVERCLAIMS))
def test_trust_boundary_schema_rejects_single_field_overclaim(tmp_path: Path, overclaim: str) -> None:
    """A qualifying v3 terminal must fail schema if any single trust-boundary field is promoted."""
    terminal = evidence.verify_bundle(_bundle(tmp_path), receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    # Non-vacuity: the unmutated terminal validates, so the failure below is caused by the mutation.
    jsonschema.validate(terminal, EVIDENCE_SCHEMA)
    mutated = copy.deepcopy(terminal)
    _TRUST_BOUNDARY_TERMINAL_OVERCLAIMS[overclaim](mutated)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mutated, EVIDENCE_SCHEMA)


def test_trust_boundary_terminal_carries_bounded_claim_not_overclaim(tmp_path: Path) -> None:
    """The produced terminal claims exactly the user-decided boundary and nothing stronger."""
    terminal = evidence.verify_bundle(_bundle(tmp_path), receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    execution = terminal["execution"]
    assert execution["claim"] == evidence.PASS_CLAIM
    assert execution["database_audit_proof"] is False
    assert execution["sole_db_user_attested"] is True
    assert execution["trust_limit"] == "discrete observations; no absolute direct-SQL bypass proof"
    authorization = terminal["authorization"]
    assert authorization["acceptance_claim"] == evidence.PASS_CLAIM
    assert authorization["database_audit_proof"] is False
    assert authorization["sole_db_user_during_window"] is True


def _rewrite_run_plan(bundle: dict[str, Any], tmp_path: Path, mutate: Any) -> None:
    """Apply `mutate` to the run plan, rebind its id, and propagate to the ledger events."""
    plan = _read_ref(bundle["execution"]["run_plan"])
    mutate(plan)
    plan["run_plan_id"] = evidence._supervisor_run_plan_id(plan)
    bundle["execution"]["run_plan"] = _json_ref(tmp_path, "attestation-plan.json", plan)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    for event in events:
        event["run_plan_id"] = plan["run_plan_id"]
    ledger = tmp_path / "attestation-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(event) for event in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)


_OPERATOR_ATTESTATION_DRIFTS = {
    "sole_db_user_denied": lambda plan: plan["operator_attestation"].__setitem__("sole_db_user_during_window", False),
    "audit_proof_promoted": lambda plan: plan["operator_attestation"].__setitem__("database_audit_proof", True),
    "trust_limit_weakened": lambda plan: plan["operator_attestation"].__setitem__(
        "trust_limit", "absolute direct-SQL bypass proof obtained"
    ),
    "sole_db_user_absent": lambda plan: plan["operator_attestation"].pop("sole_db_user_during_window"),
    "audit_proof_absent": lambda plan: plan["operator_attestation"].pop("database_audit_proof"),
    "trust_limit_absent": lambda plan: plan["operator_attestation"].pop("trust_limit"),
}


@pytest.mark.parametrize("drift", sorted(_OPERATOR_ATTESTATION_DRIFTS))
def test_verifier_rejects_operator_attestation_drift(tmp_path: Path, drift: str) -> None:
    """A run plan whose attestation triple differs from the bound decision cannot verify."""
    bundle = _bundle(tmp_path)
    _rewrite_run_plan(bundle, tmp_path, _OPERATOR_ATTESTATION_DRIFTS[drift])
    with pytest.raises(evidence.EvidenceError, match="sole-user attestation"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_verifier_rejects_absent_operator_attestation_key(tmp_path: Path) -> None:
    """Deleting the whole attestation key is refused before any command is trusted."""
    bundle = _bundle(tmp_path)
    _rewrite_run_plan(bundle, tmp_path, lambda plan: plan.pop("operator_attestation"))
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_verifier_publish_lock_is_deadline_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"outcome":"newer"}\n')
    expected = evidence._output_identity(output)
    lock_fd = os.open(output.with_name(f".{output.name}.publish.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(evidence, "PUBLISH_LOCK_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    try:
        assert not evidence._publish_terminal_failure(
            output,
            stage="test",
            expected=expected,
            intent_context=_intent_context(),
        )
        assert evidence._terminal_intent_path(output).exists()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert time.monotonic() - started < 0.5
    assert output.read_bytes() == b'{"outcome":"newer"}\n'
    assert evidence._publish_terminal_failure(
        output,
        stage="test",
        expected=expected,
        intent_context=_intent_context(),
    )
    assert json.loads(output.read_text())["qualifies_task_4_5"] is False
    assert not evidence._terminal_intent_root_path(output).exists()
    newer = output.read_bytes()
    assert not evidence._publish_terminal_failure(
        output,
        stage="test",
        expected=expected,
        intent_context=_intent_context(),
    )
    assert output.read_bytes() == newer
    assert not evidence._terminal_intent_root_path(output).exists()


def _create_pending_failure_intent(
    output: Path,
    *,
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[evidence.FileIdentity | None, Path]:
    expected = evidence._output_identity(output)
    lock_path = evidence._terminal_lock_path(output)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(evidence, "PUBLISH_LOCK_TIMEOUT_SECONDS", 0.03)
    try:
        assert not evidence._publish_terminal_failure(
            output,
            stage=stage,
            expected=expected,
            intent_context=_intent_context(),
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return expected, evidence._terminal_intent_path(output)


def test_main_failure_intent_invalidates_old_pass_until_successful_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(_canonical(bundle))
    closure = resolve_artifact_closure(bundle)
    old_pass = evidence.verify_bundle(
        bundle,
        receipt_schema=RECEIPT_SCHEMA,
        verifier_head_sha=VERIFIER_HEAD,
        artifact_manifest=closure.manifest,
    )
    output = tmp_path / "terminal.json"
    output.write_bytes(_canonical(old_pass))
    old_identity = evidence._output_identity(output)
    lock_path = evidence._terminal_lock_path(output)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(evidence, "PUBLISH_LOCK_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(evidence, "_current_verifier_head", lambda: VERIFIER_HEAD)
    try:
        assert evidence.main(["--bundle-path", str(bundle_path), "--output-path", str(output)]) == 1
        assert evidence._terminal_intent_path(output).exists()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    with pytest.raises(evidence.EvidenceError, match="intent is pending"):
        evidence.read_authoritative_terminal(output)
    assert evidence._output_identity(output) == old_identity
    assert evidence.main(["--bundle-path", str(bundle_path), "--output-path", str(output)]) == 0
    authoritative = evidence.read_authoritative_terminal(output)
    assert authoritative["qualifies_task_4_5"] is True
    assert authoritative["verdict"] == evidence.PASS_VERDICT
    assert not evidence._terminal_intent_path(output).exists()


def test_newer_valid_pass_reconciles_exact_intent_but_tampered_pass_cannot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    bundle = _bundle(fixture_dir)
    closure = resolve_artifact_closure(bundle)
    valid_pass = evidence.verify_bundle(
        bundle,
        receipt_schema=RECEIPT_SCHEMA,
        verifier_head_sha=VERIFIER_HEAD,
        artifact_manifest=closure.manifest,
    )
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"old":"terminal"}\n')
    _, intent_path = _create_pending_failure_intent(output, stage="original", monkeypatch=monkeypatch)
    output.write_bytes(_canonical(valid_pass))
    newer_identity = evidence._output_identity(output)
    tampered_pass = {**valid_pass, "foreign": True}
    with pytest.raises(evidence.EvidenceError, match="schema-valid"):
        evidence._publish_terminal_cas(output, _canonical(tampered_pass), newer_identity)
    assert intent_path.exists()
    published = evidence._publish_terminal_cas(output, _canonical(valid_pass), newer_identity)
    assert published == evidence._output_identity(output)
    assert not intent_path.exists()
    assert evidence.read_authoritative_terminal(output)["verdict"] == evidence.PASS_VERDICT


def test_later_verifier_success_replaces_shared_supervisor_tombstone(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    bundle = _bundle(fixture_dir)
    closure = resolve_artifact_closure(bundle)
    valid_pass = evidence.verify_bundle(
        bundle,
        receipt_schema=RECEIPT_SCHEMA,
        verifier_head_sha=VERIFIER_HEAD,
        artifact_manifest=closure.manifest,
    )
    output = tmp_path / "terminal.json"
    stale = b'{"schema_version":"3.0","qualifies_task_4_5":true}\n'
    output.write_bytes(stale)
    assert supervisor.finalize_receipt(
        output,
        expected_stale_sha256=hashlib.sha256(stale).hexdigest(),
        run_id=valid_pass["execution"]["run_id"],
        stage="systemd-stop-post",
        possible_mutation=True,
        mutation_head_sha=valid_pass["mutation_head_sha"],
    )
    tombstone_identity = evidence._output_identity(output)
    assert tombstone_identity is not None
    evidence._publish_terminal_cas(output, _canonical(valid_pass), tombstone_identity)
    terminal = evidence.read_authoritative_terminal(output)
    assert terminal["qualifies_task_4_5"] is True
    assert terminal["verdict"] == evidence.PASS_VERDICT


def test_success_publication_fsync_uncertainty_keeps_intent_until_idempotent_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    bundle = _bundle(fixture_dir)
    valid_pass = evidence.verify_bundle(
        bundle,
        receipt_schema=RECEIPT_SCHEMA,
        verifier_head_sha=VERIFIER_HEAD,
        artifact_manifest=resolve_artifact_closure(bundle).manifest,
    )
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"stale":true}\n')
    expected = evidence._output_identity(output)
    assert expected is not None
    real_fsync_directory = terminal_state._fsync_directory_fd
    failed = False

    def fail_after_replace(fd: int, path: Path, *, label: str) -> None:
        nonlocal failed
        if label == "terminal parent" and not failed:
            failed = True
            raise evidence.EvidenceError("injected success publication fsync uncertainty")
        real_fsync_directory(fd, path, label=label)

    monkeypatch.setattr(terminal_state, "_fsync_directory_fd", fail_after_replace)
    with pytest.raises(evidence.EvidenceError, match="fsync uncertainty"):
        evidence._publish_terminal_cas(output, _canonical(valid_pass), expected)
    assert evidence._terminal_intent_root_path(output).exists()
    monkeypatch.setattr(terminal_state, "_fsync_directory_fd", real_fsync_directory)
    assert evidence.read_authoritative_terminal(output)["verdict"] == evidence.PASS_VERDICT
    assert not evidence._terminal_intent_root_path(output).exists()


def _apply_committed_cleanup_crash_prefix(
    output: Path, pending: Mapping[str, Any], prefix: str
) -> None:
    directory = output.parent / str(pending["directory_name"])
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if prefix == "a":
            return
        if prefix == "c":
            os.unlink("identity.json", dir_fd=directory_fd)
            os.fsync(directory_fd)
            return
        os.unlink("intent.json", dir_fd=directory_fd)
        os.fsync(directory_fd)
        if prefix == "b":
            return
        os.unlink("identity.json", dir_fd=directory_fd)
        os.fsync(directory_fd)
        if prefix == "d":
            return
    finally:
        os.close(directory_fd)
    os.rmdir(directory)
    parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("lane", ["failure", "pass", "finalizer"])
@pytest.mark.parametrize("prefix", ["a", "b", "c", "d", "e"])
def test_committed_cleanup_fresh_invocation_recovers_every_crash_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    prefix: str,
) -> None:
    output = tmp_path / "terminal.json"
    stale = b'{"stale":true}\n'
    output.write_bytes(stale)
    expected = evidence._output_identity(output)
    assert expected is not None
    valid_pass: Mapping[str, Any] | None = None
    if lane == "pass":
        fixture_dir = tmp_path / "fixture"
        fixture_dir.mkdir()
        bundle = _bundle(fixture_dir)
        valid_pass = evidence.verify_bundle(
            bundle,
            receipt_schema=RECEIPT_SCHEMA,
            verifier_head_sha=VERIFIER_HEAD,
            artifact_manifest=resolve_artifact_closure(bundle).manifest,
        )
    real_recover = terminal_state._recover_committed_cleanup_locked
    injected = False

    def crash_after_prefix(*args: Any, **kwargs: Any) -> None:
        nonlocal injected
        pending = kwargs["pending"]
        assert pending["state"]["state"] == "committed_cleanup"
        _apply_committed_cleanup_crash_prefix(output, pending, prefix)
        injected = True
        raise evidence.EvidenceError(f"injected committed cleanup crash {prefix}")

    monkeypatch.setattr(terminal_state, "_recover_committed_cleanup_locked", crash_after_prefix)
    if lane == "failure":
        assert not terminal_state.publish_unavailable_failure(
            output,
            stage="crash-prefix",
            expected=expected,
            verifier_head_sha=VERIFIER_HEAD,
        )
    elif lane == "pass":
        assert valid_pass is not None
        with pytest.raises(evidence.EvidenceError, match="injected committed cleanup"):
            evidence._publish_terminal_cas(output, _canonical(valid_pass), expected)
    else:
        assert not supervisor.finalize_receipt(
            output,
            expected_stale_sha256=expected.sha256,
            run_id="cleanup-finalizer-run",
            stage="systemd-stop-post",
            possible_mutation=True,
            mutation_head_sha=HEAD,
        )
    assert injected
    monkeypatch.setattr(terminal_state, "_recover_committed_cleanup_locked", real_recover)
    if prefix == "c":
        with pytest.raises(evidence.EvidenceError, match="identity-first prefix is unreachable"):
            evidence.read_authoritative_terminal(output)
        consumed = list(tmp_path.glob(f"{evidence._terminal_intent_root_path(output).name}.consumed-*"))
        assert len(consumed) == 1
        assert {entry.name for entry in consumed[0].iterdir()} == {"intent.json"}
        return
    terminal = evidence.read_authoritative_terminal(output)
    if lane == "failure":
        assert terminal["provenance_state"] == "unavailable"
    elif lane == "pass":
        assert terminal["verdict"] == evidence.PASS_VERDICT
    else:
        assert terminal["provenance_state"] == "bound"
        assert terminal["run_id"] == "cleanup-finalizer-run"
    assert not evidence._terminal_intent_root_path(output).exists()
    assert not list(tmp_path.glob(f"{evidence._terminal_intent_root_path(output).name}.consumed-*"))
    with terminal_state._locked_intent_gate(output, label="committed cleanup final audit") as (_, parent_fd):
        assert terminal_state._read_gate_state(parent_fd, output)["state"] == "idle"


def test_committed_cleanup_tampered_single_survivor_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"stale":true}\n')
    expected = evidence._output_identity(output)
    assert expected is not None
    real_recover = terminal_state._recover_committed_cleanup_locked
    survivor: list[Path] = []

    def crash_with_tampered_survivor(*args: Any, **kwargs: Any) -> None:
        pending = kwargs["pending"]
        _apply_committed_cleanup_crash_prefix(output, pending, "b")
        path = output.parent / str(pending["directory_name"]) / "identity.json"
        raw = path.read_bytes()
        changed = raw.replace(VERIFIER_HEAD.encode(), ("f" * 40).encode(), 1)
        assert len(changed) == len(raw) and changed != raw
        path.write_bytes(changed)
        survivor.append(path)
        raise evidence.EvidenceError("injected tampered survivor crash")

    monkeypatch.setattr(
        terminal_state, "_recover_committed_cleanup_locked", crash_with_tampered_survivor
    )
    assert not terminal_state.publish_unavailable_failure(
        output,
        stage="tampered-survivor",
        expected=expected,
        verifier_head_sha=VERIFIER_HEAD,
    )
    monkeypatch.setattr(terminal_state, "_recover_committed_cleanup_locked", real_recover)
    with pytest.raises(evidence.EvidenceError, match="survivor identity changed"):
        evidence.read_authoritative_terminal(output)
    assert survivor and survivor[0].exists()


@pytest.mark.parametrize("compatible", [True, False])
def test_committed_cleanup_terminal_change_applies_explicit_newer_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compatible: bool
) -> None:
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"stale":true}\n')
    expected = evidence._output_identity(output)
    assert expected is not None
    real_recover = terminal_state._recover_committed_cleanup_locked
    committed_directory: list[Path] = []

    def crash_before_cleanup(*args: Any, **kwargs: Any) -> None:
        pending = kwargs["pending"]
        committed_directory.append(output.parent / str(pending["directory_name"]))
        raise evidence.EvidenceError("injected committed terminal replacement window")

    monkeypatch.setattr(terminal_state, "_recover_committed_cleanup_locked", crash_before_cleanup)
    assert not terminal_state.publish_unavailable_failure(
        output,
        stage="terminal-change",
        expected=expected,
        verifier_head_sha=VERIFIER_HEAD,
    )
    committed_identity = evidence._output_identity(output)
    assert committed_identity is not None
    if compatible:
        newer, _ = terminal_state.bound_failure_payload(
            stage="newer-bound",
            expected_output=committed_identity,
            run_id="newer-bound-run",
            mutation_head_sha=HEAD,
            possible_mutation=True,
        )
    else:
        newer, _ = terminal_state.unavailable_failure_payload(
            stage="foreign-unavailable",
            expected_output=committed_identity,
            verifier_head_sha="f" * 40,
        )
    output.write_bytes(_canonical(newer))
    monkeypatch.setattr(terminal_state, "_recover_committed_cleanup_locked", real_recover)
    if compatible:
        terminal = evidence.read_authoritative_terminal(output)
        assert terminal["provenance_state"] == "bound"
        assert not committed_directory[0].exists()
    else:
        with pytest.raises(evidence.EvidenceError, match="safe newer-wins provenance"):
            evidence.read_authoritative_terminal(output)
        assert committed_directory[0].exists()
        assert {entry.name for entry in committed_directory[0].iterdir()} == {
            "intent.json",
            "identity.json",
        }


def test_authoritative_terminal_reader_rejects_malformed_terminal(tmp_path: Path) -> None:
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"qualifies_task_4_5":true}\n')
    with pytest.raises(evidence.EvidenceError, match="schema-valid"):
        evidence.read_authoritative_terminal(output)


@pytest.mark.parametrize("tamper", ["content", "context", "same-byte-replacement"])
def test_pending_failure_identity_sidecar_replacement_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"old":"terminal"}\n')
    _create_pending_failure_intent(output, stage="original", monkeypatch=monkeypatch)
    identity_path = evidence._terminal_intent_identity_path(output)
    raw = identity_path.read_bytes()
    replacement = identity_path.with_name("replacement.json")
    if tamper == "content":
        document = json.loads(raw)
        document["failure_payload_sha256"] = "0" * 64
        replacement.write_bytes(_canonical(document))
    elif tamper == "context":
        document = json.loads(raw)
        document["context"]["mutation_head_sha"] = "f" * 40
        replacement.write_bytes(_canonical(document))
    else:
        replacement.write_bytes(raw)
    replacement.chmod(0o600)
    os.replace(replacement, identity_path)
    with pytest.raises(evidence.EvidenceError, match="identity|durable"):
        evidence.read_authoritative_terminal(output)
    assert evidence._terminal_intent_root_path(output).exists()


@pytest.mark.parametrize("target", ["intent", "identity"])
def test_consume_revalidates_equal_length_in_place_content_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    bundle = _bundle(fixture_dir)
    closure = resolve_artifact_closure(bundle)
    valid_pass = evidence.verify_bundle(
        bundle,
        receipt_schema=RECEIPT_SCHEMA,
        verifier_head_sha=VERIFIER_HEAD,
        artifact_manifest=closure.manifest,
    )
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"old":"terminal"}\n')
    _create_pending_failure_intent(output, stage="original", monkeypatch=monkeypatch)
    output.write_bytes(_canonical(valid_pass))
    newer_identity = evidence._output_identity(output)
    real_consume = terminal_state._consume_pending_intent_locked
    original_bytes: list[bytes] = []

    def mutate_then_consume(*args: Any, **kwargs: Any) -> None:
        target_path = (
            evidence._terminal_intent_path(output)
            if target == "intent"
            else evidence._terminal_intent_identity_path(output)
        )
        raw = target_path.read_bytes()
        original_bytes.append(raw)
        if target == "intent":
            changed = raw.replace(b"original", b"tampered")
        else:
            replacement_sha = ("f" * 40 if VERIFIER_HEAD != "f" * 40 else "e" * 40).encode()
            changed = raw.replace(VERIFIER_HEAD.encode(), replacement_sha, 1)
        assert len(changed) == len(raw) and changed != raw
        target_path.write_bytes(changed)
        real_consume(*args, **kwargs)

    monkeypatch.setattr(terminal_state, "_consume_pending_intent_locked", mutate_then_consume)
    with pytest.raises(evidence.EvidenceError, match="identity|durable|changed|stage differs"):
        evidence._publish_terminal_cas(output, _canonical(valid_pass), newer_identity)
    with pytest.raises(evidence.EvidenceError):
        evidence.read_authoritative_terminal(output)
    consuming = list(tmp_path.glob(f"{evidence._terminal_intent_root_path(output).name}.consumed-*"))
    assert len(consuming) == 1
    assert {item.name for item in consuming[0].iterdir()} == {"intent.json", "identity.json"}
    restored_path = consuming[0] / ("intent.json" if target == "intent" else "identity.json")
    restored_path.write_bytes(original_bytes[0])
    monkeypatch.setattr(terminal_state, "_consume_pending_intent_locked", real_consume)
    current_identity = evidence._output_identity(output)
    evidence._publish_terminal_cas(output, _canonical(valid_pass), current_identity)
    assert evidence.read_authoritative_terminal(output)["verdict"] == evidence.PASS_VERDICT


def test_failure_intent_parent_fsync_failure_leaves_no_authoritative_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "terminal.json"
    old = b'{"old":"terminal"}\n'
    output.write_bytes(old)
    with evidence._locked_intent_gate(output, label="test gate bootstrap"):
        pass
    parent_info = os.stat(tmp_path)
    real_fsync = evidence.os.fsync
    failed = False

    def fail_parent_once(fd: int) -> None:
        nonlocal failed
        info = os.fstat(fd)
        if not failed and stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (
            parent_info.st_dev,
            parent_info.st_ino,
        ):
            failed = True
            raise OSError("injected parent fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(evidence.os, "fsync", fail_parent_once)
    assert not evidence._publish_terminal_failure(
        output,
        stage="fsync-failure",
        expected=evidence._output_identity(output),
        intent_context=_intent_context(),
    )
    assert failed
    assert output.read_bytes() == old
    assert not evidence._terminal_intent_root_path(output).exists()
    with evidence._locked_intent_gate(output, label="test gate audit") as (_, parent_fd):
        assert evidence._read_gate_state(parent_fd, output)["state"] == "idle"


def test_reader_cannot_pass_concurrent_failure_invalidation_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"old":"terminal"}\n')
    with evidence._locked_intent_gate(output, label="test gate bootstrap"):
        pass
    terminal_lock_fd = os.open(evidence._terminal_lock_path(output), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(terminal_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(evidence, "PUBLISH_LOCK_TIMEOUT_SECONDS", 0.1)
    started = threading.Event()
    release_creation = threading.Event()
    real_create = terminal_state._create_pending_intent_locked

    def barrier_create(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.set()
        assert release_creation.wait(2)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(terminal_state, "_create_pending_intent_locked", barrier_create)
    publisher_result: list[bool] = []
    reader_errors: list[str] = []

    def publish_failure() -> None:
        publisher_result.append(
            evidence._publish_terminal_failure(
                output,
                stage="concurrent",
                expected=evidence._output_identity(output),
                intent_context=_intent_context(),
            )
        )

    def read_terminal() -> None:
        try:
            evidence.read_authoritative_terminal(output)
        except evidence.EvidenceError as error:
            reader_errors.append(str(error))

    publisher = threading.Thread(target=publish_failure)
    publisher.start()
    assert started.wait(2)
    reader = threading.Thread(target=read_terminal)
    reader.start()
    time.sleep(0.03)
    assert reader.is_alive()
    release_creation.set()
    publisher.join(2)
    reader.join(2)
    fcntl.flock(terminal_lock_fd, fcntl.LOCK_UN)
    os.close(terminal_lock_fd)
    assert publisher_result == [False]
    assert reader_errors and ("intent is pending" in reader_errors[0] or "gate failed safely" in reader_errors[0])
    assert evidence._terminal_intent_path(output).exists()


def test_cross_process_same_byte_intent_inode_swap_is_rejected(tmp_path: Path) -> None:
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"old":"terminal"}\n')
    terminal_lock_fd = os.open(evidence._terminal_lock_path(output), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(terminal_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    creator = """
import json
import sys
from pathlib import Path
from scripts import node27_timeseries_compression_live_evidence as evidence
evidence.PUBLISH_LOCK_TIMEOUT_SECONDS = 0.05
path = Path(sys.argv[1])
context = json.loads(sys.argv[2])
published = evidence._publish_terminal_failure(
    path,
    stage="cross-process",
    expected=evidence._output_identity(path),
    intent_context=context,
)
raise SystemExit(1 if published or not evidence._terminal_intent_path(path).exists() else 0)
"""
    created = subprocess.run(
        [sys.executable, "-c", creator, str(output), json.dumps(_intent_context())],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    fcntl.flock(terminal_lock_fd, fcntl.LOCK_UN)
    os.close(terminal_lock_fd)
    assert created.returncode == 0, created.stderr
    intent_path = evidence._terminal_intent_path(output)
    raw = intent_path.read_bytes()
    replacement = intent_path.with_name("replacement.json")
    replacement.write_bytes(raw)
    replacement.chmod(0o600)
    os.replace(replacement, intent_path)
    reader = """
import sys
from pathlib import Path
from scripts import node27_timeseries_compression_live_evidence as evidence
evidence.PUBLISH_LOCK_TIMEOUT_SECONDS = 0.2
try:
    evidence.read_authoritative_terminal(Path(sys.argv[1]))
except evidence.EvidenceError as error:
    print(str(error))
    raise SystemExit(0)
raise SystemExit(1)
"""
    checked = subprocess.run(
        [sys.executable, "-c", reader, str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert "identity" in checked.stdout


@pytest.mark.parametrize("tamper", ["pass", "different", "secret", "same-byte-replacement"])
def test_pending_failure_intent_rejects_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"old":"terminal"}\n')
    expected, intent_path = _create_pending_failure_intent(output, stage="original", monkeypatch=monkeypatch)
    raw = intent_path.read_bytes()
    document = json.loads(raw)
    if tamper == "pass":
        document["payload"]["qualifies_task_4_5"] = True
        intent_path.write_bytes(_canonical(document))
    elif tamper == "different":
        document["payload"]["failure"]["stage"] = "different"
        intent_path.write_bytes(_canonical(document))
    elif tamper == "secret":
        document["payload"]["failure"]["stage"] = "token=not-a-real-token"
        intent_path.write_bytes(_canonical(document))
    else:
        intent_path.unlink()
        intent_path.write_bytes(raw)
    assert not evidence._publish_terminal_failure(
        output,
        stage="original",
        expected=expected,
        intent_context=_intent_context(),
    )
    assert output.read_bytes() == b'{"old":"terminal"}\n'
    assert intent_path.exists()


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_pending_failure_intent_rejects_link_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    output = tmp_path / "terminal.json"
    output.write_bytes(b'{"old":"terminal"}\n')
    expected, intent_path = _create_pending_failure_intent(output, stage="original", monkeypatch=monkeypatch)
    raw = intent_path.read_bytes()
    intent_path.unlink()
    target = tmp_path / "foreign-intent.json"
    target.write_bytes(raw)
    if alias == "symlink":
        intent_path.symlink_to(target)
    else:
        os.link(target, intent_path)
    assert not evidence._publish_terminal_failure(
        output,
        stage="original",
        expected=expected,
        intent_context=_intent_context(),
    )
    assert output.read_bytes() == b'{"old":"terminal"}\n'
    assert intent_path.exists()


@pytest.mark.parametrize(
    "derived_kind", ["intent-root", "intent", "identity", "gate", "gate-state", "lock"]
)
@pytest.mark.parametrize("alias", ["path", "symlink", "hardlink"])
def test_terminal_derived_paths_cannot_alias_complete_input_closure(
    tmp_path: Path,
    derived_kind: str,
    alias: str,
) -> None:
    output = tmp_path / "terminal.json"
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(b"{}\n")
    derived = {
        "intent-root": evidence._terminal_intent_root_path(output),
        "intent": evidence._terminal_intent_path(output),
        "identity": evidence._terminal_intent_identity_path(output),
        "gate": evidence._terminal_intent_gate_path(output),
        "gate-state": evidence._terminal_intent_state_path(output),
        "lock": evidence._terminal_lock_path(output),
    }[derived_kind]
    if derived_kind in {"intent", "identity"}:
        derived.parent.mkdir()
    source = derived if alias == "path" else tmp_path / f"{derived_kind}-closure-input.json"
    source.write_bytes(b'{"input":true}\n')
    if alias == "symlink":
        derived.symlink_to(source)
    elif alias == "hardlink":
        os.link(source, derived)
    identity = evidence.inspect_bounded_file_no_follow(
        source,
        max_bytes=source.stat().st_size,
        label="crafted closure input",
    )
    closure = evidence.ArtifactClosure((identity,), (), identity.size)
    with pytest.raises(evidence.BoundedEvidenceError, match="symlink|aliases an input"):
        evidence._assert_terminal_state_paths_disjoint(
            output,
            bundle_path=bundle_path,
            closure=closure,
        )
    assert source.exists()
    assert derived.exists()


def test_terminal_lock_open_stays_on_anchored_parent_during_namespace_swap(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    moved_dir = tmp_path / "state-moved"
    state_dir.mkdir()
    output = state_dir / "terminal.json"
    output.write_bytes(b"{}\n")
    lock_name = evidence._terminal_lock_path(output).name
    with pytest.raises(evidence.EvidenceError, match="parent.*identity changed"):
        with evidence._locked_intent_gate(output, label="namespace-swap gate") as (_, parent_fd):
            state_dir.rename(moved_dir)
            state_dir.mkdir()
            lock_fd = evidence._open_terminal_lock(output, parent_fd=parent_fd)
            os.close(lock_fd)
            assert (moved_dir / lock_name).exists()
            assert not (state_dir / lock_name).exists()
    assert (moved_dir / lock_name).exists()
    assert not (state_dir / lock_name).exists()


def test_verifier_accepts_bounded_post_relation_measurement_drift(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    post = _sizes(post=True)
    post["tables"]["hydro.river_timeseries"]["compressed_relations"][0]["bytes"] += 8192
    bundle["sizes"]["post"] = _json_ref(tmp_path, "sizes-post-drift.json", post)
    _replace_produced_artifact(bundle, "compression_enforce", "sizes_post", bundle["sizes"]["post"], tmp_path)
    terminal = evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    assert terminal["verdict"] == "PASS_TASK_4_5"


def test_verifier_rejects_excessive_post_relation_measurement_drift(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    post = _sizes(post=True)
    post["tables"]["hydro.river_timeseries"]["compressed_relations"][0]["bytes"] += (
        evidence.MAX_POST_MEASUREMENT_DRIFT_BYTES + 1
    )
    bundle["sizes"]["post"] = _json_ref(tmp_path, "sizes-post-large-drift.json", post)
    with pytest.raises(evidence.EvidenceError, match="measurement-time drift"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_live_evidence_example_and_required_top_level_contract() -> None:
    example = json.loads(
        (ROOT / "schemas/examples/timeseries_compression_live_evidence.example.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(example, EVIDENCE_SCHEMA)
    for key in EVIDENCE_SCHEMA["required"]:
        candidate = dict(example)
        del candidate[key]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(candidate, EVIDENCE_SCHEMA)


_V3_ONLY_REQUIRED_REMOVERS = {
    "execution": lambda doc: doc.pop("execution"),
    "chronology": lambda doc: doc.pop("chronology"),
    "source_manifest": lambda doc: doc.pop("source_manifest"),
    "qualifies_task_4_5": lambda doc: doc.pop("qualifies_task_4_5"),
    "authorization.sole_db_user_during_window": lambda doc: doc["authorization"].pop("sole_db_user_during_window"),
    "authorization.database_audit_proof": lambda doc: doc["authorization"].pop("database_audit_proof"),
    "authorization.acceptance_claim": lambda doc: doc["authorization"].pop("acceptance_claim"),
    "execution.claim": lambda doc: doc["execution"].pop("claim"),
    "execution.database_audit_proof": lambda doc: doc["execution"].pop("database_audit_proof"),
    "execution.sole_db_user_attested": lambda doc: doc["execution"].pop("sole_db_user_attested"),
    "execution.trust_limit": lambda doc: doc["execution"].pop("trust_limit"),
    "recovery.invocation": lambda doc: doc["recovery"].pop("invocation"),
    "preflight.schema_dump_list": lambda doc: doc["preflight"].pop("schema_dump_list"),
    "benchmarks.queries[0].request": lambda doc: doc["benchmarks"]["queries"][0].pop("request"),
}


def _live_evidence_example() -> dict[str, Any]:
    return json.loads(
        (ROOT / "schemas/examples/timeseries_compression_live_evidence.example.json").read_text(encoding="utf-8")
    )


def test_live_evidence_example_is_the_qualifying_v3_shape() -> None:
    """The committed example must exercise the v3 `allOf` branch CI validates, not the v2 shape."""
    example = _live_evidence_example()
    jsonschema.validate(example, EVIDENCE_SCHEMA)
    assert example["schema_version"] == "3.0"
    assert example["qualifies_task_4_5"] is True
    assert example["verdict"] == "PASS_TASK_4_5"
    assert example["execution"]["claim"] == evidence.PASS_CLAIM
    assert example["execution"]["database_audit_proof"] is False


@pytest.mark.parametrize("removed", sorted(_V3_ONLY_REQUIRED_REMOVERS))
def test_live_evidence_v3_example_requires_each_v3_only_key(removed: str) -> None:
    """Every v3-only required key is load-bearing: removing it must fail schema validation."""
    example = _live_evidence_example()
    jsonschema.validate(example, EVIDENCE_SCHEMA)  # non-vacuity: clean example is valid
    _V3_ONLY_REQUIRED_REMOVERS[removed](example)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(example, EVIDENCE_SCHEMA)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda bundle: bundle["authorization"].__setitem__("bound", 5),
        lambda bundle: bundle.__setitem__("verifier_head_sha", "0" * 40),
        lambda bundle: bundle["out_of_scope"].__setitem__("retention_mutated", True),
        lambda bundle: bundle["migration"].__setitem__("second_exit_code", 1),
    ],
)
def test_verifier_rejects_semantically_inconsistent_bundle(tmp_path: Path, mutate: Any) -> None:
    bundle = _bundle(tmp_path)
    mutate(bundle)
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_verifier_rejects_tampered_artifact(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    Path(bundle["receipts"]["enforce"]["path"]).write_text("{}\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError, match="byte count or sha256 mismatch"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("schema_version", ["1.0", "2.0"])
def test_verifier_requires_v2_receipts_bound_to_mutation_head(tmp_path: Path, schema_version: str) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["receipts"]["enforce"])
    receipt["schema_version"] = schema_version
    if schema_version == "1.0":
        del receipt["head_sha"]
    else:
        receipt["head_sha"] = "f" * 40
    bundle["receipts"]["enforce"] = _json_ref(tmp_path, f"enforce-{schema_version}.json", receipt)
    with pytest.raises(evidence.EvidenceError, match="artifact association|bound-1 semantics"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_verifier_rejects_schema_valid_receipt_with_bad_arithmetic(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    receipt = _receipt(enforce=True)
    receipt["per_table_totals"]["hydro.river_timeseries"]["before_bytes"] = 1
    bundle["receipts"]["enforce"] = _json_ref(tmp_path, "bad-enforce.json", receipt)
    invocation = _read_ref(bundle["receipts"]["enforce_invocation"])
    invocation["artifact_bindings"]["receipt_sha256"] = bundle["receipts"]["enforce"]["sha256"]
    bundle["receipts"]["enforce_invocation"] = _json_ref(tmp_path, "bad-enforce-invocation.json", invocation)
    with pytest.raises(evidence.EvidenceError, match="arithmetic"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("missing", ["recovery", "preflight", "selection", "receipts", "benchmarks", "cleanup"])
def test_verifier_rejects_required_top_level_omission(tmp_path: Path, missing: str) -> None:
    bundle = _bundle(tmp_path)
    del bundle[missing]
    with pytest.raises(evidence.EvidenceError, match="keys differ"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_verifier_recomputes_query_and_result_hashes(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    benchmark_ref = bundle["benchmarks"]["evidence"]
    benchmark = json.loads(Path(benchmark_ref["path"]).read_text(encoding="utf-8"))
    benchmark["queries"][0]["query_text"] += " -- tampered"
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "bad-benchmark.json", benchmark)
    with pytest.raises(evidence.EvidenceError, match="public production owner|query hash mismatch"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def _read_ref(ref: dict[str, Any]) -> dict[str, Any]:
    return json.loads(Path(ref["path"]).read_text(encoding="utf-8"))


def _replace_produced_artifact(
    bundle: dict[str, Any], kind: str, name: str, ref: dict[str, Any], tmp_path: Path
) -> None:
    plan = _read_ref(bundle["execution"]["run_plan"])
    if name in evidence.EXPECTED_CAPTURE_SEQUENCE:
        capture = next(item for item in plan["captures"] if item["kind"] == name)
        capture["output_path"] = ref["path"]
    else:
        command = next(item for item in plan["commands"] if item["kind"] == kind)
        command["artifact_associations"][name] = ref["path"]
    plan["run_plan_id"] = evidence._supervisor_run_plan_id(plan)
    bundle["execution"]["run_plan"] = _json_ref(tmp_path, f"updated-{name}-plan.json", plan)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    for event in events:
        event["run_plan_id"] = plan["run_plan_id"]
        if event.get("event_type") == "capture" and event.get("kind") == name:
            event["artifact_association"] = _observed(ref)
        elif (
            name not in evidence.EXPECTED_CAPTURE_SEQUENCE
            and event.get("event_type") == "child_exit"
            and event.get("kind") == kind
        ):
            event["artifact_associations"][name] = _observed(ref)
    ledger = tmp_path / f"updated-{name}-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(event) for event in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)


@pytest.mark.parametrize("missing", ["preflight", "receipt"])
def test_recovery_requires_two_artifacts(tmp_path: Path, missing: str) -> None:
    bundle = _bundle(tmp_path)
    del bundle["recovery"][missing]
    with pytest.raises(evidence.EvidenceError, match="recovery keys differ"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_recovery_rejects_tampered_artifact(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    Path(bundle["recovery"]["receipt"]["path"]).write_text("{}\n", encoding="utf-8")
    with pytest.raises(evidence.EvidenceError, match="byte count or sha256 mismatch"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


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
def test_recovery_rejects_required_field_omission(tmp_path: Path, artifact_name: str, missing: str) -> None:
    bundle = _bundle(tmp_path)
    raw = _read_ref(bundle["recovery"][artifact_name])
    del raw[missing]
    bundle["recovery"][artifact_name] = _json_ref(tmp_path, f"recovery-{artifact_name}-no-{missing}.json", raw)
    with pytest.raises(evidence.EvidenceError, match="keys differ"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_recovery_rejects_nonquiescent_safety_preflight(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["recovery"]["preflight"])
    preflight["autopipe_quiescent"] = False
    bundle["recovery"]["preflight"] = _json_ref(tmp_path, "recovery-nonquiescent.json", preflight)
    with pytest.raises(evidence.EvidenceError, match="mutation-head boundary"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize(
    ("field", "timestamp"),
    [
        ("started_at", "2026-07-15T11:39:59Z"),
        ("finished_at", "2026-07-15T11:50:01Z"),
    ],
)
def test_recovery_rejects_invalid_chronology(tmp_path: Path, field: str, timestamp: str) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["recovery"]["receipt"])
    receipt[field] = timestamp
    bundle["recovery"]["receipt"] = _json_ref(tmp_path, f"recovery-time-{field}.json", receipt)
    with pytest.raises(evidence.EvidenceError, match="chronology"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("artifact_name", ["preflight", "receipt"])
def test_recovery_rejects_mutation_head_drift(tmp_path: Path, artifact_name: str) -> None:
    bundle = _bundle(tmp_path)
    raw = _read_ref(bundle["recovery"][artifact_name])
    raw["mutation_head_sha"] = "f" * 40
    bundle["recovery"][artifact_name] = _json_ref(tmp_path, f"recovery-head-{artifact_name}.json", raw)
    with pytest.raises(evidence.EvidenceError, match="boundary"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("artifact_name", ["preflight", "receipt"])
def test_recovery_rejects_target_drift(tmp_path: Path, artifact_name: str) -> None:
    bundle = _bundle(tmp_path)
    raw = _read_ref(bundle["recovery"][artifact_name])
    raw["target"]["chunk_name"] = "_hyper_3_other_chunk"
    bundle["recovery"][artifact_name] = _json_ref(tmp_path, f"recovery-target-{artifact_name}.json", raw)
    with pytest.raises(evidence.EvidenceError, match="exact authorized chunk"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_recovery_rejects_row_parity_failure(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["recovery"]["receipt"])
    receipt["after_row_count"] += 1
    bundle["recovery"]["receipt"] = _json_ref(tmp_path, "recovery-row-drift.json", receipt)
    with pytest.raises(evidence.EvidenceError, match="row parity"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


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
    bundle["recovery"][artifact_name] = _json_ref(tmp_path, f"recovery-{artifact_name}-{field}.json", raw)
    with pytest.raises(evidence.EvidenceError, match=message):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_recovery_rejects_insufficient_free_space(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["recovery"]["preflight"])
    preflight["free_bytes"] = evidence.MIN_FREE_BYTES - 1
    bundle["recovery"]["preflight"] = _json_ref(tmp_path, "recovery-low-space.json", preflight)
    with pytest.raises(evidence.EvidenceError, match="below 300 GiB"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_selection_must_reselect_exact_recovered_target(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    for observation in ("post_dry_run", "pre_enforce"):
        snapshot = _read_ref(bundle["selection"][observation])
        snapshot["candidates"][0]["chunk_name"] = "_hyper_3_70_chunk"
        snapshot["selected"][0]["chunk_name"] = "_hyper_3_70_chunk"
        bundle["selection"][observation] = _json_ref(tmp_path, f"selection-{observation}-other-target.json", snapshot)
    with pytest.raises(evidence.EvidenceError, match="exact recovered chunk"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("receipt_name", ["dry_run", "enforce"])
def test_replay_receipts_must_reselect_exact_recovered_target(tmp_path: Path, receipt_name: str) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["receipts"][receipt_name])
    receipt["selected"][0]["chunk_name"] = "_hyper_3_other_chunk"
    bundle["receipts"][receipt_name] = _json_ref(tmp_path, f"{receipt_name}-other-target.json", receipt)
    with pytest.raises(evidence.EvidenceError, match="artifact association|selected tuples differ"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_recovery_authorization_and_truth_flag_are_required(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle["authorization"]["replay_decompression"] = False
    with pytest.raises(evidence.EvidenceError, match="authorization differs"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    bundle = _bundle(tmp_path)
    bundle["out_of_scope"]["decompress_run"] = False
    with pytest.raises(evidence.EvidenceError, match="out_of_scope"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("missing", ["captured_at", "mutation_head_sha", "container_state", "units"])
def test_preflight_rejects_missing_capture_contract(tmp_path: Path, missing: str) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["preflight"]["evidence"])
    del preflight[missing]
    bundle["preflight"]["evidence"] = _json_ref(tmp_path, f"preflight-no-{missing}.json", preflight)
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_preflight_rejects_posthoc_mutation_head_override(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle["mutation_head_sha"] = "f" * 40
    with pytest.raises(evidence.EvidenceError, match="mutation-head"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("missing", ["enabled", "active", "sub", "result", "main_pid", "journal"])
def test_preflight_rejects_incomplete_unit_state(tmp_path: Path, missing: str) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["preflight"]["evidence"])
    del preflight["units"]["nhms-node27-autopipe.service"][missing]
    bundle["preflight"]["evidence"] = _json_ref(tmp_path, f"preflight-unit-no-{missing}.json", preflight)
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_legacy_single_head_and_selection_bundle_fails_closed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle["head_sha"] = bundle.pop("mutation_head_sha")
    bundle["selection"] = {"snapshot": bundle["selection"]["post_dry_run"]}
    with pytest.raises(evidence.EvidenceError, match="keys differ"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_selection_requires_two_distinct_artifacts(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle["selection"]["pre_enforce"] = bundle["selection"]["post_dry_run"]
    with pytest.raises(evidence.EvidenceError, match="distinct observations"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_selection_rejects_incomplete_or_reordered_candidates(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    selection = _read_ref(bundle["selection"]["post_dry_run"])
    selection["candidates"] = selection["candidates"][:1]
    bundle["selection"]["post_dry_run"] = _json_ref(tmp_path, "selection-incomplete.json", selection)
    with pytest.raises(evidence.EvidenceError, match="complete ordered receipt scope"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_selection_rejects_tuple_drift_between_observations(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    selection = _read_ref(bundle["selection"]["pre_enforce"])
    selection["candidates"][0]["chunk_name"] = "_hyper_1_other_chunk"
    selection["selected"][0]["chunk_name"] = "_hyper_1_other_chunk"
    bundle["selection"]["pre_enforce"] = _json_ref(tmp_path, "selection-drift.json", selection)
    with pytest.raises(evidence.EvidenceError, match="selected tuples differ"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_selection_rejects_pre_enforce_observation_older_than_60_seconds(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    selection = _read_ref(bundle["selection"]["pre_enforce"])
    selection["observed_at"] = "2026-07-15T11:59:00Z"
    selection["cutoff"] = "2026-07-08T11:59:00Z"
    bundle["selection"]["pre_enforce"] = _json_ref(tmp_path, "selection-stale.json", selection)
    with pytest.raises(evidence.EvidenceError, match="within 60 seconds"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


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
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("missing", ["variable", "feature_limit", "simplification_tolerance_m"])
def test_mvt_binding_requires_exact_production_params(tmp_path: Path, missing: str) -> None:
    bundle = _mutated_benchmark_bundle(tmp_path, lambda benchmark: benchmark["queries"][1]["binding"].pop(missing))
    with pytest.raises(evidence.EvidenceError, match="exact production parameter"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


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
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_every_after_measurement_must_bind_decompress_chunk(tmp_path: Path) -> None:
    def mutate(benchmark: dict[str, Any]) -> None:
        benchmark["queries"][1]["after"]["measurements"][3]["plan"] = {
            "Node Type": "Index Scan",
            "Relation Name": "river_timeseries",
        }

    bundle = _mutated_benchmark_bundle(tmp_path, mutate)
    with pytest.raises(evidence.EvidenceError, match="after measurement 3"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_activity_sample_must_prove_stable_load(tmp_path: Path) -> None:
    def mutate(benchmark: dict[str, Any]) -> None:
        benchmark["queries"][0]["before"]["activity_samples"][0]["material_load_stable"] = False

    bundle = _mutated_benchmark_bundle(tmp_path, mutate)
    with pytest.raises(evidence.EvidenceError, match="load drift"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_cli_atomically_replaces_terminal_and_keeps_mode_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    source = (ROOT / "scripts/node27_timeseries_compression_live_evidence.py").read_text(encoding="utf-8")
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
    bundle["selection"]["pre_enforce"] = _json_ref(tmp_path, "selection-future-cutoff.json", snapshot)
    with pytest.raises(evidence.EvidenceError, match="observed_at minus"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("failure", ["reused", "nonzero", "timeout"])
def test_legacy_authored_invocations_do_not_contribute_to_v3_truth(tmp_path: Path, failure: str) -> None:
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
        bundle["migration"][key] = _json_ref(tmp_path, f"migration-{failure}.json", invocation)
    terminal = evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    assert terminal["qualifies_task_4_5"] is True


def test_dry_run_totals_are_recomputed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    receipt = _read_ref(bundle["receipts"]["dry_run"])
    receipt["per_table_totals"]["hydro.river_timeseries"]["chunks_compressed"] = 1
    bundle["receipts"]["dry_run"] = _json_ref(tmp_path, "dry-bad-totals.json", receipt)
    invocation = _read_ref(bundle["receipts"]["dry_run_invocation"])
    invocation["artifact_bindings"]["receipt_sha256"] = bundle["receipts"]["dry_run"]["sha256"]
    bundle["receipts"]["dry_run_invocation"] = _json_ref(tmp_path, "dry-bad-totals-invocation.json", invocation)
    with pytest.raises(evidence.EvidenceError, match="dry-run per_table_totals"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


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
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


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
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_dump_magic_and_cleanup_execstart_are_derived(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    dump = tmp_path / "fake.dump"
    dump.write_bytes(b"random bytes")
    bundle["preflight"]["schema_dump"] = _file_ref(dump)
    listing = _read_ref(bundle["preflight"]["schema_dump_list"])
    listing["dump_descriptor_sha256"] = bundle["preflight"]["schema_dump"]["sha256"]
    bundle["preflight"]["schema_dump_list"] = _json_ref(tmp_path, "fake-dump-list.json", listing)
    with pytest.raises(evidence.EvidenceError, match="custom format"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    cleanup = _read_ref(bundle["cleanup"]["evidence"])
    cleanup["resolved_exec_start"].remove("--enforce")
    bundle["cleanup"]["evidence"] = _json_ref(tmp_path, "cleanup-no-enforce.json", cleanup)
    with pytest.raises(evidence.EvidenceError, match="--enforce"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


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
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    bundle["benchmarks"]["evidence"]["bytes"] = evidence.MAX_JSON_ARTIFACT_BYTES + 1
    with pytest.raises(evidence.EvidenceError, match="byte ceiling"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    nested: Any = {"leaf": True}
    for _ in range(evidence.MAX_PLAN_DEPTH + 2):
        nested = {"next": nested}
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "too-deep.json", nested)
    with pytest.raises(evidence.EvidenceError, match="depth ceiling"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    cleanup = _read_ref(bundle["cleanup"]["evidence"])
    cleanup["api_key"] = "must-never-be-echoed"
    bundle["cleanup"]["evidence"] = _json_ref(tmp_path, "cleanup-secret.json", cleanup)
    with pytest.raises(evidence.EvidenceError, match="forbidden credential field"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_weak_schema_and_wrong_request_range_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(evidence.EvidenceError, match="canonical verifier checkout schema"):
        evidence.verify_bundle(_bundle(tmp_path), receipt_schema={}, verifier_head_sha=VERIFIER_HEAD)

    def mutate(document: dict[str, Any]) -> None:
        document["queries"][1]["request"]["valid_time"] = "2026-06-05T00:00:00Z"

    bundle = _mutated_benchmark_bundle(tmp_path, mutate)
    with pytest.raises(evidence.EvidenceError, match="public production owner|selected chunk range"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


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
    document["execution_bounds"]["before"] = {
        "started_at": "2026-07-15T11:54:59Z",
        "finished_at": "2026-07-15T11:55:10Z",
        "wall_seconds": 900,
    }
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "benchmark-reversed.json", document)
    with pytest.raises(evidence.EvidenceError, match="global chronology|supervisor child events"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_v3_terminal_retains_provenance_and_v2_cannot_qualify(tmp_path: Path) -> None:
    terminal = evidence.verify_bundle(_bundle(tmp_path), receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
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
        (
            ROOT / "docs/runbooks/receipts/tier-node27-timeseries-storage/"
            "timeseries-compression/terminal-replay-20260715T114625Z.json"
        ).read_text()
    )
    legacy["qualifies_task_4_5"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(legacy, EVIDENCE_SCHEMA)


def test_v3_failure_context_and_pass_failure_fields_are_exact_reader_contracts(
    tmp_path: Path,
) -> None:
    stale = tmp_path / "stale.json"
    stale.write_bytes(b'{"stale":true}\n')
    expected = terminal_state.terminal_identity(stale)
    assert expected is not None
    unavailable, _ = terminal_state.unavailable_failure_payload(
        stage="provenance-unavailable",
        expected_output=expected,
        verifier_head_sha=VERIFIER_HEAD,
    )
    validator = jsonschema.Draft202012Validator(
        EVIDENCE_SCHEMA, format_checker=jsonschema.FormatChecker()
    )
    validator.validate(unavailable)
    missing = json.loads(json.dumps(unavailable))
    missing.pop("failure_context")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(missing)
    missing_path = tmp_path / "missing-unavailable-context.json"
    missing_path.write_bytes(_canonical(missing))
    with pytest.raises(evidence.EvidenceError, match="schema-valid"):
        evidence.read_authoritative_terminal(missing_path)
    tampered = json.loads(json.dumps(unavailable))
    tampered["failure_context"]["foreign"] = True
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(tampered)
    tampered_path = tmp_path / "tampered-unavailable.json"
    tampered_path.write_bytes(_canonical(tampered))
    with pytest.raises(evidence.EvidenceError, match="schema-valid"):
        evidence.read_authoritative_terminal(tampered_path)

    bound, _ = terminal_state.bound_failure_payload(
        stage="systemd-stop-post",
        expected_output=expected,
        run_id="bound-run",
        mutation_head_sha=HEAD,
        possible_mutation=True,
    )
    validator.validate(bound)
    bound_path = tmp_path / "bound-terminal.json"
    bound_path.write_bytes(_canonical(bound))
    assert evidence.read_authoritative_terminal(bound_path)["provenance_state"] == "bound"

    fixture_dir = tmp_path / "pass-fixture"
    fixture_dir.mkdir()
    bundle = _bundle(fixture_dir)
    qualifying = evidence.verify_bundle(
        bundle,
        receipt_schema=RECEIPT_SCHEMA,
        verifier_head_sha=VERIFIER_HEAD,
        artifact_manifest=resolve_artifact_closure(bundle).manifest,
    )
    failure_only_values = {
        "failure": {"stage": "forbidden", "mutation_state": "indeterminate"},
        "failure_context": unavailable["failure_context"],
        "provenance_state": "bound",
        "outcome": "failed",
    }
    for index, (field, value) in enumerate(failure_only_values.items()):
        invalid_pass = {**qualifying, field: value}
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid_pass)
        path = tmp_path / f"invalid-pass-{index}.json"
        path.write_bytes(_canonical(invalid_pass))
        with pytest.raises(evidence.EvidenceError, match="schema-valid|failure-only"):
            evidence.read_authoritative_terminal(path)


def test_historical_v2_terminal_remains_readable_but_nonqualifying(tmp_path: Path) -> None:
    historical = (
        ROOT
        / "docs/runbooks/receipts/tier-node27-timeseries-storage/timeseries-compression/"
        "terminal-replay-20260715T114625Z.json"
    )
    raw = historical.read_bytes()
    document = json.loads(raw)
    jsonschema.Draft202012Validator(EVIDENCE_SCHEMA).validate(document)
    output = tmp_path / "historical-v2.json"
    output.write_bytes(raw)
    authoritative = evidence.read_authoritative_terminal(output)
    assert authoritative["schema_version"] == "2.0"
    assert authoritative.get("qualifies_task_4_5") is not True


@pytest.mark.parametrize("hardlink", [False, True])
def test_terminal_output_alias_preserves_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hardlink: bool) -> None:
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
    assert not evidence._terminal_intent_root_path(output).exists()
    assert not evidence._terminal_intent_gate_path(output).exists()
    assert not evidence._terminal_intent_state_path(output).exists()


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
    "corruption",
    ["verifier-binding", "run-plan-reference", "ledger-jsonl", "ledger-run-id"],
)
def test_post_disjointness_provenance_failure_invalidates_old_qualifying_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    bundle = _bundle(tmp_path)
    original_closure = resolve_artifact_closure(bundle)
    old_pass = evidence.verify_bundle(
        bundle,
        receipt_schema=RECEIPT_SCHEMA,
        verifier_head_sha=VERIFIER_HEAD,
        artifact_manifest=original_closure.manifest,
    )
    output = tmp_path / "terminal.json"
    output.write_bytes(_canonical(old_pass))
    old_identity = evidence._output_identity(output)
    if corruption == "verifier-binding":
        bundle["verifier_head_sha"] = "0" * 40
    elif corruption == "run-plan-reference":
        bundle["execution"]["run_plan"] = _json_ref(tmp_path, "not-a-run-plan.json", {"not": "a-run-plan"})
    else:
        ledger = tmp_path / f"{corruption}.jsonl"
        if corruption == "ledger-jsonl":
            ledger.write_bytes(b'{"run_id":"run-1069"}\nnot-json\n')
        else:
            ledger.write_bytes(b'{"run_id":"run-1069"}\n{"run_id":"foreign-run"}\n')
        bundle["execution"]["ledger"] = _file_ref(ledger)
    bundle_path = tmp_path / f"bundle-{corruption}.json"
    bundle_path.write_bytes(_canonical(bundle))
    monkeypatch.setattr(evidence, "_current_verifier_head", lambda: VERIFIER_HEAD)
    assert evidence.main(["--bundle-path", str(bundle_path), "--output-path", str(output)]) == 1
    terminal = evidence.read_authoritative_terminal(output)
    assert terminal["qualifies_task_4_5"] is False
    assert terminal["provenance_state"] == "unavailable"
    assert terminal["failure"]["stage"] == "provenance_unavailable"
    assert terminal["failure_context"] == {
        "reason_category": "provenance_unavailable",
        "expected_output": evidence._identity_document(old_identity),
        "verifier_head_sha": VERIFIER_HEAD,
    }
    assert "run_id" not in terminal
    assert "mutation_head_sha" not in terminal
    assert not evidence._terminal_intent_root_path(output).exists()


def test_untrusted_current_verifier_failure_does_not_fabricate_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path)
    closure = resolve_artifact_closure(bundle)
    old_pass = evidence.verify_bundle(
        bundle,
        receipt_schema=RECEIPT_SCHEMA,
        verifier_head_sha=VERIFIER_HEAD,
        artifact_manifest=closure.manifest,
    )
    output = tmp_path / "terminal.json"
    output.write_bytes(_canonical(old_pass))
    bundle_path = tmp_path / "bundle-untrusted-verifier.json"
    bundle_path.write_bytes(_canonical(bundle))

    def fail_verifier_identity() -> str:
        raise evidence.EvidenceError("cannot independently bind verifier")

    monkeypatch.setattr(evidence, "_current_verifier_head", fail_verifier_identity)
    assert evidence.main(["--bundle-path", str(bundle_path), "--output-path", str(output)]) == 1
    terminal = evidence.read_authoritative_terminal(output)
    assert terminal["qualifies_task_4_5"] is False
    assert terminal["failure_context"]["verifier_head_sha"] is None
    assert "mutation_head_sha" not in terminal and "run_id" not in terminal


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
            "Plans": [{"Node Type": "Index Scan", "Relation Name": IDENTITY["chunk_name"]}],
        },
    ],
)
def test_plan_suffix_filter_and_child_decoys_fail(tmp_path: Path, plan: dict[str, Any]) -> None:
    bundle = _bundle(tmp_path)
    document = _read_ref(bundle["benchmarks"]["evidence"])
    document["queries"][0]["after"]["measurements"][0]["plan"] = plan
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "plan-decoy-v2.json", document)
    with pytest.raises(evidence.EvidenceError, match="lacks selected DecompressChunk"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_snapshot_bijection_rejects_cross_table_sibling_reuse(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    post = _read_ref(bundle["sizes"]["post"])
    copied = dict(post["tables"]["hydro.river_timeseries"]["compressed_relations"][0])
    copied["origin_chunk_name"] = "_hyper_2_20_chunk"
    post["tables"]["met.forcing_station_timeseries"]["compressed_chunks"] = 1
    post["tables"]["met.forcing_station_timeseries"]["compressed_relations"] = [copied]
    bundle["sizes"]["post"] = _json_ref(tmp_path, "cross-table-sibling.json", post)
    with pytest.raises(evidence.EvidenceError, match="bijection"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_repo_path_and_remote_lineage_are_pinned(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["preflight"]["evidence"])
    preflight["repo_path"] = "/tmp/unrelated"
    bundle["preflight"]["evidence"] = _json_ref(tmp_path, "wrong-repo.json", preflight)
    with pytest.raises(evidence.EvidenceError, match="mutation-head boundary"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    bundle["authorization"]["remote_identity"] = "attacker/repo"
    with pytest.raises(evidence.EvidenceError, match="authorization differs"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_supervisor_ledger_rejects_extra_or_unowned_invocation(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    ledger_path = Path(bundle["execution"]["ledger"]["path"])
    events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    child = next(event for event in events if event["event_type"] == "child_exit")
    extra = {**child, "event_id": "unowned", "command_id": "not-in-plan"}
    altered = tmp_path / "unowned-ledger.jsonl"
    altered.write_bytes(b"".join(_canonical(event) for event in [*events, extra]))
    bundle["execution"]["ledger"] = _file_ref(altered)
    with pytest.raises(evidence.EvidenceError, match="unowned child"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_text_journal_secret_assignment_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    cleanup = _read_ref(bundle["cleanup"]["evidence"])
    journal = tmp_path / "secret-journal.log"
    journal.write_text("status=ok token=never-print-this\n", encoding="utf-8")
    cleanup["final_units"]["nhms-node27-autopipe.service"]["journal"] = _file_ref(journal)
    bundle["cleanup"]["evidence"] = _json_ref(tmp_path, "secret-journal.json", cleanup)
    with pytest.raises(evidence.EvidenceError, match="credential") as caught:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
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
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "exclusive-end-curve.json", document)
    with pytest.raises(evidence.EvidenceError, match="selected chunk range"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_retained_reference_change_after_publish_replaces_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _bundle(tmp_path)
    bundle_path = tmp_path / "bundle-retained.json"
    bundle_path.write_bytes(_canonical(bundle))
    output = tmp_path / "terminal.json"
    retained = Path(bundle["receipts"]["dry_run"]["path"])
    original_publish = terminal_state._atomic_replace_terminal_at
    changed = False

    def publish(
        parent_fd: int,
        parent_path: Path,
        path: Path,
        payload: bytes,
        *,
        expected: Any,
    ) -> Any:
        nonlocal changed
        published = original_publish(parent_fd, parent_path, path, payload, expected=expected)
        if path == output and not changed:
            retained.write_text("{}\n", encoding="utf-8")
            changed = True
        return published

    monkeypatch.setattr(terminal_state, "_atomic_replace_terminal_at", publish)
    monkeypatch.setattr(evidence, "_current_verifier_head", lambda: VERIFIER_HEAD)
    assert evidence.main(["--bundle-path", str(bundle_path), "--output-path", str(output)]) == 1
    marker = json.loads(output.read_text(encoding="utf-8"))
    assert marker["qualifies_task_4_5"] is False


def test_round3_current_d3_passes_and_catalog_drift_fails(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    assert (
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)["preflight"][
            "quiescent"
        ]
        is True
    )
    before = _read_ref(bundle["preflight"]["catalog_before"])
    before["catalog"]["compression_settings"][0]["segmentby_column_index"] = 99
    bundle["preflight"]["catalog_before"] = _json_ref(tmp_path, "catalog-before-drift.json", before)
    with pytest.raises(evidence.EvidenceError, match="exact D3|neither pristine"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_round3_checkpoint_bijection_and_raw_refs_are_required(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    plan = _read_ref(bundle["execution"]["run_plan"])
    plan["checkpoints"].pop()
    plan["run_plan_id"] = evidence._supervisor_run_plan_id(plan)
    bundle["execution"]["run_plan"] = _json_ref(tmp_path, "missing-checkpoint-plan.json", plan)
    with pytest.raises(evidence.EvidenceError, match="checkpoint|run-plan identity"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    checkpoint = next(item for item in events if item["event_type"] == "checkpoint")
    activity = _read_ref(checkpoint["database_activity"]["artifact"])
    activity["sessions"] = [{"pid": 999}]
    checkpoint["database_activity"] = _observed(_json_ref(tmp_path, "conflicting-session.json", activity))
    ledger = tmp_path / "conflicting-session-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(item) for item in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)
    with pytest.raises(evidence.EvidenceError, match="conflicting writer"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_round3_plan_hash_ledger_order_journal_and_observed_associations_are_derived(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    plan = _read_ref(bundle["execution"]["run_plan"])
    plan["commands"][0]["argv"].append("--tampered")
    bundle["execution"]["run_plan"] = _json_ref(tmp_path, "tampered-plan.json", plan)
    with pytest.raises(evidence.EvidenceError, match="run plan provenance"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    before_index = next(
        index
        for index, event in enumerate(events)
        if event["event_type"] == "checkpoint" and event["phase"] == "before_mutation"
    )
    events[before_index], events[before_index + 1] = events[before_index + 1], events[before_index]
    ledger = tmp_path / "reordered-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(event) for event in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)
    with pytest.raises(evidence.EvidenceError, match="chronology|monotonic|strict"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    checkpoint = next(event for event in events if event["event_type"] == "checkpoint")
    journal = Path(checkpoint["journal"]["artifact"]["path"])
    journal.write_text(
        json.dumps(
            {
                "_SYSTEMD_UNIT": "user@1000.service",
                "_SYSTEMD_USER_UNIT": "nhms-node27-timeseries-compression.service",
                "_SYSTEMD_INVOCATION_ID": "2" * 32,
            },
            sort_keys=True,
        )
        + "\n"
        f"-- cursor: {checkpoint['journal_end_cursor']}\n"
    )
    checkpoint["journal"] = _observed(_file_ref(journal))
    ledger = tmp_path / "activation-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(event) for event in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)
    with pytest.raises(evidence.EvidenceError, match="recurring compression activation"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    dry_event = next(event for event in events if event.get("kind") == "compression_dry_run")
    dry_event["artifact_associations"]["dry_run_receipt"] = _observed(bundle["selection"]["post_dry_run"])
    ledger = tmp_path / "association-mismatch-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(event) for event in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)
    with pytest.raises(evidence.EvidenceError, match="artifact path differs|artifact association"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_round3_canonical_unit_and_container_pg_restore_are_bound(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    cleanup = _read_ref(bundle["cleanup"]["evidence"])
    cleanup["installed_unit_paths"]["service"] = "/tmp/same-bytes.service"
    bundle["cleanup"]["evidence"] = _json_ref(tmp_path, "wrong-unit-path.json", cleanup)
    with pytest.raises(evidence.EvidenceError, match="canonical user units"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    listing = _read_ref(bundle["preflight"]["schema_dump_list"])
    listing["list_argv"] = [
        "/usr/bin/pg_restore",
        "--list",
        "/var/lib/postgresql/evidence/schema.dump",
    ]
    bundle["preflight"]["schema_dump_list"] = _json_ref(tmp_path, "host-pg-restore.json", listing)
    with pytest.raises(evidence.EvidenceError, match="not verifiable"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


# A minimal docker stub equivalent to the supervisor suite's `_docker_responses`
# machinery: it dispatches on the argv tokens the real resolver emits so the REAL
# producer can run in-process here without a container.
_CROSS_PLANE_DOCKER_STUB = """#!{python}
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "docker.responses.json"), encoding="utf-8") as _fh:
    _responses = json.load(_fh)
_argv = " ".join(sys.argv[1:])
for _response in _responses:
    if all(_token in _argv for _token in _response["match"]):
        sys.stdout.write(_response.get("stdout", ""))
        sys.exit(_response.get("exit", 0))
sys.stderr.write("no docker stub response for argv: " + _argv + "\\n")
sys.exit(97)
"""


def _supervisor_pg_restore_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, realpath: str
) -> dict[str, str]:
    """Run the REAL supervisor producer through a docker stub, so the verifier's
    expected pg_restore identity is derived from the producer rather than a
    hand-authored constant.  The stub reproduces the MEASURED (Round-5 gate §G2)
    container contract.
    """
    bindir = tmp_path / "supervisor-bin"
    bindir.mkdir()
    dump_path = "/var/lib/postgresql/evidence/schema.dump"
    image = "sha256:" + "1" * 64
    binary_sha = "2" * 64
    dump_sha = "3" * 64
    responses = [
        {"match": ["inspect"], "stdout": image + "\n"},
        {"match": ["readlink"], "stdout": realpath + "\n"},
        {"match": ["sha256sum"], "stdout": f"{binary_sha}  {realpath}\n{dump_sha}  {dump_path}\n"},
    ]
    stub = bindir / "docker"
    stub.write_text(_CROSS_PLANE_DOCKER_STUB.replace("{python}", sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    (bindir / "docker.responses.json").write_text(json.dumps(responses), encoding="utf-8")
    monkeypatch.setattr(supervisor, "SUPERVISOR_BIN_DIR", bindir)
    return supervisor.resolve_container_pg_restore_identity(
        wall=supervisor.HardWall.start(30), dump_path=dump_path
    )


def _ledger_events(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    ledger_path = Path(bundle["execution"]["ledger"]["path"])
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]


def test_cross_plane_pg_restore_realpath_binds_producer_to_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression lock for the issue-1069 defect class (an external-contract value
    # hard-coded independently in two planes, where a fix updates one and leaves
    # the twin rotted).  Derive the verifier's expected pg_restore realpath from
    # the REAL supervisor producer -- never a hand-authored assumption -- and
    # require the verifier to ACCEPT exactly that realpath end to end.
    identity = _supervisor_pg_restore_identity(
        tmp_path, monkeypatch, realpath="/usr/share/postgresql-common/pg_wrapper"
    )
    assert identity["binary_realpath"] == evidence.CONTAINER_PG_RESTORE_REALPATH

    bundle = _bundle(tmp_path)
    listing = _read_ref(bundle["preflight"]["schema_dump_list"])
    version_event = next(
        event for event in _ledger_events(bundle) if event.get("kind") == "pg_restore_version"
    )
    list_event = next(
        event for event in _ledger_events(bundle) if event.get("kind") == "pg_restore_list"
    )
    # The terminal document the fixed supervisor produces carries the producer's
    # realpath in BOTH planes -- the dump-listing document AND the ledger
    # association the verifier cross-checks:
    assert listing["binary_realpath"] == identity["binary_realpath"]
    assert version_event["artifact_associations"]["binary_realpath"] == identity["binary_realpath"]
    assert list_event["artifact_associations"]["binary_realpath"] == identity["binary_realpath"]
    # ... and the verifier ACCEPTS it end to end (dump-listing guard + association
    # cross-check).  No EvidenceError.
    evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    # Negative: reverting the dump-listing realpath to the refuted
    # /usr/bin/pg_restore symlink path (the exact value the fix removed) must turn
    # this suite RED, so any regression re-pinning the old value is caught.
    reverted = _bundle(tmp_path)
    reverted_listing = _read_ref(reverted["preflight"]["schema_dump_list"])
    reverted_listing["binary_realpath"] = "/usr/bin/pg_restore"
    reverted["preflight"]["schema_dump_list"] = _json_ref(
        tmp_path, "reverted-realpath-dump-list.json", reverted_listing
    )
    with pytest.raises(evidence.EvidenceError, match="not verifiable"):
        evidence.verify_bundle(reverted, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_round3_raw_plan_summaries_and_snapshot_maps_are_derived(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    benchmark_document = _read_ref(bundle["benchmarks"]["evidence"])
    benchmark_document["queries"][0]["after"]["measurements"][0]["execution_ms"] += 1
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "authored-timing.json", benchmark_document)
    with pytest.raises(evidence.EvidenceError, match="authored timing"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    benchmark_document = _read_ref(bundle["benchmarks"]["evidence"])
    benchmark_document["queries"][0]["after"]["measurements"][0]["plan"]["Plan"]["Alias"] = IDENTITY["chunk_name"]
    bundle["benchmarks"]["evidence"] = _json_ref(tmp_path, "wrong-real-alias.json", benchmark_document)
    with pytest.raises(evidence.EvidenceError, match="DecompressChunk"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    bundle = _bundle(tmp_path)
    pre = _read_ref(bundle["sizes"]["pre"])
    pre["selected_origin_uncompressed_index"] = 0
    bundle["sizes"]["pre"] = _json_ref(tmp_path, "wrong-uncompressed-state.json", pre)
    with pytest.raises(evidence.EvidenceError, match="uncompressed-state"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_round3_v3_schema_requires_request_and_execution_bounds(tmp_path: Path) -> None:
    terminal = evidence.verify_bundle(_bundle(tmp_path), receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    broken_request = json.loads(json.dumps(terminal))
    broken_request["benchmarks"]["queries"][0].pop("request")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken_request, EVIDENCE_SCHEMA)
    broken_bounds = json.loads(json.dumps(terminal))
    broken_bounds["benchmarks"]["queries"][0]["before_capture"].pop("execution_bounds")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken_bounds, EVIDENCE_SCHEMA)


def test_round3_source_manifest_is_exact_transitive_closure(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    terminal = evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    assert terminal["source_manifest"] == list(resolve_artifact_closure(bundle).manifest)


def _replace_execution_events(bundle: dict[str, Any], events: list[dict[str, Any]], path: Path) -> None:
    path.write_bytes(b"".join(_canonical(event) for event in events))
    bundle["execution"]["ledger"] = _file_ref(path)


def test_round3_same_bytes_inode_replacement_fails(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    target = Path(bundle["preflight"]["evidence"]["path"])
    raw = target.read_bytes()
    replacement = tmp_path / "same-bytes-replacement.json"
    replacement.write_bytes(raw)
    replacement.replace(target)
    with pytest.raises(evidence.EvidenceError, match="inode identity changed"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("name", ["preflight_evidence", "cleanup"])
def test_round3_unassociated_semantic_output_fails(tmp_path: Path, name: str) -> None:
    bundle = _bundle(tmp_path)
    plan = _read_ref(bundle["execution"]["run_plan"])
    capture = next(item for item in plan["captures"] if item["kind"] == name)
    plan["captures"].remove(capture)
    plan["run_plan_id"] = evidence._supervisor_run_plan_id(plan)
    bundle["execution"]["run_plan"] = _json_ref(tmp_path, f"missing-{name}-plan.json", plan)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    for event in events:
        event["run_plan_id"] = plan["run_plan_id"]
    events = [event for event in events if not (event.get("event_type") == "capture" and event.get("kind") == name)]
    _replace_execution_events(bundle, events, tmp_path / f"missing-{name}-ledger.jsonl")
    with pytest.raises(evidence.EvidenceError, match="capture order/cardinality|ownership bijection"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_round3_duplicate_semantic_output_owner_fails(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    plan = _read_ref(bundle["execution"]["run_plan"])
    duplicate = bundle["preflight"]["evidence"]
    command = next(item for item in plan["commands"] if item["kind"] == "migration_apply")
    command["artifact_associations"]["preflight_evidence"] = duplicate["path"]
    plan["run_plan_id"] = evidence._supervisor_run_plan_id(plan)
    bundle["execution"]["run_plan"] = _json_ref(tmp_path, "duplicate-owner-plan.json", plan)
    with pytest.raises(evidence.EvidenceError, match="duplicate producers"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize("kind", sorted(set(evidence.EXPECTED_LEDGER_SEQUENCE)))
def test_round3_each_kind_rejects_true_substitution(tmp_path: Path, kind: str) -> None:
    bundle = _bundle(tmp_path)
    plan = _read_ref(bundle["execution"]["run_plan"])
    command = next(item for item in plan["commands"] if item["kind"] == kind)
    command["argv"] = ["/bin/true"]
    plan["run_plan_id"] = evidence._supervisor_run_plan_id(plan)
    bundle["execution"]["run_plan"] = _json_ref(tmp_path, f"true-{kind}-plan.json", plan)
    with pytest.raises(evidence.EvidenceError, match="executable|argv|entrypoint|contract"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def _rewrite_checkpoint_json(
    tmp_path: Path,
    event: dict[str, Any],
    key: str,
    value: dict[str, Any],
    serial: str,
) -> None:
    event[key] = _observed(_json_ref(tmp_path, f"{serial}-{key}.json", value))


def test_round3_journal_user_unit_fields_govern_activation_identity(tmp_path: Path) -> None:
    for variant in (
        "missing",
        "manager-noise",
        "arbitrary-noise",
        "empty-user-field",
        "manager-both-foreign",
        "manager-both-recurring",
        "manager-both-conflict",
        "user-unit",
        "legacy-user-unit",
        "foreign",
        "recurring",
    ):
        variant_path = tmp_path / variant
        variant_path.mkdir()
        bundle = _bundle(variant_path)
        events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
        checkpoints = [event for event in events if event["event_type"] == "checkpoint"]
        for index, event in enumerate(checkpoints):
            cursor = event["journal_end_cursor"]
            rows: list[dict[str, Any]] = []
            if variant == "manager-noise":
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "user@1000.service",
                        "_SYSTEMD_INVOCATION_ID": INVOCATION_ID,
                    }
                )
            if variant == "arbitrary-noise":
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "unrelated.service",
                        "_SYSTEMD_INVOCATION_ID": INVOCATION_ID,
                    }
                )
            if variant == "empty-user-field":
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "nhms-node27-timeseries-compression.service",
                        "_SYSTEMD_USER_UNIT": "",
                        "_SYSTEMD_INVOCATION_ID": "3" * 32,
                    }
                )
            if variant == "manager-both-foreign" and index == 0:
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "user@1000.service",
                        "_SYSTEMD_USER_UNIT": "init.scope",
                        "USER_UNIT": "nhms-node27-timeseries-compression-replay.service",
                        "_SYSTEMD_INVOCATION_ID": "4" * 32,
                    }
                )
            if variant == "manager-both-recurring" and index == 0:
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "user@1000.service",
                        "_SYSTEMD_USER_UNIT": "init.scope",
                        "USER_UNIT": "nhms-node27-timeseries-compression.service",
                        "_SYSTEMD_INVOCATION_ID": "5" * 32,
                    }
                )
            if variant == "manager-both-conflict" and index == 0:
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "user@1000.service",
                        "_SYSTEMD_USER_UNIT": "nhms-node27-timeseries-compression-replay.service",
                        "USER_UNIT": "nhms-node27-timeseries-compression.service",
                        "_SYSTEMD_INVOCATION_ID": INVOCATION_ID,
                    }
                )
            if variant == "user-unit":
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "user@1000.service",
                        "_SYSTEMD_USER_UNIT": "nhms-node27-timeseries-compression-replay.service",
                        "_SYSTEMD_INVOCATION_ID": INVOCATION_ID,
                    }
                )
            if variant == "legacy-user-unit":
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "user@1000.service",
                        "USER_UNIT": "nhms-node27-timeseries-compression-replay.service",
                        "_SYSTEMD_INVOCATION_ID": INVOCATION_ID,
                    }
                )
            if variant == "foreign" and index == 0:
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "user@1000.service",
                        "_SYSTEMD_USER_UNIT": "nhms-node27-timeseries-compression-replay.service",
                        "_SYSTEMD_INVOCATION_ID": "2" * 32,
                    }
                )
            if variant == "recurring" and index == 0:
                rows.append(
                    {
                        "_SYSTEMD_UNIT": "user@1000.service",
                        "_SYSTEMD_USER_UNIT": "nhms-node27-timeseries-compression.service",
                        "_SYSTEMD_INVOCATION_ID": "3" * 32,
                    }
                )
            journal = variant_path / f"journal-{index}.log"
            journal.write_text("".join(json.dumps(row) + "\n" for row in rows) + f"-- cursor: {cursor}\n")
            event["journal"] = _observed(_file_ref(journal))
        _replace_execution_events(bundle, events, variant_path / "changed-ledger.jsonl")
        if variant in {
            "missing",
            "manager-noise",
            "arbitrary-noise",
            "empty-user-field",
            "user-unit",
            "legacy-user-unit",
        }:
            evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
        elif variant in {"foreign", "manager-both-foreign"}:
            with pytest.raises(evidence.EvidenceError, match="additional replay activation"):
                evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
        elif variant in {"recurring", "manager-both-recurring"}:
            with pytest.raises(evidence.EvidenceError, match="recurring compression activation"):
                evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
        else:
            with pytest.raises(evidence.EvidenceError, match="fields conflict"):
                evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_round3_active_running_variant_and_wrong_manager_invocation_fail(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    checkpoints = [event for event in events if event["event_type"] == "checkpoint"]
    for index, event in enumerate(checkpoints):
        show = _read_ref(event["systemd_show"]["artifact"])
        show["replay"].update({"ActiveState": "active", "SubState": "running"})
        _rewrite_checkpoint_json(tmp_path, event, "systemd_show", show, f"active-{index}")
    _replace_execution_events(bundle, events, tmp_path / "active-ledger.jsonl")
    with pytest.raises(evidence.EvidenceError, match="active owner"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)

    wrong_path = tmp_path / "wrong"
    wrong_path.mkdir()
    bundle = _bundle(wrong_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    checkpoint = next(event for event in events if event["event_type"] == "checkpoint")
    show = _read_ref(checkpoint["systemd_show"]["artifact"])
    show["replay"]["InvocationID"] = "2" * 32
    _rewrite_checkpoint_json(wrong_path, checkpoint, "systemd_show", show, "wrong")
    _replace_execution_events(bundle, events, wrong_path / "wrong-ledger.jsonl")
    with pytest.raises(evidence.EvidenceError, match="active owner"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("InvocationID", ""),
        ("MainPID", 9999),
        ("ExecMainStartTimestamp", ""),
        # An actively-starting replay unit that reports systemd's unset "n/a"
        # sentinel never really started and must be rejected as the active owner.
        ("ExecMainStartTimestamp", evidence.SYSTEMD_UNSET_TIMESTAMP),
        ("ExecMainStartTimestampMonotonic", 0),
    ],
)
def test_round3_current_activation_identity_is_complete_and_pid_bound(
    tmp_path: Path, field: str, value: Any
) -> None:
    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    checkpoint = next(event for event in events if event["event_type"] == "checkpoint")
    show = _read_ref(checkpoint["systemd_show"]["artifact"])
    show["replay"][field] = value
    _rewrite_checkpoint_json(tmp_path, checkpoint, "systemd_show", show, f"broken-{field}")
    _replace_execution_events(bundle, events, tmp_path / f"broken-{field}-ledger.jsonl")
    with pytest.raises(evidence.EvidenceError, match="active owner"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize(
    ("capture_kind", "started_at", "finished_at"),
    [
        ("preflight_evidence", "2026-07-15T11:19:02.100000Z", "2026-07-15T11:19:02.200000Z"),
        ("sizes_post", "2026-07-15T12:00:24.600000Z", "2026-07-15T12:00:24.700000Z"),
    ],
)
def test_round3_capture_pre_post_causality_is_strict(
    tmp_path: Path, capture_kind: str, started_at: str, finished_at: str
) -> None:
    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    capture = next(
        event for event in events if event.get("event_type") == "capture" and event.get("kind") == capture_kind
    )
    capture["started_at"] = started_at
    capture["finished_at"] = finished_at
    capture["started_monotonic"] = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
    capture["finished_monotonic"] = datetime.fromisoformat(finished_at.replace("Z", "+00:00")).timestamp()
    events.sort(key=lambda event: event.get("started_monotonic", event.get("monotonic")))
    _replace_execution_events(bundle, events, tmp_path / f"bad-{capture_kind}-causality.jsonl")
    with pytest.raises(evidence.EvidenceError, match="capture owner chronology"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
