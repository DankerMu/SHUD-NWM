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
from scripts import node27_timeseries_compression_bundle_author as bundle_author
from scripts import node27_timeseries_compression_live_evidence as evidence
from scripts import node27_timeseries_compression_plan_author as plan_author
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
# The replay entrypoint's option name for each recovery-target field, in the exact
# order the decompress argv carries them (#1244).
_RECOVERY_TARGET_OPTIONS = {
    "hypertable_schema": "--hypertable-schema",
    "hypertable_name": "--hypertable-name",
    "chunk_schema": "--chunk-schema",
    "chunk_name": "--chunk-name",
    "range_start": "--range-start",
    "range_end": "--range-end",
}


def _recovery_target_argv_tail(**overrides: str) -> list[str]:
    """The six exact-chunk options, built from the verifier's own bound constant.

    The bundle fixture below no longer keeps an independent literal copy of the
    tail, so this helper is definitional against the verifier for the accepted
    case; the oracles that stay independent are the per-field deviation
    rejections that pass ``overrides``, the contract drift guard in
    tests/test_node27_timeseries_compression_supervisor.py, and the plan_author
    e2e (``test_real_state_machine_bundle_verifies_task_4_5_pass``).
    """

    values = {**evidence.RECOVERY_TARGET, **overrides}
    return [token for field, option in _RECOVERY_TARGET_OPTIONS.items() for token in (option, values[field])]


def _decompress_argv(receipt_path: str, **overrides: str) -> list[str]:
    return [
        "/home/nwm/NWM/.venv/bin/python",
        "/home/nwm/NWM/scripts/node27_timeseries_decompression_replay.py",
        "--database",
        "nhms",
        "--mutation-head-sha",
        HEAD,
        "--receipt-path",
        receipt_path,
        *_recovery_target_argv_tail(**overrides),
    ]


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


def _pinned_capture_options(database: str) -> list[str]:
    """The capture-argv options this helper can bind to PRODUCTION values, all of them.

    Both argv templates in this module (`_bundle`'s captures and `_producer_argv`) bind
    these identically so a template stays a production-shaped argv and every test below
    corrupts exactly the one field it is about.  `--database` is the only dynamic entry
    (it tracks the plan's own database); the other seven come from the verifier's public
    map, whose VALUES are pinned literally and against `plan_author` by the structural
    tests at the end of this module -- so sharing the map here cannot go tautological.

    `--evidence-dir` is value-pinned by the verifier too, but deliberately NOT bound here:
    the verifier derives its expected value RELATIONALLY from each capture's own
    `output_path`, so there is no production literal to share -- every caller binds it
    from the tmp root its captures actually write to.
    """

    return [
        "--database",
        database,
        *(
            token
            for option, value in evidence.EXPECTED_CAPTURE_TOOL_VALUES.items()
            for token in (option, value)
        ),
    ]


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


def _checkpoint_session(
    backend_type: str,
    *,
    pid: int = 999,
    usename: str | None = "nhms",
    has_write_privilege_on_target: bool = False,
) -> dict[str, Any]:
    """A checkpoint-shape pg_stat_activity session (verifier exact-key set)."""

    return {
        "pid": pid,
        "state": "active",
        "wait_event_type": None,
        "backend_type": backend_type,
        "usename": usename,
        "has_write_privilege_on_target": has_write_privilege_on_target,
    }


def _benchmark_session(
    backend_type: str,
    *,
    pid: int = 1135,
    usename: str | None = "nhms",
    has_write_privilege_on_target: bool = False,
) -> dict[str, Any]:
    """A benchmark-shape activity session (producer/verifier exact-key set)."""

    return {
        "pid": pid,
        "backend_start": "2026-07-15T12:00:00Z",
        "xact_start": None,
        "query_start": "2026-07-15T12:00:01Z",
        "state": "active",
        "wait_event_type": None,
        "backend_type": backend_type,
        "usename": usename,
        "has_write_privilege_on_target": has_write_privilege_on_target,
        "query_signature": "a" * 32,
    }


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
    replay_unit = "nhms-node27-timeseries-compression-replay.service"
    for unit_name in evidence.EXPECTED_UNITS:
        journal = tmp_path / f"{unit_name}.journal.log"
        journal.write_text("bounded journal evidence\n", encoding="utf-8")
        # MEASURED (node-27 launch 8): the replay supervisor is the process that
        # captures this preflight, so it is legitimately activating with a live
        # MainPID; the other four governed units are quiescent.
        is_replay = unit_name == replay_unit
        preflight["units"][unit_name] = {
            "enabled": "enabled" if unit_name.endswith(".timer") else "static",
            "active": "activating" if is_replay else "inactive",
            "sub": "start" if is_replay else "dead",
            "result": "success",
            "main_pid": 4137040 if is_replay else 0,
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
                *_recovery_target_argv_tail(),
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
            "argv": [
                sys.executable,
                evidence.EXPECTED_CAPTURE_SCRIPT,
                "--kind",
                kind,
                "--mutation-head-sha",
                HEAD,
                # Bound RELATIONALLY, exactly as the verifier derives it: every capture
                # `output_path` in this template lives directly under `tmp_path`, so the
                # sibling the gate computes is `tmp_path/capture-artifacts` for all twelve
                # kinds.  Derived, never hardcoded -- without this binding every negative
                # below would be refused for a missing `--evidence-dir` instead of the one
                # field it corrupts.
                "--evidence-dir",
                str(tmp_path / "capture-artifacts"),
                *_pinned_capture_options("nhms"),
            ],
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
        "schema_dump_list": ("2026-07-15T11:19:06.100000Z", "2026-07-15T11:19:06.200000Z"),
        "catalog_before": ("2026-07-15T11:19:06.300000Z", "2026-07-15T11:19:06.400000Z"),
        "catalog_after_first": ("2026-07-15T11:31:00.300000Z", "2026-07-15T11:31:00.400000Z"),
        "catalog_after_second": ("2026-07-15T11:32:00.300000Z", "2026-07-15T11:32:00.400000Z"),
        "recovery_preflight": ("2026-07-15T11:40:58Z", "2026-07-15T11:40:58.500000Z"),
        "preflight_evidence": ("2026-07-15T11:50:00.100000Z", "2026-07-15T11:50:00.200000Z"),
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


# --- G10: committed bundle-author round-trip ----------------------------------------
# `build_bundle` is the committed assembler that replaces the hand-assembled
# "ten-step procedure".  `_bundle(tmp_path)` lays a complete supervisor replay
# work directory on disk (run-plan.json, supervisor-ledger.jsonl, and every
# capture/child artifact) and returns the hand-assembled reference bundle; the
# author must reconstruct a verifier-ACCEPTED bundle purely by reading that
# work directory's ledger + artifacts.
_TOP_LEVEL_BUNDLE_KEYS = {
    "schema_version",
    "issue",
    "generated_at",
    "node",
    "mutation_head_sha",
    "verifier_head_sha",
    "database_identity",
    "authorization",
    "execution",
    "recovery",
    "preflight",
    "migration",
    "selection",
    "receipts",
    "sizes",
    "catalog",
    "benchmarks",
    "cleanup",
    "out_of_scope",
}


def _author_bundle_from_workdir(tmp_path: Path, reference: dict[str, Any]) -> dict[str, Any]:
    return bundle_author.build_bundle(
        work_dir=tmp_path,
        repo_path=evidence.REPO_ROOT,
        run_plan_path=tmp_path / "run-plan.json",
        ledger_path=tmp_path / "supervisor-ledger.jsonl",
        schema_dump_path=reference["preflight"]["schema_dump"]["path"],
        mutation_head_sha=HEAD,
        verifier_head_sha=VERIFIER_HEAD,
        generated_at="2026-07-15T12:00:00Z",
    )


def _iter_refs(value: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            if set(current) == {"path", "sha256", "bytes"}:
                refs.append(dict(current))
            else:
                stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return refs


def test_bundle_author_reconstructs_a_verifier_accepted_bundle(tmp_path: Path) -> None:
    """The committed author, fed a real work dir, yields a PASS terminal."""
    reference = _bundle(tmp_path)
    built = _author_bundle_from_workdir(tmp_path, reference)

    terminal = evidence.verify_bundle(built, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    assert terminal["qualifies_task_4_5"] is True
    assert terminal["verdict"] == evidence.PASS_VERDICT


def test_bundle_author_emits_exact_top_level_keys_and_well_formed_refs(tmp_path: Path) -> None:
    """Every top-level key is present and every artifact ref is a {path,sha256,bytes} triple."""
    reference = _bundle(tmp_path)
    built = _author_bundle_from_workdir(tmp_path, reference)

    assert set(built) == _TOP_LEVEL_BUNDLE_KEYS
    assert built["schema_version"] == evidence.SCHEMA_VERSION
    assert built["issue"] == evidence.ISSUE
    assert built["node"] == "node-27"
    assert built["mutation_head_sha"] == HEAD
    assert built["verifier_head_sha"] == VERIFIER_HEAD
    # The author sources the authorization envelope from the module constants.
    assert built["authorization"]["reviewed_mutation_sha"] == HEAD
    assert built["authorization"]["max_selected_bytes"] == evidence.MAX_SELECTED_BYTES

    refs = _iter_refs(built)
    assert refs, "bundle must carry artifact references"
    import re as _re

    for ref in refs:
        assert set(ref) == {"path", "sha256", "bytes"}
        assert Path(ref["path"]).is_absolute()
        assert _re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]) is not None
        assert isinstance(ref["bytes"], int) and not isinstance(ref["bytes"], bool)
        assert ref["bytes"] > 0
        # The digest and size are recomputed from the exact on-disk bytes.
        raw = Path(ref["path"]).read_bytes()
        assert ref["sha256"] == hashlib.sha256(raw).hexdigest()
        assert ref["bytes"] == len(raw)


def test_bundle_author_mutation_wrong_file_is_rejected_by_the_verifier(tmp_path: Path) -> None:
    """Swapping one artifact ref onto the wrong file must fail the verifier (mutation guard)."""
    reference = _bundle(tmp_path)
    built = _author_bundle_from_workdir(tmp_path, reference)
    # Point catalog_after_first at the pre-migration catalog_before artifact.
    built["migration"]["catalog_after_first"] = dict(built["preflight"]["catalog_before"])
    with pytest.raises(evidence.EvidenceError):
        evidence.verify_bundle(built, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


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


def test_decompress_argv_derived_from_the_recovery_target_is_accepted(tmp_path: Path) -> None:
    """An argv whose tail comes from the verifier's own bound constant passes (#1244)."""

    receipt_path = str(tmp_path / "recovery-receipt.json")
    evidence._validate_exact_command_argv(
        _decompress_argv(receipt_path),
        kind="decompress",
        associations={"recovery_receipt": receipt_path},
        label="run plan command[2]",
    )


@pytest.mark.parametrize("field", list(_RECOVERY_TARGET_OPTIONS))
def test_decompress_argv_rejects_any_single_recovery_target_field_deviation(tmp_path: Path, field: str) -> None:
    """Every one of the six fields is load-bearing in the expected tail (#1244)."""

    receipt_path = str(tmp_path / "recovery-receipt.json")
    argv = _decompress_argv(receipt_path, **{field: f"{evidence.RECOVERY_TARGET[field]}-drift"})
    assert len(argv) == 20
    with pytest.raises(evidence.EvidenceError, match="decompress argv differs"):
        evidence._validate_exact_command_argv(
            argv,
            kind="decompress",
            associations={"recovery_receipt": receipt_path},
            label="run plan command[2]",
        )


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


def test_preflight_rejects_quiescent_replay_supervisor(tmp_path: Path) -> None:
    # The replay supervisor captures this preflight from INSIDE its own running
    # process; a quiescent/dead/zero-pid replay unit means the active owner is
    # missing, so the verifier must fail closed.
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["preflight"]["evidence"])
    replay = preflight["units"]["nhms-node27-timeseries-compression-replay.service"]
    replay["active"] = "inactive"
    replay["sub"] = "dead"
    replay["main_pid"] = 0
    bundle["preflight"]["evidence"] = _json_ref(tmp_path, "preflight-quiescent-replay.json", preflight)
    with pytest.raises(evidence.EvidenceError, match="replay supervisor unit is not the active owner"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize(
    "unit_name",
    ["nhms-node27-autopipe.service", "nhms-node27-timeseries-compression.service"],
)
def test_preflight_rejects_nonquiescent_governed_service(tmp_path: Path, unit_name: str) -> None:
    # The non-replay governed .service units must be quiescent (MainPID 0) at
    # preflight; a live PID means a competing writer.
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["preflight"]["evidence"])
    preflight["units"][unit_name]["main_pid"] = 9999
    bundle["preflight"]["evidence"] = _json_ref(tmp_path, "preflight-nonquiescent-service.json", preflight)
    with pytest.raises(evidence.EvidenceError, match="is not quiescent"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize(
    ("unit_name", "field", "value"),
    [
        ("nhms-node27-timeseries-compression.timer", "active", "active"),
        ("nhms-node27-timeseries-compression.timer", "main_pid", 4242),
        ("nhms-node27-timeseries-compression.service", "active", "active"),
    ],
)
def test_preflight_rejects_active_compression_unit(
    tmp_path: Path, unit_name: str, field: str, value: Any
) -> None:
    # The recurring compression timer/service must remain inactive with no PID.
    bundle = _bundle(tmp_path)
    preflight = _read_ref(bundle["preflight"]["evidence"])
    preflight["units"][unit_name][field] = value
    bundle["preflight"]["evidence"] = _json_ref(tmp_path, "preflight-active-compression.json", preflight)
    with pytest.raises(evidence.EvidenceError, match="must remain inactive"):
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


def test_evidence_schema_requires_backend_type_on_activity_sessions() -> None:
    # Locks the schema contract (G9 item F + G14): the activity_session $def
    # must require backend_type, so a session missing it fails the canonical
    # schema even though additionalProperties otherwise governs the shape.
    # G14 additionally requires ``usename`` and ``has_write_privilege_on_target``
    # so the two-factor client-writer judgment can never regress to a "backend
    # type alone" fail-open.  Dropping any of the three from the required list
    # (or the properties block) makes this test RED.
    session_def = EVIDENCE_SCHEMA["$defs"]["activity_session"]
    assert "backend_type" in session_def["required"]
    assert "usename" in session_def["required"]
    assert "has_write_privilege_on_target" in session_def["required"]
    assert session_def["properties"]["has_write_privilege_on_target"]["type"] == "boolean"
    validator = jsonschema.Draft7Validator(session_def, format_checker=jsonschema.FormatChecker())
    complete = {
        "pid": 1135,
        "backend_start": "2026-07-15T12:00:00Z",
        "xact_start": None,
        "query_start": "2026-07-15T12:00:01Z",
        "state": "active",
        "wait_event_type": None,
        "backend_type": "autovacuum worker",
        "usename": None,
        "has_write_privilege_on_target": False,
        "query_signature": "a" * 32,
    }
    validator.validate(complete)
    for missing in ("backend_type", "usename", "has_write_privilege_on_target"):
        assert not validator.is_valid({key: value for key, value in complete.items() if key != missing})


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
    activity["sessions"] = [
        _checkpoint_session(
            evidence.CLIENT_BACKEND_TYPE,
            usename="nhms",
            has_write_privilege_on_target=True,
        )
    ]
    checkpoint["database_activity"] = _observed(_json_ref(tmp_path, "conflicting-session.json", activity))
    ledger = tmp_path / "conflicting-session-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(item) for item in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)
    with pytest.raises(evidence.EvidenceError, match="conflicting writer"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_checkpoint_verifier_tolerates_an_autovacuum_worker_session(tmp_path: Path) -> None:
    # G9 verifier twin: the launch-7 aborter -- an autovacuum worker vacuuming
    # the chunk the mutation just created -- must now pass the checkpoint.
    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    checkpoint = next(item for item in events if item["event_type"] == "checkpoint")
    activity = _read_ref(checkpoint["database_activity"]["artifact"])
    activity["sessions"] = [_checkpoint_session("autovacuum worker", pid=1135)]
    checkpoint["database_activity"] = _observed(_json_ref(tmp_path, "autovacuum-session.json", activity))
    ledger = tmp_path / "autovacuum-session-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(item) for item in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)
    assert (
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)["preflight"][
            "quiescent"
        ]
        is True
    )


def test_checkpoint_verifier_rejects_a_session_missing_backend_type(tmp_path: Path) -> None:
    # Fail-closed shape gate: a session missing backend_type cannot be silently
    # tolerated by the client-only judgment.
    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    checkpoint = next(item for item in events if item["event_type"] == "checkpoint")
    activity = _read_ref(checkpoint["database_activity"]["artifact"])
    activity["sessions"] = [{"pid": 999, "state": "active", "wait_event_type": None}]
    checkpoint["database_activity"] = _observed(_json_ref(tmp_path, "shapeless-session.json", activity))
    ledger = tmp_path / "shapeless-session-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(item) for item in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)
    with pytest.raises(evidence.EvidenceError, match="keys differ"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_benchmark_verifier_tolerates_transient_background_worker(tmp_path: Path) -> None:
    # G9 benchmark twin: an autovacuum worker appearing in only some activity
    # stages is not session-identity drift; the full lists stay persisted. This
    # re-points the produced-artifact association so the tolerance is exercised
    # on the SUCCESS path, not short-circuited by an ownership guard.
    bundle = _bundle(tmp_path)
    document = _read_ref(bundle["benchmarks"]["evidence"])
    samples = document["queries"][0]["before"]["activity_samples"]
    for index in (1, 3):
        samples[index]["sessions"] = [_benchmark_session("autovacuum worker")]
    ref = _json_ref(tmp_path, "benchmark-transient-worker.json", document)
    bundle["benchmarks"]["evidence"] = ref
    _replace_produced_artifact(bundle, "benchmark_after", "benchmarks", ref, tmp_path)
    terminal = evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    assert [query["name"] for query in terminal["benchmarks"]["queries"]] == ["curve", "mvt"]


def test_benchmark_verifier_rejects_client_backend_drift(tmp_path: Path) -> None:
    # A client backend that has write access to the compression target
    # appearing in only some stages IS session-identity drift (G9 + G14).
    def mutate(document: dict[str, Any]) -> None:
        samples = document["queries"][0]["before"]["activity_samples"]
        for index in (1, 3):
            samples[index]["sessions"] = [
                _benchmark_session(
                    evidence.CLIENT_BACKEND_TYPE,
                    usename="nhms",
                    has_write_privilege_on_target=True,
                )
            ]

    bundle = _mutated_benchmark_bundle(tmp_path, mutate)
    with pytest.raises(evidence.EvidenceError, match="session-identity drift"):
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_benchmark_verifier_tolerates_readonly_display_client_backend(tmp_path: Path) -> None:
    # G14 (launch 11): the display API's nhms_display_ro pool renders as a
    # 'client backend' too, but it holds no writes on the compression target,
    # so its arrival/departure across stages is NOT session-identity drift.
    bundle = _bundle(tmp_path)
    document = _read_ref(bundle["benchmarks"]["evidence"])
    samples = document["queries"][0]["before"]["activity_samples"]
    for index in (1, 3):
        samples[index]["sessions"] = [
            _benchmark_session(
                evidence.CLIENT_BACKEND_TYPE,
                pid=125,
                usename="nhms_display_ro",
                has_write_privilege_on_target=False,
            )
        ]
    ref = _json_ref(tmp_path, "benchmark-readonly-display.json", document)
    bundle["benchmarks"]["evidence"] = ref
    _replace_produced_artifact(bundle, "benchmark_after", "benchmarks", ref, tmp_path)
    terminal = evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    assert [query["name"] for query in terminal["benchmarks"]["queries"]] == ["curve", "mvt"]


def test_checkpoint_verifier_tolerates_a_readonly_display_client_backend(tmp_path: Path) -> None:
    # G14 verifier twin: the checkpoint-side judgment must ignore the display
    # API's readonly client-backend session for the same reason.
    bundle = _bundle(tmp_path)
    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    checkpoint = next(item for item in events if item["event_type"] == "checkpoint")
    activity = _read_ref(checkpoint["database_activity"]["artifact"])
    activity["sessions"] = [
        _checkpoint_session(
            evidence.CLIENT_BACKEND_TYPE,
            pid=125,
            usename="nhms_display_ro",
            has_write_privilege_on_target=False,
        )
    ]
    checkpoint["database_activity"] = _observed(_json_ref(tmp_path, "readonly-display-session.json", activity))
    ledger = tmp_path / "readonly-display-ledger.jsonl"
    ledger.write_bytes(b"".join(_canonical(item) for item in events))
    bundle["execution"]["ledger"] = _file_ref(ledger)
    assert (
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)["preflight"][
            "quiescent"
        ]
        is True
    )


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


# --------------------------------------------------------------------------- #
# Issue #1255 -- the recurring-unit gate judges current activity, not boot
# history, and the boot-history fields it stopped gating on stay pinned as
# evidence.
# --------------------------------------------------------------------------- #

# MEASURED on node-27 2026-08-14 (read-only `systemctl --user show`, the day
# after the daily 04:25 UTC timer tick): a unit that RAN THIS BOOT and returned
# to idle keeps a non-empty InvocationID and real start timestamps while every
# activity/identity field reads canonically idle.  `_bundle` still emits the
# never-started rendering ("n/a" + empty InvocationID), so both measured forms
# are exercised.
RECURRING_RAN_THIS_BOOT = {
    "FragmentPath": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.service",
    "ActiveState": "inactive",
    "SubState": "dead",
    "MainPID": 0,
    "InvocationID": "0d8bd46e8f634e0296d8cbf49a938231",
    "ExecMainStartTimestamp": "Thu 2026-08-13 12:25:00 CST",
    "ExecMainStartTimestampMonotonic": 1306766054421,
}


def _rewrite_recurring_show(
    bundle: dict[str, Any],
    tmp_path: Path,
    recurring: dict[str, Any],
    *,
    serial: str,
    first_only: bool = False,
) -> None:
    """Re-render the recurring half of every (or the first) checkpoint's show doc."""

    events = [json.loads(line) for line in Path(bundle["execution"]["ledger"]["path"]).read_text().splitlines()]
    checkpoints = [event for event in events if event["event_type"] == "checkpoint"]
    for index, event in enumerate(checkpoints[:1] if first_only else checkpoints):
        show = _read_ref(event["systemd_show"]["artifact"])
        show["recurring"] = recurring
        _rewrite_checkpoint_json(tmp_path, event, "systemd_show", show, f"{serial}-{index}")
    _replace_execution_events(bundle, events, tmp_path / f"{serial}-ledger.jsonl")


def test_recurring_unit_gate_accepts_the_unit_that_already_ran_this_boot(tmp_path: Path) -> None:
    # #1255 core: the deployed timer makes "never started this boot" permanently
    # false, so the pre-fix whole-dict equality rejected the steady state itself.
    bundle = _bundle(tmp_path)
    _rewrite_recurring_show(bundle, tmp_path, RECURRING_RAN_THIS_BOOT, serial="ran-this-boot")
    evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


def test_recurring_unit_gate_still_accepts_the_never_started_form(tmp_path: Path) -> None:
    # The narrower predicate must not have traded one over-fit for another: a
    # bundle regenerated on a host where the unit never started this boot ("n/a"
    # + empty InvocationID) still verifies.
    bundle = _bundle(tmp_path)
    never_started = {
        **RECURRING_RAN_THIS_BOOT,
        "InvocationID": "",
        "ExecMainStartTimestamp": evidence.SYSTEMD_UNSET_TIMESTAMP,
        "ExecMainStartTimestampMonotonic": 0,
    }
    _rewrite_recurring_show(bundle, tmp_path, never_started, serial="never-started")
    evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)


@pytest.mark.parametrize(
    ("field", "value", "observed"),
    [
        ("ActiveState", "activating", "'activating'"),
        ("SubState", "failed", "'failed'"),
        ("MainPID", 4242, "4242"),
        # JSON `true`/`false` for MainPID: Python's bool/int equality would let
        # `False == 0` pass a value-only check, so the predicate compares types
        # as strictly as values.  LIVE-PLANE ONLY by construction -- the
        # supervisor reads MainPID out of real `systemctl show` stdout through
        # `int()`, so no bool can reach its gate, while a hand-edited evidence
        # document can carry one.
        ("MainPID", False, "False"),
        (
            "FragmentPath",
            "/etc/systemd/user/nhms-node27-timeseries-compression.service",
            "'/etc/systemd/user/nhms-node27-timeseries-compression.service'",
        ),
    ],
)
def test_recurring_unit_gate_rejects_one_deviating_field_and_names_it(
    tmp_path: Path, field: str, value: Any, observed: str
) -> None:
    # One gated field deviating ALONE on top of the accepted ran-this-boot base:
    # deleting any single field's check leaves its own parameter red.
    bundle = _bundle(tmp_path)
    _rewrite_recurring_show(
        bundle,
        tmp_path,
        {**RECURRING_RAN_THIS_BOOT, field: value},
        serial=f"deviating-{field}",
        first_only=True,
    )
    with pytest.raises(evidence.EvidenceError) as raised:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(raised.value)
    assert f"{field}={observed}" in message
    if field == "SubState":
        # A unit left failed by the per-chunk timeout wall (runbook §4.5) is
        # neither concurrent compression nor an identity swap.
        assert "not running" in message
        assert "systemctl --user reset-failed nhms-node27-timeseries-compression.service" in message
    else:
        assert "shows current activity or unexpected identity" in message


def test_recurring_failed_sentence_is_not_claimed_over_a_polluted_document(tmp_path: Path) -> None:
    # `SubState == "failed"` alone must not buy the "not running" headline plus
    # the reset-failed remedy: this document names ANOTHER unit's fragment, is
    # ActiveState=active and carries a live MainPID.  "Not running" would be a
    # lie about it and `reset-failed` on the pinned name the wrong remedy, so
    # the generic current-activity/identity sentence is the honest one.
    bundle = _bundle(tmp_path)
    _rewrite_recurring_show(
        bundle,
        tmp_path,
        {
            **RECURRING_RAN_THIS_BOOT,
            "FragmentPath": "/etc/systemd/user/nhms-node27-timeseries-compression.service",
            "ActiveState": "active",
            "SubState": "failed",
            "MainPID": 4242,
        },
        serial="polluted-failed",
        first_only=True,
    )
    with pytest.raises(evidence.EvidenceError) as raised:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(raised.value)
    assert "shows current activity or unexpected identity" in message
    assert "MainPID=4242" in message
    assert "not running" not in message
    assert "reset-failed" not in message


def test_recurring_failed_sentence_covers_the_real_wall_trip_geometry(tmp_path: Path) -> None:
    # The converse of the pollution guard above, and the shape systemd really
    # leaves after the per-chunk timeout wall (runbook §4.5): `ActiveState` and
    # `SubState` BOTH `failed`, MainPID cleared, the pinned fragment intact.
    # The single-field parametrization can only reach `SubState=failed` over an
    # `inactive` base, so this is the only guard keeping the remedy sentence
    # attached to the geometry the operator will actually meet.
    bundle = _bundle(tmp_path)
    _rewrite_recurring_show(
        bundle,
        tmp_path,
        {**RECURRING_RAN_THIS_BOOT, "ActiveState": "failed", "SubState": "failed"},
        serial="wall-tripped-failed",
        first_only=True,
    )
    with pytest.raises(evidence.EvidenceError) as raised:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(raised.value)
    assert "ActiveState='failed'" in message
    assert "SubState='failed'" in message
    assert "not running" in message
    assert "systemctl --user reset-failed nhms-node27-timeseries-compression.service" in message


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("drop-invocation-id", "keys differ"),
        ("drop-timestamp", "keys differ"),
        ("drop-monotonic", "keys differ"),
        ("extra-field", "keys differ"),
        ("null-invocation-id", "boot-history evidence fields are malformed"),
        ("numeric-timestamp", "boot-history evidence fields are malformed"),
        ("string-monotonic", "boot-history evidence fields are malformed"),
        ("boolean-monotonic", "boot-history evidence fields are malformed"),
    ],
)
def test_recurring_boot_history_evidence_is_pinned_even_though_it_never_gates(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    # Demoting the boot-history fields out of the gate must not demote them out
    # of the evidence: dropping or mistyping any of them is malformed evidence,
    # not a passing checkpoint.  (The whole-dict equality was the only key-set
    # pin before #1255; this is its explicit replacement.)
    recurring = dict(RECURRING_RAN_THIS_BOOT)
    if mutation == "drop-invocation-id":
        recurring.pop("InvocationID")
    elif mutation == "drop-timestamp":
        recurring.pop("ExecMainStartTimestamp")
    elif mutation == "drop-monotonic":
        recurring.pop("ExecMainStartTimestampMonotonic")
    elif mutation == "extra-field":
        recurring["LoadState"] = "loaded"
    elif mutation == "null-invocation-id":
        recurring["InvocationID"] = None
    elif mutation == "numeric-timestamp":
        recurring["ExecMainStartTimestamp"] = 1306766054421
    elif mutation == "string-monotonic":
        recurring["ExecMainStartTimestampMonotonic"] = "1306766054421"
    else:
        recurring["ExecMainStartTimestampMonotonic"] = True
    bundle = _bundle(tmp_path)
    _rewrite_recurring_show(bundle, tmp_path, recurring, serial=mutation, first_only=True)
    with pytest.raises(evidence.EvidenceError, match=expected):
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


# --------------------------------------------------------------------------- #
# Issue #1069 -- MERGED hermetic end-to-end lock.
#
# This is the union of the two halves that previously stopped short of each
# other: the capture suite's `test_authored_plan_survives_the_real_state_machine
# _and_verifier_validators` drives the REAL supervisor state machine + REAL
# capture_checkpoint against stubs but never runs `verify_bundle`; the G10
# round-trip runs the REAL `verify_bundle` but over a hand-shaped synthetic work
# dir, never the real state machine.  The test below merges them: the REAL
# `execute_producer_state_machine` + REAL `capture_checkpoint` (post-G12 order)
# generate the work dir through stubbed producers whose outputs are shaped to the
# G10 verifier contract, the REAL `build_bundle` assembles the bundle, and the
# REAL `verify_bundle` returns a task-4.5 PASS -- proving the whole live-evidence
# lane works from committed code before it is ever run on node-27.
# --------------------------------------------------------------------------- #
from datetime import timedelta as _timedelta  # noqa: E402

from tests import test_node27_timeseries_compression_capture as _capture_harness  # noqa: E402
from tests import test_node27_timeseries_compression_supervisor as _sup  # noqa: E402

# The self-test data-volume headroom handed to the capture producer through the
# `--self-test-free-bytes` seam (>= the 300 GiB `MIN_FREE_BYTES` floor), so the
# selection snapshots verify on a small-disk CI runner rather than false-RED on
# the runner's real statvfs.
_SELFTEST_FREE_BYTES = 500_000_000_000
_IMAGE_SHA = "sha256:" + "a" * 64
_BINARY_SHA = "b" * 64
_VERSION_BYTES = b"pg_restore (PostgreSQL) 15.2\n"
_ENTRIES_BYTES = b"TABLE hydro river_timeseries\nTABLE met forcing_station_timeseries\n"
_DUMP_BYTES = b"PGDMP\x00hermetic self-test forensic schema\n"

# A stateful psql stub: identical to the supervisor stub template except that a
# response may carry a `sequence` of stdouts consumed in call order (a per-marker
# on-disk counter).  The two preflight-family captures (recovery, then
# compression) each issue the same `capture:preflight` / `capture:preflight_probe`
# marker, but the verifier requires their `captured_at`s to be distinct and
# ordered, so those two markers return an ordered pair.
_SELFTEST_PSQL_TEMPLATE = """#!{python}
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_name = os.path.basename(__file__)
with open(os.path.join(_here, _name + ".responses.json"), encoding="utf-8") as _fh:
    _responses = json.load(_fh)
_argv = " ".join(sys.argv[1:])
for _index, _response in enumerate(_responses):
    if all(_token in _argv for _token in _response["match"]):
        if "sequence" in _response:
            _counter = os.path.join(_here, _name + ".seq%d.count" % _index)
            try:
                with open(_counter, encoding="utf-8") as _cf:
                    _n = int(_cf.read().strip() or "0")
            except OSError:
                _n = 0
            with open(_counter, "w", encoding="utf-8") as _cf:
                _cf.write(str(_n + 1))
            _seq = _response["sequence"]
            _out = _seq[_n if _n < len(_seq) else len(_seq) - 1]
        else:
            _out = _response.get("stdout", "")
        sys.stdout.write(_out)
        sys.stderr.write(_response.get("stderr", ""))
        sys.exit(_response.get("exit", 0))
sys.stderr.write("no stub response for argv: " + _argv + "\\n")
sys.exit(97)
"""


def _e2e_fmt(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _e2e_write_stub(bindir: Path, name: str, responses: list[dict[str, Any]], *, template: str | None = None) -> str:
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / name
    script.write_text((template or _sup._STUB_TEMPLATE).replace("{python}", sys.executable), encoding="utf-8")
    script.chmod(0o755)
    (bindir / f"{name}.responses.json").write_text(json.dumps(responses), encoding="utf-8")
    return str(script)


def _e2e_unit_show(enabled: str, active: str, sub: str, pid: int) -> str:
    return f"UnitFileState={enabled}\nActiveState={active}\nSubState={sub}\nResult=success\nMainPID={pid}\n"


_PROBE_QUERY = (
    "SELECT current_database() AS dbname, "
    "current_setting('server_version') AS postgres_version, "
    "extversion AS timescaledb_version FROM pg_extension "
    "WHERE extname = 'timescaledb'"
)
_DB_IDENTITY = {
    "dbname": "nhms",
    "instance": "node27-primary-pg15",
    "postgres_version": "15.2",
    "timescaledb_version": "2.10.2",
}
_ROLE = {
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


def _e2e_build_timeline() -> tuple[list[datetime], dict[str, datetime], _timedelta]:
    """Deterministic, compressed 2026-07-15 timeline for the 55 ``_utc_now`` calls.

    The supervisor state machine calls ``_utc_now`` in a fixed order: one per
    checkpoint, two (start/finish) per child, two per capture.  Child wall deltas
    must be within 0.5s of the real monotonic elapsed (verifier isclose gate), so
    every child/capture start->finish is a small 0.3s window while distinct
    operations are 2s apart.  Every document timestamp is derived from the START
    of its producing operation, which -- because execution order equals the
    verifier's global chronology order -- makes the whole chain strictly
    increasing by construction.
    """

    ops = [
        ("checkpoint", "preflight"),
        ("child", "pg_dump"),
        ("child", "pg_restore_version"),
        ("child", "pg_restore_list"),
        ("capture", "schema_dump_list"),
        ("capture", "catalog_before"),
        ("checkpoint", "before-migration-1"),
        ("child", "migration-1"),
        ("checkpoint", "after-migration-1"),
        ("capture", "catalog_after_first"),
        ("checkpoint", "before-migration-2"),
        ("child", "migration-2"),
        ("checkpoint", "after-migration-2"),
        ("capture", "catalog_after_second"),
        ("capture", "recovery_preflight"),
        ("checkpoint", "before-decompress"),
        ("child", "decompress"),
        ("checkpoint", "after-decompress"),
        ("capture", "preflight_evidence"),
        ("child", "dry-run"),
        ("capture", "post_dry_selection"),
        ("child", "benchmark_before"),
        ("capture", "pre_enforce_selection"),
        ("capture", "sizes_pre"),
        ("checkpoint", "before-enforce"),
        ("child", "enforce"),
        ("checkpoint", "after-enforce"),
        ("capture", "sizes_post"),
        ("capture", "catalog_post"),
        ("child", "benchmark_after"),
        ("checkpoint", "postflight"),
        ("capture", "cleanup"),
        ("checkpoint", "cleanup-final"),
    ]
    base = datetime(2026, 7, 15, 11, 0, 0, tzinfo=UTC)
    step = _timedelta(seconds=2)
    child = _timedelta(seconds=0.3)
    seq: list[datetime] = []
    starts: dict[str, datetime] = {}
    cursor = base
    for kind, name in ops:
        starts[name] = cursor
        if kind == "checkpoint":
            seq.append(cursor)
        else:
            seq.append(cursor)
            seq.append(cursor + child)
        cursor = cursor + step
    return seq, starts, child


def _e2e_capture_psql_responses(starts: dict[str, datetime], child: _timedelta) -> list[dict[str, Any]]:
    d3 = _sup._d3_catalog()

    def preflight_body(captured: datetime) -> str:
        return json.dumps({"captured_at": _e2e_fmt(captured), "database_identity": _DB_IDENTITY}) + "\n"

    def probe_body(captured: datetime) -> str:
        return (
            json.dumps({"captured_at": _e2e_fmt(captured), "query": _PROBE_QUERY, "row": _DB_IDENTITY}) + "\n"
        )

    def catalog_body(name: str) -> str:
        return json.dumps({"captured_at": _e2e_fmt(starts[name]), "catalog": d3}) + "\n"

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

    def selection_body(name: str) -> str:
        observed = starts[name]
        cutoff = observed - _timedelta(seconds=evidence.EXPECTED_LAG_SECONDS)
        return (
            json.dumps(
                {
                    "observed_at": _e2e_fmt(observed),
                    "cutoff": _e2e_fmt(cutoff),
                    "candidates": [candidate, deferred],
                }
            )
            + "\n"
        )

    def sizes_body(name: str, *, post: bool) -> str:
        return json.dumps({"captured_at": _e2e_fmt(starts[name]), "tables": _sizes(post=post)["tables"]}) + "\n"

    cleanup_captured = starts["cleanup"]
    cleanup_body = {
        "captured_at": _e2e_fmt(cleanup_captured),
        "window_started_at": _e2e_fmt(starts["recovery_preflight"] + _timedelta(seconds=1)),
        "window_finished_at": _e2e_fmt(cleanup_captured - _timedelta(seconds=0.5)),
    }
    catalog_post_body = {
        "captured_at": _e2e_fmt(starts["catalog_post"]),
        "catalog": d3,
        "compressed_chunk_identities": [dict(IDENTITY)],
    }
    return [
        {"match": ["capture:now"], "stdout": json.dumps(_e2e_fmt(starts["schema_dump_list"])) + "\n"},
        # The `*_probe` marker is a superset of the `capture:preflight` prefix, so
        # it must precede it in the match order.  Both return an ordered pair
        # consumed recovery-first, then compression-preflight.
        {
            "match": ["capture:preflight_probe"],
            "sequence": [
                probe_body(starts["recovery_preflight"] - _timedelta(seconds=1)),
                probe_body(starts["preflight_evidence"] - _timedelta(seconds=1)),
            ],
        },
        {
            "match": ["capture:preflight"],
            "sequence": [
                preflight_body(starts["recovery_preflight"]),
                preflight_body(starts["preflight_evidence"]),
            ],
        },
        {"match": ["capture:role"], "stdout": json.dumps(_ROLE) + "\n"},
        {
            "match": ["capture:quiescence"],
            "stdout": json.dumps({"database_writes_quiescent": True, "conflicting_locks_absent": True}) + "\n",
        },
        {
            "match": ["capture:recovery_preflight"],
            "stdout": json.dumps(
                {"free_bytes": _SELFTEST_FREE_BYTES, "before_compressed": True, "before_row_count": 12_345_678}
            )
            + "\n",
        },
        {"match": ["capture:catalog_before"], "stdout": catalog_body("catalog_before")},
        {"match": ["capture:catalog_after_first"], "stdout": catalog_body("catalog_after_first")},
        {"match": ["capture:catalog_after_second"], "stdout": catalog_body("catalog_after_second")},
        {"match": ["capture:catalog_post"], "stdout": json.dumps(catalog_post_body) + "\n"},
        {"match": ["capture:post_dry_selection"], "stdout": selection_body("post_dry_selection")},
        {"match": ["capture:pre_enforce_selection"], "stdout": selection_body("pre_enforce_selection")},
        {"match": ["capture:sizes_pre"], "stdout": sizes_body("sizes_pre", post=False)},
        {"match": ["capture:sizes_post"], "stdout": sizes_body("sizes_post", post=True)},
        {"match": ["capture:cleanup_window"], "stdout": json.dumps(cleanup_body) + "\n"},
    ]


def _e2e_capture_bin(bindir: Path, starts: dict[str, datetime], child: _timedelta) -> None:
    _e2e_write_stub(bindir, "psql", _e2e_capture_psql_responses(starts, child), template=_SELFTEST_PSQL_TEMPLATE)
    systemctl = []
    for unit, (enabled, active, sub, pid) in {
        "nhms-node27-autopipe.timer": ("enabled", "active", "waiting", 0),
        "nhms-node27-autopipe.service": ("static", "inactive", "dead", 0),
        "nhms-node27-timeseries-compression.timer": ("enabled", "inactive", "dead", 0),
        "nhms-node27-timeseries-compression.service": ("static", "inactive", "dead", 0),
        "nhms-node27-timeseries-compression-replay.service": ("static", "activating", "start", 4137040),
    }.items():
        systemctl.append({"match": ["UnitFileState", unit], "stdout": _e2e_unit_show(enabled, active, sub, pid)})
    _e2e_write_stub(bindir, "systemctl", systemctl)
    _e2e_write_stub(bindir, "journalctl", [{"match": ["--user"], "stdout": "-- boot --\njournal line\n"}])
    realpath = evidence.CONTAINER_PG_RESTORE_REALPATH
    _e2e_write_stub(
        bindir,
        "docker",
        [
            {
                "match": ["inspect", ".State.Running"],
                "stdout": "/nhms-db\tcontainer-123\ttimescale/timescaledb:2.10.2-pg15\trunning\ttrue\n",
            },
            {"match": ["inspect", ".Image"], "stdout": _IMAGE_SHA + "\n"},
            {"match": ["exec", "--version"], "stdout": _VERSION_BYTES.decode()},
            {"match": ["exec", "--list"], "stdout": _ENTRIES_BYTES.decode()},
            {"match": ["exec", "readlink"], "stdout": realpath + "\n"},
            {"match": ["exec", "sha256sum"], "stdout": _BINARY_SHA + "  " + realpath + "\n"},
        ],
    )
    _e2e_write_stub(
        bindir,
        "git",
        [
            {"match": ["status"], "stdout": ""},
            {"match": ["remote", "get-url"], "stdout": "https://github.com/DankerMu/SHUD-NWM.git\n"},
        ],
    )


def _e2e_bench_phase(
    name: str, samples: list[float], *, after: bool, bounds: tuple[datetime, datetime], activity: list[datetime]
) -> dict[str, Any]:
    payload: Any = "deadbeef" if name == "mvt" else [{"valid_time": "2026-05-29T00:00:00Z", "value": 1.25}]
    if name == "mvt":
        raw = bytes.fromhex(payload)
        rows = 1
    else:
        raw = _canonical(payload)
        rows = len(payload)
    stages = ["before_cold", "after_cold", "before_measurements", "mid_measurements", "after_result"]
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
            {"captured_at": _e2e_fmt(when), "stage": stage, "sessions": [], "material_load_stable": True}
            for when, stage in zip(activity, stages, strict=True)
        ],
        "execution_bounds": {
            "statement_timeout_ms": 60_000,
            "lock_timeout_ms": 5_000,
            "phase_timeout_seconds": 900,
            "started_at": _e2e_fmt(bounds[0]),
            "finished_at": _e2e_fmt(bounds[1]),
        },
    }


def _e2e_benchmarks_document(starts: dict[str, datetime]) -> dict[str, Any]:
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
    curve_source = ROOT / "packages/common/forecast_store.py"
    mvt_source = ROOT / "services/tiles/mvt.py"
    route_source = ROOT / "apps/api/routes/hydro_display.py"

    def window(base: datetime, offset: float) -> datetime:
        return base + _timedelta(seconds=offset)

    def phase_pack(base: datetime, *, after: bool, samples: list[float]) -> dict[str, Any]:
        return {
            "curve": (
                (window(base, 0.010), window(base, 0.090)),
                [window(base, off) for off in (0.020, 0.035, 0.050, 0.065, 0.085)],
            ),
            "mvt": (
                (window(base, 0.150), window(base, 0.240)),
                [window(base, off) for off in (0.160, 0.175, 0.190, 0.205, 0.235)],
            ),
        }

    before_base = starts["benchmark_before"]
    after_base = starts["benchmark_after"]
    before_pack = phase_pack(before_base, after=False, samples=[])
    after_pack = phase_pack(after_base, after=True, samples=[])
    before_samples = [10, 11, 12, 13, 14, 15, 16]
    after_samples = [12, 13, 14, 15, 16, 17, 18]
    queries = []
    for name in ("curve", "mvt"):
        queries.append(
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
                "query_sha256": hashlib.sha256((curve_query if name == "curve" else mvt_query).encode()).hexdigest(),
                "query_text": curve_query if name == "curve" else mvt_query,
                "binding": (
                    {"parameter_names": curve_names, "bound_parameters": benchmark._json_value(curve_parameters)}
                    if name == "curve"
                    else mvt_binding
                ),
                "before": _e2e_bench_phase(
                    name, before_samples, after=False, bounds=before_pack[name][0], activity=before_pack[name][1]
                ),
                "after": _e2e_bench_phase(
                    name, after_samples, after=True, bounds=after_pack[name][0], activity=after_pack[name][1]
                ),
            }
        )
    return {
        "execution_bounds": {
            "before": {
                "started_at": _e2e_fmt(window(before_base, 0.005)),
                "finished_at": _e2e_fmt(window(before_base, 0.250)),
                "wall_seconds": 900,
            },
            "after": {
                "started_at": _e2e_fmt(window(after_base, 0.005)),
                "finished_at": _e2e_fmt(window(after_base, 0.250)),
                "wall_seconds": 900,
            },
        },
        "queries": queries,
    }


def _e2e_child_argv(kind: str, association_path: str | None, dump_container: str, src_dir: Path) -> list[str]:
    def copy(src: Path, dst: str) -> list[str]:
        return [sys.executable, "-c", f"import shutil; shutil.copyfile({str(src)!r}, {dst!r})"]

    def emit(data: bytes) -> list[str]:
        return [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({data!r})"]

    if kind == "pg_dump":
        return copy(src_dir / "schema.dump", association_path)
    if kind == "pg_restore_version":
        return emit(_VERSION_BYTES)
    if kind == "pg_restore_list":
        # argv[-1] must remain the /var/lib/postgresql container dump path: the
        # state machine hands it to `resolve_container_pg_restore_identity`.
        return [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({_ENTRIES_BYTES!r})", dump_container]
    if kind == "migration_apply":
        return [sys.executable, "-c", "pass"]
    if kind == "decompress":
        return copy(src_dir / "recovery-receipt.json", association_path)
    if kind == "compression_dry_run":
        return copy(src_dir / "dry-receipt.json", association_path)
    if kind == "benchmark_before":
        return copy(src_dir / "benchmark-before.json", association_path)
    if kind == "compression_enforce":
        return copy(src_dir / "enforce-receipt.json", association_path)
    if kind == "benchmark_after":
        return copy(src_dir / "benchmarks.json", association_path)
    raise AssertionError(f"unmapped child kind {kind!r}")


# The five capture tool options the verifier pins by value, paired with the stub binary
# `_e2e_capture_bin` writes for each.  The recorded plan keeps the production `/usr/bin/*`
# values; only the EXECUTED plan variant may point at these stubs.
_E2E_CAPTURE_TOOL_STUBS = (
    ("--psql", "psql"),
    ("--systemctl", "systemctl"),
    ("--docker", "docker"),
    ("--journalctl", "journalctl"),
    ("--git", "git"),
)


def _rebind_argv_option(argv: list[str], option: str, value: str) -> list[str]:
    """Rebind one option's value by NAME, position-independent.

    `plan_author`'s `capture_common` puts the common options at a fixed offset today, but
    that layout is not a contract (the verifier deliberately scans by name rather than by
    position), so the exec-side divergence must not assume it either.  The exactly-once
    assertion is the anti-no-op guard: a silently missed rebind would leave the executed
    capture pointing at the REAL host binary.
    """

    rebound = list(argv)
    replaced = 0
    index = 0
    while index < len(rebound):
        if rebound[index] == option and index + 1 < len(rebound):
            rebound[index + 1] = value
            replaced += 1
            index += 1
        index += 1
    assert replaced == 1, (option, argv)
    return rebound


def _e2e_option_values(argv: list[str], option: str) -> list[str]:
    """Independent by-name value lookup for the exec-side fidelity pins.

    Deliberately NOT `evidence._argv_option_values`: these pins are about THIS test's own
    rewrite, so reading the argv through the very helper the gate under test uses would
    make them circular.  Membership (`str(stub) in argv`) would be worse still -- a
    partially no-op rewrite that left one option bound to the real host binary would
    still satisfy it.
    """

    return [argv[index + 1] for index, token in enumerate(argv) if token == option and index + 1 < len(argv)]


def test_real_state_machine_bundle_verifies_task_4_5_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL state machine + REAL build_bundle + REAL verify_bundle => PASS.

    Hermetic: no network, no DB, disk-size-independent (the free-bytes seam), and
    deterministic (``_utc_now`` is driven by a fixed timeline).  Node-27 exercises
    the real provenance/identity binaries; here the autouse fixtures repoint the
    narrow git-provenance seams onto a suite-owned lineage, exactly as the other
    verifier tests in this module do.
    """

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    seq, starts, child = _e2e_build_timeline()
    clock = iter(seq)
    monkeypatch.setattr(supervisor, "_utc_now", lambda: _e2e_fmt(next(clock)))

    fixture_repo = _capture_harness._fixture_repo(tmp_path)
    checkpoint_bin = tmp_path / "checkpoint-bin"
    capture_bin = tmp_path / "capture-bin"
    src_dir = tmp_path / "child-src"
    src_dir.mkdir()
    schema_dump_host = str(tmp_path / "schema-before.dump")
    schema_dump_container = "/var/lib/postgresql/evidence/schema.dump"

    # Pre-write every child-produced artifact so the child stubs merely copy it
    # into place (the supervisor observes and hashes the on-disk bytes at exit).
    (src_dir / "schema.dump").write_bytes(_DUMP_BYTES)
    dump_sha = hashlib.sha256(_DUMP_BYTES).hexdigest()

    def override_receipt(enforce: bool) -> dict[str, Any]:
        base = starts["enforce" if enforce else "dry-run"]
        receipt = _receipt(enforce=enforce)
        receipt["generated_at"] = _e2e_fmt(base + _timedelta(seconds=0.1))
        receipt["now_utc"] = _e2e_fmt(base + _timedelta(seconds=0.05))
        return receipt

    (src_dir / "dry-receipt.json").write_bytes(_canonical(override_receipt(False)))
    (src_dir / "enforce-receipt.json").write_bytes(_canonical(override_receipt(True)))
    recovery_receipt = {
        "started_at": _e2e_fmt(starts["decompress"]),
        "finished_at": _e2e_fmt(starts["decompress"] + child),
        "node": "node-27",
        "mutation_head_sha": HEAD,
        "database_identity": _DB_IDENTITY,
        "target": dict(IDENTITY),
        "exit_code": 0,
        "decompress_return_relation": evidence.RECOVERY_RETURN_RELATION,
        "after_compressed": False,
        "after_row_count": 12_345_678,
    }
    (src_dir / "recovery-receipt.json").write_bytes(_canonical(recovery_receipt))
    (src_dir / "benchmark-before.json").write_bytes(_canonical({"phase": "before"}))
    (src_dir / "benchmarks.json").write_bytes(_canonical(_e2e_benchmarks_document(starts)))

    # Checkpoint-plane stubs (supervisor-owned probes + pg_restore identity),
    # anchored to the SAME measured container identity as the capture plane.
    _e2e_write_stub(checkpoint_bin, "psql", _sup._psql_responses())
    _e2e_write_stub(checkpoint_bin, "systemctl", _sup._systemctl_responses())
    _e2e_write_stub(checkpoint_bin, "journalctl", _sup._journalctl_responses())
    _e2e_write_stub(
        checkpoint_bin,
        "docker",
        _sup._docker_responses(
            dump_path=schema_dump_container,
            image=_IMAGE_SHA,
            realpath=evidence.CONTAINER_PG_RESTORE_REALPATH,
            binary_sha=_BINARY_SHA,
            dump_sha=dump_sha,
        ),
    )
    _e2e_capture_bin(capture_bin, starts, child)
    monkeypatch.setattr(supervisor, "SUPERVISOR_BIN_DIR", checkpoint_bin)

    # plan_PROD carries the pinned PRODUCTION command AND capture argvs the verifier
    # requires: seam-free, so `run_plan_id`, the bundle's run plan and the verifier all
    # see a production plan (the verifier refuses any `--self-test-*` token in a run
    # plan capture argv outright).  `capture_script` is left at the production default
    # (`/home/nwm/NWM/scripts/...capture.py`) because the verifier now pins the capture
    # producer identity; plan_EXEC below swaps argv[1] to the in-checkout script so the
    # state machine still executes the REAL capture producer.  The same split now covers
    # the capture TOOLING: the verifier pins `--psql/--systemctl/--docker/--journalctl/
    # --git/--repo/--container` to their committed production values, so plan_prod takes
    # `plan_author`'s defaults for all of them and the stub paths / test checkout live
    # exclusively on plan_EXEC.  `schema_dump_host/container` overrides stay -- those
    # options are deliberately unpinned -- and `capture_python` stays because argv[0] is
    # unpinned by design.
    plan_prod = plan_author.build_run_plan(
        mutation_head_sha=HEAD,
        root=str(tmp_path),
        schema_dump_host=schema_dump_host,
        schema_dump_container=schema_dump_container,
        capture_python=sys.executable,
    )
    plan_prod["run_plan_id"] = supervisor.run_plan_id(plan_prod)
    supervisor.validate_run_plan(plan_prod, inherited_env={})

    # plan_EXEC shares checkpoints but swaps the command argvs for stub producers (the
    # production binaries cannot run hermetically) and appends the hermetic capture
    # seams.  Exactly the command-side pattern below: the state machine really executes
    # this argv, and the ledger identities are rewritten back to plan_prod afterwards.
    plan_exec = copy.deepcopy(plan_prod)
    for command in plan_exec["commands"]:
        associations = command["artifact_associations"]
        association_path = next(iter(associations.values())) if associations else None
        command["argv"] = _e2e_child_argv(
            str(command["kind"]), association_path, schema_dump_container, src_dir
        )
    for capture in plan_exec["captures"]:
        # The executed capture must be the real capture producer in THIS checkout; the
        # production path plan_prod claims does not exist on a hermetic runner.  The
        # supervisor's capture anchor is suffix-based precisely so this stays legal, and
        # the ledger rewrite below restores the plan_prod argv the verifier pins.
        capture["argv"] = [
            capture["argv"][0],
            str(ROOT / "scripts/node27_timeseries_compression_capture.py"),
            *capture["argv"][2:],
        ]
        # Same divergence, one level deeper: the production `/usr/bin/*` tools plan_prod
        # records cannot run hermetically, so the EXECUTED argv points at the stubs.
        # `--repo` is per kind: `cleanup` reads the committed systemd units and
        # `_validate_reviewed_file_ref` pins those repo-unit refs to the canonical
        # checkout path, so it must read THIS checkout; every other kind performs
        # env-mode/write-guard reads the fixture repo satisfies.
        argv = _rebind_argv_option(
            capture["argv"], "--repo", str(ROOT) if capture["kind"] == "cleanup" else str(fixture_repo)
        )
        for option, tool in _E2E_CAPTURE_TOOL_STUBS:
            argv = _rebind_argv_option(argv, option, str(capture_bin / tool))
        capture["argv"] = argv
        if capture["kind"] in ("post_dry_selection", "pre_enforce_selection"):
            # The CI runner's real disk headroom is uncontrollable, so the executed
            # capture gets a deterministic figure (honoured value pinned below).
            capture["argv"] = [*capture["argv"], "--self-test-free-bytes", str(_SELFTEST_FREE_BYTES)]
        if capture["kind"] == "schema_dump_list":
            # Stub-docker injection deviates from the pinned host CLI, so the executed
            # capture must opt into the RECORD/EXEC seam explicitly (production
            # `plan_author` never emits this flag, and the verifier now refuses it).
            capture["argv"] = [*capture["argv"], "--self-test-docker-seam"]

    ledger_path = tmp_path / "supervisor-ledger.jsonl"
    checkpoints_by_phase = {(str(c["phase"]), c["command_id"]): c for c in plan_exec["checkpoints"]}
    run_id = "run-1069-selftest"
    cursor = {"value": "s=stub;i=start;b=stub;m=0;t=0;x=0"}
    with supervisor.AppendOnlyLedger(
        ledger_path, run_id=run_id, run_plan_id=plan_prod["run_plan_id"], invocation_id=_sup.PROBE_INVOCATION_ID
    ) as ledger:

        def live_checkpoint(phase: str, command_id: str | None) -> None:
            cursor["value"] = supervisor.capture_checkpoint(
                checkpoints_by_phase[(phase, command_id)],
                wall=supervisor.HardWall.start(600),
                ledger=ledger,
                artifact_dir=tmp_path,
                journal_cursor=cursor["value"],
                invocation_id=_sup.PROBE_INVOCATION_ID,
            )

        supervisor.execute_producer_state_machine(
            plan_exec,
            wall=supervisor.HardWall.start(600),
            ledger=ledger,
            artifact_dir=tmp_path,
            checkpoint_runner=live_checkpoint,
            restore_identity_resolver=lambda w, dump: supervisor.resolve_container_pg_restore_identity(
                wall=w, dump_path=dump
            ),
        )

    events = [json.loads(line) for line in ledger_path.read_text().splitlines()]

    # Hermetic-fidelity pin, asserted BEFORE the rewrite below so the rewrite can never
    # be vacuous: the state machine really spawned capture.py with the seam tokens on
    # its argv (as recorded by `run_capture_step`), AND the free-bytes seam was really
    # HONOURED -- without the value pin a capture that silently stops consuming
    # `--self-test-free-bytes` would still pass on any runner with >300 GiB free.
    executed_capture_argv = {
        str(event["capture_id"]): list(event["argv"]) for event in events if event.get("event_type") == "capture"
    }
    assert "--self-test-docker-seam" in executed_capture_argv["capture-schema_dump_list"]
    for selection_kind in ("post_dry_selection", "pre_enforce_selection"):
        executed = executed_capture_argv[f"capture-{selection_kind}"]
        assert executed[-2:] == ["--self-test-free-bytes", str(_SELFTEST_FREE_BYTES)]
        snapshot_path = next(c["output_path"] for c in plan_prod["captures"] if c["kind"] == selection_kind)
        assert json.loads(Path(snapshot_path).read_bytes())["free_bytes"] == _SELFTEST_FREE_BYTES

    # Second hermetic-fidelity pin, same reason and same position (before the rewrite):
    # the executed captures really bound the STUB tools and the per-kind test checkout,
    # so the plan_EXEC tool divergence cannot have been a no-op.  Without this a rewrite
    # that silently stopped rebinding would run the REAL host binaries -- /usr/bin/git,
    # /usr/bin/systemctl and friends all exist on the runner -- and the e2e would stay
    # green while proving nothing about the tool split.  Asserted by option-VALUE
    # equality, not membership: a partially applied rewrite would still put the stub
    # directory somewhere in the argv.
    kind_by_capture_id = {str(c["capture_id"]): str(c["kind"]) for c in plan_prod["captures"]}
    assert set(executed_capture_argv) == set(kind_by_capture_id)
    for capture_id, executed in executed_capture_argv.items():
        for option, tool in _E2E_CAPTURE_TOOL_STUBS:
            assert _e2e_option_values(executed, option) == [str(capture_bin / tool)], (capture_id, option)
        expected_repo = str(ROOT) if kind_by_capture_id[capture_id] == "cleanup" else str(fixture_repo)
        assert _e2e_option_values(executed, "--repo") == [expected_repo], capture_id

    # Re-anchor the executed ledger's child argvs to the production binary
    # identities the stubs stood in for (argv[0] pins /usr/bin/pg_dump,
    # {repo}/.venv/bin/python, ... which cannot exist on a hermetic runner), and the
    # executed capture argvs to their seam-free plan_prod twins (the verifier refuses
    # any `--self-test-*` token in the run plan, and binds ledger capture argv to plan
    # capture argv by equality).  Only the argv identity is rewritten; every produced
    # artifact path, sha, association and timestamp is exactly what the real state
    # machine emitted.
    prod_argv_by_command = {str(c["command_id"]): c["argv"] for c in plan_prod["commands"]}
    prod_argv_by_capture = {str(c["capture_id"]): c["argv"] for c in plan_prod["captures"]}
    for event in events:
        if event.get("event_type") == "child_exit":
            event["argv"] = prod_argv_by_command[str(event["command_id"])]
        elif event.get("event_type") == "capture":
            event["argv"] = prod_argv_by_capture[str(event["capture_id"])]
    ledger_path.write_bytes(b"".join(_canonical(event) for event in events))
    run_plan_path = tmp_path / "run-plan.json"
    run_plan_path.write_bytes(_canonical(plan_prod))

    built = bundle_author.build_bundle(
        work_dir=tmp_path,
        repo_path=evidence.REPO_ROOT,
        run_plan_path=run_plan_path,
        ledger_path=ledger_path,
        schema_dump_path=schema_dump_host,
        mutation_head_sha=HEAD,
        verifier_head_sha=VERIFIER_HEAD,
        generated_at="2026-07-15T12:00:00Z",
    )

    result = evidence.verify_bundle(built, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    assert result["qualifies_task_4_5"] is True
    assert result["verdict"] == evidence.PASS_VERDICT


# --------------------------------------------------------------------------- #
# #1250: the verifier structurally rejects self-test seam tokens in capture argv.
# A run plan whose capture argv carries a `--self-test-*` flag executed a
# producer that deviates from what it recorded (stub docker) or fabricated a
# rollback-feasibility figure (`--self-test-free-bytes`), so it is not
# production forensics and can never reach a PASS verdict.
# --------------------------------------------------------------------------- #
import argparse  # noqa: E402

from scripts import node27_timeseries_compression_capture as _capture  # noqa: E402

# The verifier's message when ledger capture argv != plan capture argv.  The seam
# gate must fire on its own, BEFORE this equality binding, otherwise a seam that is
# injected consistently into both sides would sail through.
_CAPTURE_EQUALITY_ERROR = "supervisor capture execution differs from its plan"


def _inject_capture_seam(bundle: dict[str, Any], tmp_path: Path, *, kind: str, tokens: list[str]) -> None:
    """Append `tokens` to one plan capture's argv AND to its equality-bound ledger event."""

    def _append(plan: dict[str, Any]) -> None:
        capture = next(item for item in plan["captures"] if item["kind"] == kind)
        capture["argv"] = [*capture["argv"], *tokens]

    _rewrite_run_plan(bundle, tmp_path, _append)
    ledger_path = Path(bundle["execution"]["ledger"]["path"])
    events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    injected = 0
    for event in events:
        if event.get("event_type") == "capture" and event["kind"] == kind:
            event["argv"] = [*event["argv"], *tokens]
            injected += 1
    # Non-vacuity: the ledger twin really received the seam, so the refusal below
    # cannot be an artefact of an untouched or missing capture event.
    assert injected == 1
    ledger_path.write_bytes(b"".join(_canonical(event) for event in events))
    bundle["execution"]["ledger"] = _file_ref(ledger_path)


def test_verifier_rejects_run_plan_capture_carrying_docker_seam(tmp_path: Path) -> None:
    """A PASS-shaped bundle whose capture argv carries `--self-test-docker-seam` cannot verify."""
    bundle = _bundle(tmp_path)
    _inject_capture_seam(bundle, tmp_path, kind="schema_dump_list", tokens=["--self-test-docker-seam"])
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "--self-test-docker-seam" in message
    assert _CAPTURE_EQUALITY_ERROR not in message
    # DIRECTION pin (#1266): the seam scan runs BEFORE the closed-world pair grammar and
    # is not subsumed by it.  A seam token is also "not a registered capture option paired
    # with a value", so a later reordering that let the grammar swallow it would still
    # refuse the bundle -- with the wrong attribution, silently retiring the #1250 wording.
    assert _UNREGISTERED_TOKEN_WORDING not in message
    assert _SEPARATOR_WORDING not in message


def test_verifier_rejects_run_plan_capture_carrying_free_bytes_seam(tmp_path: Path) -> None:
    """A fabricated disk-headroom seam is refused before the `free_bytes` gates are reached."""
    bundle = _bundle(tmp_path)
    _inject_capture_seam(
        bundle,
        tmp_path,
        kind="post_dry_selection",
        tokens=["--self-test-free-bytes", str(_SELFTEST_FREE_BYTES)],
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "--self-test-free-bytes" in message
    assert _CAPTURE_EQUALITY_ERROR not in message
    # DIRECTION pin (#1266): seam scan first, pair grammar second -- see the docker-seam
    # test above.  This pair is doubly at risk of being subsumed: it is a well-formed
    # flag/value PAIR, so only the seam scan's precedence keeps its attribution.
    assert _UNREGISTERED_TOKEN_WORDING not in message
    assert _SEPARATOR_WORDING not in message


def test_capture_cli_hidden_flags_are_all_self_test_seams() -> None:
    """Every `--help`-suppressed capture flag carries the rejected seam prefix.

    A future hidden flag outside the prefix reddens here, forcing it onto the
    prefix before it can become a new invisible seam.  The prefix is spelled
    literally on purpose: this is a producer-side structural check on capture.py's
    own parser, independent of the verifier module.
    """
    parser = _capture._parser()
    hidden: set[str] = set()
    for action in parser._actions:
        if not action.option_strings or action.help != argparse.SUPPRESS:
            continue
        assert all(option.startswith("--self-test-") for option in action.option_strings), action.option_strings
        hidden.update(action.option_strings)
    # Non-vacuity: renaming/moving the seams out of this parser must not silently
    # turn the loop above into an empty scan.
    assert {"--self-test-free-bytes", "--self-test-docker-seam"} <= hidden


def test_verifier_rejects_unregistered_self_test_prefix_token(tmp_path: Path) -> None:
    """The gate is a PREFIX rule: a never-registered `--self-test-*` token is refused too.

    An enumerated two-token implementation would let the NEXT seam through the
    verifier -- exactly the leak-by-forgetting hole this issue closes.
    """
    bundle = _bundle(tmp_path)
    _inject_capture_seam(bundle, tmp_path, kind="schema_dump_list", tokens=["--self-test-unregistered-probe"])
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "--self-test-unregistered-probe" in message
    assert _CAPTURE_EQUALITY_ERROR not in message
    # DIRECTION pin (#1266): the never-registered probe token is precisely what the
    # closed-world grammar would ALSO refuse, so this is where subsumption would be least
    # visible -- the seam PREFIX rule must own it, not the grammar.
    assert _UNREGISTERED_TOKEN_WORDING not in message
    assert _SEPARATOR_WORDING not in message


# --------------------------------------------------------------------------- #
# #1259: capture argv is anchored to the COMMITTED PRODUCER, not merely to a
# concrete shape.  Before this gate a bundle could claim production forensics
# while its run plan recorded `/usr/bin/printf` as the producer of all twelve
# snapshots -- shape-valid, seam-free, and PASS-shaped.
# --------------------------------------------------------------------------- #


def _replace_capture_argv(bundle: dict[str, Any], tmp_path: Path, *, kind: str, argv: list[str]) -> None:
    """Rewrite one plan capture's argv AND its equality-bound ledger twin.

    Both sides move together on purpose: with only the plan side rewritten, a refusal
    could be the ledger<->plan equality binding rather than the identity anchor, so the
    tests below could not tell a load-bearing gate from an accidental one.
    """

    def _swap(plan: dict[str, Any]) -> None:
        capture = next(item for item in plan["captures"] if item["kind"] == kind)
        capture["argv"] = list(argv)

    _rewrite_run_plan(bundle, tmp_path, _swap)
    ledger_path = Path(bundle["execution"]["ledger"]["path"])
    events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    swapped = 0
    for event in events:
        if event.get("event_type") == "capture" and event["kind"] == kind:
            event["argv"] = list(argv)
            swapped += 1
    assert swapped == 1
    ledger_path.write_bytes(b"".join(_canonical(event) for event in events))
    bundle["execution"]["ledger"] = _file_ref(ledger_path)


def _producer_argv(kind: str, *extra: str, evidence_dir: str) -> list[str]:
    """The production-shaped capture argv, before a single field is corrupted.

    Unlike `_bundle`'s template this one does NOT bake `--mutation-head-sha`: it stays
    caller-supplied through `*extra` because the `[pair_missing]` parametrization below
    needs a template with NO SHA binding at all.  Baking it in would turn that negative
    into a fully valid argv (DID NOT RAISE) and silently delete the "producer invoked
    without any SHA pair" coverage.  Everything the verifier pins by VALUE is baked in,
    so a test that corrupts the SHA is still refused for the SHA.

    `evidence_dir` is REQUIRED and caller-supplied for the opposite reason: the verifier
    derives its expected value from the capture's own `output_path`, which is tmp-scoped,
    so no default (module constant or otherwise) could be correct.  A missing binding
    would re-attribute every negative below to the `--evidence-dir` gate instead of the
    field it corrupts.
    """

    return [
        sys.executable,
        evidence.EXPECTED_CAPTURE_SCRIPT,
        "--kind",
        kind,
        "--evidence-dir",
        evidence_dir,
        *_pinned_capture_options("nhms"),
        *extra,
    ]


def test_verifier_rejects_capture_argv_naming_a_rogue_producer(tmp_path: Path) -> None:
    """The pre-#1259 smoking gun: twelve `/usr/bin/printf` captures verified to PASS."""

    bundle = _bundle(tmp_path)
    _replace_capture_argv(bundle, tmp_path, kind="schema_dump_list", argv=["/usr/bin/printf", "{}"])
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    # The refusal names the producer the bundle FAILED to invoke; the offending token
    # itself is pinned by the rogue-argv[1] case below (here argv[1] is printf's `{}`).
    assert evidence.EXPECTED_CAPTURE_SCRIPT in message
    assert _CAPTURE_EQUALITY_ERROR not in message


def test_verifier_rejects_capture_argv_naming_a_rogue_binary_in_argv1(tmp_path: Path) -> None:
    """argv[0] is unpinned, so the identity claim must live entirely in argv[1]."""

    bundle = _bundle(tmp_path)
    _replace_capture_argv(
        bundle,
        tmp_path,
        kind="catalog_before",
        argv=[sys.executable, "/usr/bin/docker", "--kind", "catalog_before", "--mutation-head-sha", HEAD],
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "/usr/bin/docker" in message
    assert evidence.EXPECTED_CAPTURE_SCRIPT in message


def test_verifier_rejects_capture_argv_bound_to_a_different_kind(tmp_path: Path) -> None:
    """A capture claiming `catalog_before` while invoking the producer for `sizes_pre`."""

    bundle = _bundle(tmp_path)
    _replace_capture_argv(
        bundle,
        tmp_path,
        kind="catalog_before",
        argv=_producer_argv(
            "sizes_pre", "--mutation-head-sha", HEAD, evidence_dir=str(tmp_path / "capture-artifacts")
        ),
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "catalog_before" in message
    assert "sizes_pre" in message
    assert _CAPTURE_EQUALITY_ERROR not in message


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param([], id="pair_missing"),
        pytest.param(["--mutation-head-sha", "f" * 40], id="value_mismatched"),
        pytest.param(["--mutation-head-sha=" + "f" * 40], id="inline_form_mismatched"),
        pytest.param(["--mutation-head-sha"], id="flag_without_value"),
    ],
)
def test_verifier_rejects_capture_argv_without_the_plan_mutation_sha(
    tmp_path: Path, extra: list[str]
) -> None:
    """The producer must be invoked FOR THIS RUN: `--flag value` and `--flag=value` alike."""

    bundle = _bundle(tmp_path)
    _replace_capture_argv(
        bundle,
        tmp_path,
        kind="sizes_pre",
        argv=_producer_argv("sizes_pre", *extra, evidence_dir=str(tmp_path / "capture-artifacts")),
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert HEAD in message
    assert "--mutation-head-sha" in message
    assert _CAPTURE_EQUALITY_ERROR not in message


def test_verifier_accepts_the_inline_mutation_sha_form_when_it_matches(tmp_path: Path) -> None:
    """Non-vacuity for the parametrized rejections: `=` form is a FORM, not a violation.

    Without this, an implementation that rejected every inline token outright would pass
    the mismatch cases while quietly refusing a legitimate spelling.
    """

    bundle = _bundle(tmp_path)
    _replace_capture_argv(
        bundle,
        tmp_path,
        kind="sizes_pre",
        argv=_producer_argv(
            "sizes_pre", f"--mutation-head-sha={HEAD}", evidence_dir=str(tmp_path / "capture-artifacts")
        ),
    )
    result = evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    assert result["verdict"] == evidence.PASS_VERDICT


@pytest.mark.parametrize(
    "tokens",
    [
        pytest.param(["--self-t", str(_SELFTEST_FREE_BYTES)], id="abbreviated_seam_with_value"),
        pytest.param(["--se"], id="two_letter_abbreviation_alone"),
        pytest.param(["--s"], id="one_letter_abbreviation_alone"),
        pytest.param([f"--self-t={_SELFTEST_FREE_BYTES}"], id="abbreviated_seam_inline_form"),
    ],
)
def test_verifier_rejects_argparse_abbreviations_of_a_seam_flag(tmp_path: Path, tokens: list[str]) -> None:
    """Facet B: capture.py runs with `allow_abbrev=True`, so prefixes ARE the seam.

    Today `--self-t` is ambiguous only because two seams share that prefix -- an
    accidental, unrecorded premise.  With one seam registered it would bind
    `--self-test-free-bytes` while carrying no `--self-test-` prefix at all.
    """

    bundle = _bundle(tmp_path)
    _inject_capture_seam(bundle, tmp_path, kind="post_dry_selection", tokens=tokens)
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert tokens[0] in message
    assert _CAPTURE_EQUALITY_ERROR not in message


def test_capture_cli_has_no_non_seam_flag_in_the_abbreviation_rejection_domain() -> None:
    """No legitimate capture flag ever enters the `--se` rejection domain.

    The facet-B gate refuses every base from `--s` to `--self-test-`; this pins the
    measured zero-collision fact (`--systemctl`, `--schema-dump-*` are the only non-seam
    `--s*` flags) so a future `--session-...` flag reddens HERE rather than becoming a
    plan token the verifier silently refuses.
    """

    parser = _capture._parser()
    options = {option for action in parser._actions for option in action.option_strings}
    non_seam = {option for option in options if not option.startswith(evidence.SELF_TEST_SEAM_PREFIX)}
    assert {"--systemctl", "--schema-dump-host", "--schema-dump-container"} <= non_seam
    for option in non_seam:
        assert not option.startswith("--se"), option
    # Non-vacuity: the seams themselves DO live in the domain the gate rejects.
    assert any(option.startswith("--se") for option in options - non_seam)


def test_expected_capture_script_is_the_production_producer_path() -> None:
    """Anti-tautology: gate and fixture share the constant, so pin its VALUE literally.

    A constant mis-derived from `REPO_ROOT` (the ambient checkout) instead of
    `EXPECTED_REPO_PATH` passes every other test in this module -- the fixture would move
    with it -- while silently accepting captures from any checkout the verifier runs in.
    """

    assert evidence.EXPECTED_CAPTURE_SCRIPT == "/home/nwm/NWM/scripts/node27_timeseries_compression_capture.py"
    # The load-bearing coupling the e2e depends on: production plans really do emit it.
    assert plan_author.DEFAULT_CAPTURE_SCRIPT == evidence.EXPECTED_CAPTURE_SCRIPT


# --------------------------------------------------------------------------- #
# #1259 follow-up: the anchor must be REBIND-proof and ABBREVIATION-proof.
# capture.py parses with argparse's default `allow_abbrev=True` and both anchored
# options bind last-wins, so pinning argv[2:4] and the full `--mutation-head-sha`
# spelling left four PASS-shaped rebinds open: a second full `--kind`, `--k`,
# `--kin=`, and `--m`.  Each of them re-aims the producer while every fixed-offset
# and full-spelling check still reads the anchored values.
# --------------------------------------------------------------------------- #

_OTHER_KIND = "sizes_pre"
_OTHER_SHA = "b" * 40


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        pytest.param(["--kind", _OTHER_KIND], "exactly once", id="full_second_kind"),
        pytest.param(["--k", _OTHER_KIND], "abbreviation of --kind", id="k_abbreviation_pair"),
        pytest.param([f"--kin={_OTHER_KIND}"], f"--kin={_OTHER_KIND}", id="kin_abbreviation_inline"),
        pytest.param(["--m", _OTHER_SHA], "abbreviation of --mutation-head-sha", id="m_abbreviation_pair"),
    ],
)
def test_verifier_rejects_argparse_rebinding_of_the_anchored_capture_options(
    tmp_path: Path, tokens: list[str], expected: str
) -> None:
    """Each shape verified to PASS before this gate: argv[2:4] and `--mutation-head-sha`
    both still read the anchored values while the producer would have collected another
    kind, or recorded another mutation SHA."""

    bundle = _bundle(tmp_path)
    _inject_capture_seam(bundle, tmp_path, kind="catalog_before", tokens=tokens)
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert expected in message
    # The refusal is the identity anchor's own, not the plan<->ledger equality binding:
    # `_inject_capture_seam` appends to BOTH sides, so equality still holds here.
    assert _CAPTURE_EQUALITY_ERROR not in message


def test_verifier_rejects_a_rebinding_kind_token_placed_before_the_anchored_pair(tmp_path: Path) -> None:
    """Prefix position too: argv[2:4] is only the anchor's FIRST binding.

    With the rebinding token ahead of the pair, argv[2:4] is no longer `["--kind", kind]`
    -- so this shape must be refused by the position check even if the exactly-once
    check were removed, and vice versa; neither check alone covers both placements.
    """

    bundle = _bundle(tmp_path)
    _replace_capture_argv(
        bundle,
        tmp_path,
        kind="catalog_before",
        argv=[
            sys.executable,
            evidence.EXPECTED_CAPTURE_SCRIPT,
            "--kind",
            _OTHER_KIND,
            "--kind",
            "catalog_before",
            "--mutation-head-sha",
            HEAD,
        ],
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "catalog_before" in message
    assert _CAPTURE_EQUALITY_ERROR not in message


def test_verifier_rejects_a_second_mutation_sha_binding(tmp_path: Path) -> None:
    """Last-wins on the SHA side too: a full second `--mutation-head-sha` rebinds it."""

    bundle = _bundle(tmp_path)
    _inject_capture_seam(
        bundle, tmp_path, kind="sizes_pre", tokens=["--mutation-head-sha", _OTHER_SHA]
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "--mutation-head-sha" in message
    assert _CAPTURE_EQUALITY_ERROR not in message


def test_capture_cli_has_no_flag_abbreviating_an_anchored_option() -> None:
    """The zero-collision fact the abbreviation rejection stands on.

    Rejecting every proper prefix of `--kind` / `--mutation-head-sha` /
    `--schema-dump-container` is only safe while those are the sole `--k*` / `--m*` /
    `--schema-dump-c*` flags in the capture CLI.  A future `--keep-going`, `--max-rows`
    or `--schema-dump-cache` would make `--k` / `--m` / `--schema-dump-c` a legitimate
    spelling the verifier silently refuses -- it reddens HERE instead, before it can
    reach a plan.
    """

    parser = _capture._parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert {option for option in options if option.startswith("--k")} == {"--kind"}
    assert {option for option in options if option.startswith("--m")} == {"--mutation-head-sha"}
    assert {option for option in options if option.startswith("--schema-dump-c")} == {
        "--schema-dump-container"
    }
    for anchored in evidence.ANCHORED_CAPTURE_OPTIONS:
        assert anchored in options, anchored
        for option in options - {anchored}:
            assert not (len(option) >= 3 and anchored.startswith(option)), (option, anchored)
    # Both gates reject the same domain; drift between them would reopen the rebind on
    # whichever side lagged.
    assert supervisor.ANCHORED_CAPTURE_OPTIONS == evidence.ANCHORED_CAPTURE_OPTIONS


# --------------------------------------------------------------------------- #
# #1261: the anchor pins WHO runs; this pins WITH WHAT.  Before this gate a plan
# could keep a perfectly anchored argv[0:4] -- committed producer, right kind,
# right mutation SHA, no seam token -- while binding `--psql/--systemctl/
# --docker/--journalctl/--git/--repo/--container` to stub binaries under /tmp and
# fabricating all twelve snapshots.  The command side has had literal
# `expected_executable` pins since the G-series; the capture side had none.
# --------------------------------------------------------------------------- #
import inspect  # noqa: E402

# The substitute tooling a hostile plan would point the committed producer at.
_STUB_TOOL_PATH = "/tmp/stub-tool"
_PINNED_TOOL_OPTIONS = tuple(sorted(evidence.EXPECTED_CAPTURE_TOOL_VALUES))


def _corrupt_pinned_binding(argv: list[str], option: str, mode: str) -> list[str]:
    """One production-shaped capture argv with exactly ONE pinned binding broken.

    The four modes are the four shapes a single equality-per-option check has to refuse
    at once: a wrong value, no value at all, and a last-wins second binding in either
    argparse spelling (`--flag value` and `--flag=value` are the same binding to the
    producer's parser, so a gate that scanned only the pair form would let the `=`
    spelling through).
    """

    if mode == "mismatched":
        return _rebind_argv_option(argv, option, _STUB_TOOL_PATH)
    if mode == "absent":
        index = argv.index(option)
        return [*argv[:index], *argv[index + 2 :]]
    if mode == "duplicated_pair":
        return [*argv, option, _STUB_TOOL_PATH]
    if mode == "duplicated_inline":
        return [*argv, f"{option}={_STUB_TOOL_PATH}"]
    raise AssertionError(f"unknown corruption mode {mode!r}")


@pytest.mark.parametrize("option", _PINNED_TOOL_OPTIONS)
@pytest.mark.parametrize(
    "mode", ["mismatched", "absent", "duplicated_pair", "duplicated_inline"]
)
def test_verifier_rejects_a_capture_argv_that_misbinds_a_pinned_tool_option(
    tmp_path: Path, option: str, mode: str
) -> None:
    """Every pinned option, every misbinding shape: no PASS verdict.

    `mismatched` is the smoking gun the issue names (an identity-anchored argv aimed at
    stub tooling); `absent` closes the omission variant (an unbound option is not a
    production invocation); the two `duplicated` shapes close the last-wins rebind that
    an exactly-once-free presence check would wave through.
    """

    bundle = _bundle(tmp_path)
    _replace_capture_argv(
        bundle,
        tmp_path,
        kind="sizes_pre",
        argv=_corrupt_pinned_binding(
            _producer_argv(
                "sizes_pre", "--mutation-head-sha", HEAD, evidence_dir=str(tmp_path / "capture-artifacts")
            ),
            option,
            mode,
        ),
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert option in message
    assert evidence.EXPECTED_CAPTURE_TOOL_VALUES[option] in message
    # The refusal is the tool-value gate's own, not the plan<->ledger equality binding:
    # `_replace_capture_argv` rewrites BOTH sides, so equality still holds here.
    assert _CAPTURE_EQUALITY_ERROR not in message


def test_verifier_rejects_a_capture_argv_bound_to_a_foreign_database(tmp_path: Path) -> None:
    """`--database` is pinned DYNAMICALLY, to the plan database the verifier validated.

    A capture that snapshots another database is not evidence about this run, however
    production-shaped every other binding is.
    """

    bundle = _bundle(tmp_path)
    _replace_capture_argv(
        bundle,
        tmp_path,
        kind="sizes_pre",
        argv=_rebind_argv_option(
            _producer_argv(
                "sizes_pre", "--mutation-head-sha", HEAD, evidence_dir=str(tmp_path / "capture-artifacts")
            ),
            "--database",
            "postgres",
        ),
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "--database" in message
    assert "postgres" in message
    assert "nhms" in message
    assert _CAPTURE_EQUALITY_ERROR not in message


@pytest.mark.parametrize(
    ("tokens", "option"),
    [
        pytest.param(["--ps", _STUB_TOOL_PATH], "--psql", id="ps_abbreviation_pair"),
        pytest.param(["--do", _STUB_TOOL_PATH], "--docker", id="do_abbreviation_pair"),
        pytest.param([f"--rep={_STUB_TOOL_PATH}"], "--repo", id="rep_abbreviation_inline"),
    ],
)
def test_verifier_rejects_argparse_rebinding_of_a_pinned_capture_option(
    tmp_path: Path, tokens: list[str], option: str
) -> None:
    """The bypass class an exactly-once check alone cannot see.

    capture.py parses with argparse's default `allow_abbrev=True`, so a trailing
    `--ps /tmp/stub-tool` reaches `--psql` last-wins while the full-name equality above
    still reads `/usr/bin/psql` -- the argv passes every value check and the producer
    still runs the stub.  Closed here the same way #1259 closed it for the identity
    anchor: the base is a proper prefix of a pinned option, so the token is refused.
    """

    bundle = _bundle(tmp_path)
    _inject_capture_seam(bundle, tmp_path, kind="catalog_before", tokens=tokens)
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert tokens[0] in message
    assert option in message
    # `_inject_capture_seam` appends to BOTH sides, so the plan<->ledger equality holds.
    assert _CAPTURE_EQUALITY_ERROR not in message


def test_capture_cli_has_no_flag_abbreviating_a_pinned_capture_option() -> None:
    """The zero-collision fact the pinned-option abbreviation rejection stands on.

    Refusing every proper prefix of `--psql`/`--docker`/`--repo`/... is only safe while no
    OTHER registered capture flag is such a prefix.  A future `--do-not-fsync` or `--rep`
    would make a legitimate spelling something the verifier silently refuses -- it reddens
    HERE instead, before it can ever reach a plan.
    """

    parser = _capture._parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert len(evidence.PINNED_CAPTURE_VALUE_OPTIONS) == 9
    for pinned in evidence.PINNED_CAPTURE_VALUE_OPTIONS:
        assert pinned in options, pinned
        for option in options - {pinned}:
            assert not (len(option) >= 3 and pinned.startswith(option)), (option, pinned)
    # Non-vacuity: the rejection domain really is non-empty -- these bases are exactly the
    # `len >= 3` proper prefixes the rebinding test above exercises, and none of them is a
    # registered flag.
    for base, pinned in (("--ps", "--psql"), ("--do", "--docker"), ("--rep", "--repo")):
        assert base not in options
        assert len(base) >= 3 and pinned.startswith(base) and base != pinned
    # #1263: `--evidence-dir` joined the tuple on the same premise -- it is the ONLY
    # registered `--e*` flag, so refusing `--e`/`--ev`/`--evi`/... collides with nothing.
    # A future `--exclude-...` would redden here before it could reach a plan.
    assert {option for option in options if option.startswith("--e")} == {"--evidence-dir"}


def test_expected_capture_tool_values_match_the_plan_author_defaults() -> None:
    """Drift guard: the verifier RESTATES the production values, it does not import them.

    The restatement keeps the verifier a non-derived oracle (a plan_author edit cannot
    move the expectation with it), so something has to notice when the two really do
    diverge -- that is this test.  The five tool values are FUNCTION SIGNATURE defaults,
    not module constants, so they are read off the signature.
    """

    signature = inspect.signature(plan_author.build_run_plan)
    for option, parameter in (
        ("--psql", "capture_psql"),
        ("--systemctl", "capture_systemctl"),
        ("--docker", "capture_docker"),
        ("--journalctl", "capture_journalctl"),
        ("--git", "capture_git"),
    ):
        assert evidence.EXPECTED_CAPTURE_TOOL_VALUES[option] == signature.parameters[parameter].default, option
    assert evidence.EXPECTED_CAPTURE_TOOL_VALUES["--repo"] == plan_author.DEFAULT_REPO
    assert evidence.EXPECTED_CAPTURE_TOOL_VALUES["--container"] == plan_author.DEFAULT_CONTAINER
    # `--database` is pinned dynamically rather than literally, so its production value is
    # bound through the plan itself; the plan-level check already pins THAT to the bundle.
    assert plan_author.DEFAULT_DATABASE == "nhms"


def test_expected_capture_tool_values_are_the_committed_production_literals() -> None:
    """Anti-tautology: gate and fixtures share the map, so pin the WHOLE map literally.

    The parametrized suites above iterate `EXPECTED_CAPTURE_TOOL_VALUES`, and the argv
    templates build from it -- so a map that lost five entries, or whose `--repo` was
    mis-derived from the ambient `REPO_ROOT` instead of `EXPECTED_REPO_PATH`, would pass
    every one of them vacuously.  Whole-dict comparison, not key-by-key.
    """

    assert evidence.EXPECTED_CAPTURE_TOOL_VALUES == {
        "--psql": "/usr/bin/psql",
        "--systemctl": "/usr/bin/systemctl",
        "--docker": "/usr/bin/docker",
        "--journalctl": "/usr/bin/journalctl",
        "--git": "/usr/bin/git",
        "--repo": "/home/nwm/NWM",
        "--container": "nhms-db",
    }


def test_default_plan_author_capture_argvs_pass_the_whole_capture_gate_stack(tmp_path: Path) -> None:
    """Positive control: what production actually authors verifies.

    The runbook invocation passes only `--mutation-head-sha`/`--output`, so every pinned
    value in a real plan is exactly a `plan_author` default.  Swapping ALL TWELVE capture
    argvs for the real authored ones (not just one) proves the gate stack admits the
    production shape end to end -- a gate that were accidentally unsatisfiable, or a map
    entry with no counterpart in the authored argv, reddens here rather than only ever
    being exercised by rejections.
    """

    bundle = _bundle(tmp_path)
    plan = plan_author.build_run_plan(mutation_head_sha=HEAD, root=str(tmp_path))
    assert len(plan["captures"]) == len(evidence.EXPECTED_CAPTURE_SEQUENCE)
    for capture in plan["captures"]:
        _replace_capture_argv(bundle, tmp_path, kind=str(capture["kind"]), argv=list(capture["argv"]))
    result = evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    assert result["verdict"] == evidence.PASS_VERDICT


# --------------------------------------------------------------------------- #
# #1263: the three residual argv shapes the anchor series left verifying PASS.
# (1) A help early-exit token -- `-h`, `--help`, `--help=x` or an unambiguous
# abbreviation -- makes the recorded producer leave inside argparse before it
# collects anything, so the argv provably did NOT produce the snapshot it is
# recorded against.  (2) `--evidence-dir` is the `os.statvfs` measurement input
# behind the MIN_FREE_BYTES hard gates, so it is now bound RELATIONALLY to the
# capture's own output directory.  argv[0] stays unpinned by design (its residual
# trust root is recorded in the verifier, closure is producer-side).
# --------------------------------------------------------------------------- #

# The distinguishing substring of the relational evidence-dir gate's message: used to
# assert the OTHER refusal classes are not it (and vice versa).
_EVIDENCE_DIR_GATE_WORDING = "own output directory"
# The seam and abbreviation refusal wordings, for the same attribution discipline.
_SEAM_TOKEN_WORDING = "self-test seam token"
_ABBREVIATION_WORDING = "an argparse abbreviation"
_HELP_EARLY_EXIT_TOKENS = (
    "-h",
    "--help",
    "--help=x",
    "--h",
    "--he",
    "--hel",
    # Single-dash CLUSTERS: argparse reads `-hx` as the short options `-h` + `-x`, so the
    # auto help action fires exactly as it does for a bare `-h`.  An equality check on
    # `-h` alone left this whole family verifying PASS.
    "-hx",
    "-hh",
    "-help",
    "-hs",
)


@pytest.mark.parametrize("token", _HELP_EARLY_EXIT_TOKENS)
def test_verifier_rejects_a_capture_argv_carrying_a_help_early_exit_token(
    tmp_path: Path, token: str
) -> None:
    """Every spelling of the help family, appended to an OTHERWISE VALID argv.

    Measured (issue #1263): capture.py's parser keeps argparse's default
    `add_help=True` and `main` calls `parse_args` first, so `-h`/`--help`/`--h`/`--he`/
    `--hel` print the help text and `SystemExit(0)` while `--help=x` is a usage error
    exiting 2 -- no capture runs in any of those cases.  Before this branch each one
    verified PASS: identity-anchored, value-pinned, seam-free, and forensically false.

    The single-dash CLUSTERS (`-hx`, `-hh`, `-help`, `-hs`) are the PR #1264 review's
    bypass: argparse expands a single-dash token into short options, so each of them
    reaches the very same auto help action, printing help and exiting 0 with zero
    captures -- while an equality-on-`-h` gate saw nothing to refuse.
    """

    bundle = _bundle(tmp_path)
    _inject_capture_seam(bundle, tmp_path, kind="catalog_before", tokens=[token])
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert token in message
    # A refusal class of its own: not the seam branch, not either abbreviation branch,
    # not the relational evidence-dir gate, and not the plan<->ledger equality binding
    # (`_inject_capture_seam` appends to BOTH sides, so that equality still holds).
    assert _SEAM_TOKEN_WORDING not in message
    assert _ABBREVIATION_WORDING not in message
    assert _EVIDENCE_DIR_GATE_WORDING not in message
    assert _CAPTURE_EQUALITY_ERROR not in message
    # DIRECTION pin (#1266): the help scan runs BEFORE the closed-world pair grammar and
    # is not subsumed by it.  Every token in this family is also outside the grammar's
    # registered flag set, so without this the grammar could quietly take the family over
    # and the #1263 wording would stop being exercised by anything.
    assert _UNREGISTERED_TOKEN_WORDING not in message


def test_verifier_rejects_a_help_token_placed_between_the_pinned_bindings(tmp_path: Path) -> None:
    """Position independence: the scan is per token, so a mid-argv `--help` is the same.

    A trailing-token-only rejection would be trivially dodged by an author who put the
    early-exit token anywhere else -- argparse does not care where it sits either.
    """

    bundle = _bundle(tmp_path)
    argv = _producer_argv(
        "sizes_pre", "--mutation-head-sha", HEAD, evidence_dir=str(tmp_path / "capture-artifacts")
    )
    index = argv.index("--psql")
    _replace_capture_argv(
        bundle, tmp_path, kind="sizes_pre", argv=[*argv[:index], "--help", *argv[index:]]
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "--help" in message
    assert _CAPTURE_EQUALITY_ERROR not in message
    # DIRECTION pin (#1266): mid-argv is where subsumption would bite first -- a `--help`
    # sitting in FLAG position is exactly the shape the pair grammar judges, so only the
    # help scan's precedence keeps this refusal attributed to the help family.
    assert _UNREGISTERED_TOKEN_WORDING not in message


def test_capture_cli_registers_no_business_flag_in_the_help_rejection_domain() -> None:
    """The zero-collision fact the help-token rejection stands on.

    The gate refuses two DOMAINS: the whole single-dash `-h*` prefix (argparse expands
    single-dash tokens into short-option clusters, so `-hx` reaches help exactly as `-h`
    does) and every `len >= 3` prefix of `--help`.  Both are only safe while the capture
    CLI registers no `--h*` business flag and no single-dash flag at all beyond
    argparse's auto `-h`.  A future `--hosts` or `-v` would make a legitimate spelling
    something the verifier silently refuses (or leave a single-dash flag outside the
    rejection) -- it reddens HERE instead, before it can ever reach a plan.
    """

    parser = _capture._parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert {option for option in options if option.startswith("--h")} == {"--help"}
    assert {option for option in options if not option.startswith("--")} == {"-h"}
    # Non-vacuity: the abbreviations the gate refuses really are unregistered prefixes of
    # the auto help flag -- argparse's `allow_abbrev=True` is what makes them reach it.
    for base in ("--h", "--he", "--hel"):
        assert base not in options
        assert len(base) >= 3 and "--help".startswith(base)
    # Same non-vacuity for the single-dash domain: `-h` is the only registered token the
    # `-h*` prefix rejection swallows, so the cluster spellings it also refuses cost the
    # capture CLI nothing.  (`"--help".startswith("-h")` is False, so the two arms of the
    # gate address disjoint domains.)
    assert not "--help".startswith("-h")
    for cluster in ("-hx", "-hh", "-help", "-hs"):
        assert cluster not in options
        assert cluster.startswith("-h")


@pytest.mark.parametrize(
    "mode", ["mismatched", "absent", "duplicated_pair", "dangling_inline"]
)
def test_verifier_rejects_a_capture_argv_that_misbinds_the_evidence_dir(
    tmp_path: Path, mode: str
) -> None:
    """The four shapes one relational equality has to refuse at once.

    `mismatched` is the issue's smoking gun (an EXISTING, roomier sibling directory: the
    statvfs headroom the snapshot records would be about that filesystem, not the one the
    capture outputs claim); `absent` closes the omission; `duplicated_pair` and
    `dangling_inline` close the last-wins rebind an exactly-once-free check waves through.
    """

    bundle = _bundle(tmp_path)
    expected = str(tmp_path / "capture-artifacts")
    other = tmp_path / "elsewhere-artifacts"
    other.mkdir()
    argv = _producer_argv("sizes_pre", "--mutation-head-sha", HEAD, evidence_dir=expected)
    if mode == "mismatched":
        argv = _rebind_argv_option(argv, "--evidence-dir", str(other))
    elif mode == "absent":
        index = argv.index("--evidence-dir")
        argv = [*argv[:index], *argv[index + 2 :]]
    elif mode == "duplicated_pair":
        argv = [*argv, "--evidence-dir", str(other)]
    else:
        argv = [*argv, "--evidence-dir="]
    _replace_capture_argv(bundle, tmp_path, kind="sizes_pre", argv=argv)
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "--evidence-dir" in message
    # The DERIVED expectation is printed, so the refusal shows what the relation demanded.
    assert expected in message
    assert _EVIDENCE_DIR_GATE_WORDING in message
    # `_replace_capture_argv` rewrites BOTH sides, so this is the gate, not the equality.
    assert _CAPTURE_EQUALITY_ERROR not in message


@pytest.mark.parametrize(
    "base", ["--ev", "--e"], ids=["ev_abbreviation_pair", "e_abbreviation_pair"]
)
def test_verifier_rejects_argparse_rebinding_of_the_evidence_dir_option(
    tmp_path: Path, base: str
) -> None:
    """Abbreviation closure for the newly pinned option, in the SAME change.

    The relational equality matches the full spelling only, so a trailing `--ev /roomy`
    (or `--e`, which is length 3 and so reaches the mechanism) would rebind the measured
    directory last-wins while the equality still read the derived value.  This is the
    existing pinned-prefix branch doing its job on a widened tuple -- so the wording is
    that branch's, and the relational gate itself never fires on such a token.
    """

    bundle = _bundle(tmp_path)
    _inject_capture_seam(
        bundle, tmp_path, kind="catalog_before", tokens=[base, str(tmp_path / "elsewhere-artifacts")]
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert base in message
    assert "--evidence-dir" in message
    assert "pinned capture tooling value" in message
    # The two refusals stay distinct: this is the abbreviation branch, not the equality
    # gate (which the argv still satisfies), and not the plan<->ledger binding.
    assert _EVIDENCE_DIR_GATE_WORDING not in message
    assert _CAPTURE_EQUALITY_ERROR not in message


def _relocated_sizes_post_bundle(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    """A bundle whose `sizes_post` capture writes into a tmp SUBDIRECTORY.

    The relation the gate enforces is `output_path`-relative, so moving one capture's
    output moves its expected `--evidence-dir` with it -- that is what the two tests
    below pin from opposite sides.
    """

    bundle = _bundle(tmp_path)
    nested = tmp_path / "nested-capture-root"
    nested.mkdir()
    ref = _json_ref(nested, "sizes-post.json", _sizes(post=True))
    bundle["sizes"]["post"] = ref
    _replace_produced_artifact(bundle, "compression_enforce", "sizes_post", ref, tmp_path)
    return bundle, nested


def test_verifier_accepts_an_evidence_dir_bound_to_a_relocated_capture_output(
    tmp_path: Path,
) -> None:
    """Relational, NOT absolute: the gate follows `output_path`, it does not pin a root.

    A gate hardcoded to the plan's top-level root would refuse this bundle even though
    its `--evidence-dir` is exactly the sibling of the capture's own output -- and would
    equally refuse a production plan authored with any other `--root`.
    """

    bundle, nested = _relocated_sizes_post_bundle(tmp_path)
    _replace_capture_argv(
        bundle,
        tmp_path,
        kind="sizes_post",
        argv=_producer_argv(
            "sizes_post", "--mutation-head-sha", HEAD, evidence_dir=str(nested / "capture-artifacts")
        ),
    )
    try:
        result = evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    except evidence.EvidenceError as error:  # pragma: no cover - defensive attribution
        assert _EVIDENCE_DIR_GATE_WORDING not in str(error), str(error)
    else:
        assert result["verdict"] == evidence.PASS_VERDICT


def test_evidence_dir_gate_accepts_a_plan_authored_with_a_trailing_slash_root(
    tmp_path: Path,
) -> None:
    """The derivation is TEXTUAL (`rsplit`), and this is why that is load-bearing.

    The double-slash spelling is SYNTHESIZED here by hand, and since #1265 that is the
    only honest way to obtain it: `plan_author` now refuses a non-canonical `--root` at
    authoring time (`test_plan_author_rejects_non_canonical_repo_and_root`), so the
    production author can no longer emit `{root}//capture-artifacts` AND
    `{root}//capture-<kind>.json`.  What this pin guards is therefore no longer a claim
    about the producer but the VERIFIER's own verbatim textual posture: `--evidence-dir`
    is derived from this capture's `output_path` by plain string `rsplit`, so whatever
    spelling the two fields share round-trips exactly, while a `Path(...).parent` /
    `os.path.dirname` "cleanup" would normalize the double slash away, derive
    `{tmp_path}/capture-artifacts`, and refuse this bundle at the evidence-dir gate.  The
    dirname-swap redness proof still holds; only the construction changed.

    The bundle cannot reach PASS in this spelling for a reason that predates #1263 and is
    NOT this gate's: `_artifact_bytes` returns `str(Path(ref["path"]))`, so the ledger
    side of the capture output binding arrives normalized while the plan's `output_path`
    stays raw, and the pre-existing equality at the ledger<->plan capture binding refuses
    the pair.  That refusal is precisely the assertion: reaching it proves execution ran
    PAST the relational evidence-dir gate, which is many checks earlier -- so the gate
    accepted the double-slash round-trip.  Under a normalizing derivation the refusal
    would instead be the evidence-dir gate's own, and this test reddens.
    """

    bundle = _bundle(tmp_path)
    output_path = f"{tmp_path}//capture-sizes_post.json"
    capture = next(
        item for item in _read_ref(bundle["execution"]["run_plan"])["captures"] if item["kind"] == "sizes_post"
    )
    argv = list(capture["argv"])
    # Rewritten BY OPTION NAME, not by offset: only this one value moves to the
    # double-slash sibling, whatever shape the template argv happens to have.
    argv[argv.index("--evidence-dir") + 1] = f"{tmp_path}//capture-artifacts"
    # The two synthesized fields must satisfy the gate's textual relation BEFORE
    # verification runs -- otherwise they could drift apart and the test would stop
    # reaching (and therefore stop pinning) the gate at all.
    assert argv[argv.index("--evidence-dir") + 1] == output_path.rsplit("/", 1)[0] + "/capture-artifacts"
    raw = Path(bundle["sizes"]["post"]["path"]).read_bytes()
    Path(output_path).write_bytes(raw)
    ref = {"path": output_path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    bundle["sizes"]["post"] = ref
    _replace_produced_artifact(bundle, "compression_enforce", "sizes_post", ref, tmp_path)
    _replace_capture_argv(bundle, tmp_path, kind="sizes_post", argv=argv)
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert message == "supervisor capture output path differs"
    assert _EVIDENCE_DIR_GATE_WORDING not in message


def test_verifier_rejects_an_evidence_dir_stranded_by_a_relocated_capture_output(
    tmp_path: Path,
) -> None:
    """The other side of the same relation: the template value is no longer the sibling.

    Same relocation as the positive above, with `--evidence-dir` left at the top-level
    `tmp_path/capture-artifacts` -- proving the derived expectation really tracks THIS
    capture's `output_path` rather than any fixed root the fixture happens to use.
    """

    bundle, nested = _relocated_sizes_post_bundle(tmp_path)
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "--evidence-dir" in message
    assert str(nested / "capture-artifacts") in message
    assert str(tmp_path / "capture-artifacts") in message
    assert _CAPTURE_EQUALITY_ERROR not in message


# --------------------------------------------------------------------------- #
# #1266: the EXIT-2 early-exit family, the last shape class the anchor series left
# verifying PASS.  `-xh`, `--evidence-dirx`, a trailing `-- /tmp/whatever` and a
# perfectly correct `--evidence-dir <expected>` pair moved BEHIND a `--` all satisfied
# every identity anchor, exactly-once binding, tool-value pin and per-token family
# scan -- while the recorded producer would have exited 2 inside `parse_args` having
# collected nothing.  Two gates close it: a position-independent `--` refusal pre-posed
# ahead of the value gates, and a closed-world pair grammar after the family scans.
# --------------------------------------------------------------------------- #

# Distinguishing substrings of the three grammar refusal classes.  Each test below
# asserts its own class present AND the other two absent, the same attribution
# discipline the #1263 tests use -- a single gate that fired on everything would look
# identical from the outside otherwise.
_SEPARATOR_WORDING = "end-of-options separator at argv["
# One class for both flag-position failures (unknown token, and a registered option
# left unpaired at the end): the verifier gives them a shared class phrase with
# different diagnostic tails, so this substring is the class and the tails are
# asserted separately where they matter.
_UNREGISTERED_TOKEN_WORDING = "is not a registered capture option paired with a value"
_VALUE_POSITION_WORDING = "a value beginning with '-'"
_HELP_EARLY_EXIT_WORDING = "an argparse help early-exit token"
_GRAMMAR_REFUSAL_WORDINGS = {
    "separator": _SEPARATOR_WORDING,
    "unregistered": _UNREGISTERED_TOKEN_WORDING,
    "value_position": _VALUE_POSITION_WORDING,
}


def _assert_grammar_refusal_class(message: str, expected: str) -> None:
    """The refusal is THIS grammar class and none of the neighbouring ones.

    The four established capture refusal families (seam, help, both abbreviation arms)
    and the plan<->ledger equality binding are asserted absent too: every mutation below
    is applied to BOTH argv sides, so a refusal quoting the equality would mean the gate
    under test never ran.
    """

    assert _GRAMMAR_REFUSAL_WORDINGS[expected] in message, message
    for name, wording in _GRAMMAR_REFUSAL_WORDINGS.items():
        if name != expected:
            assert wording not in message, message
    assert _SEAM_TOKEN_WORDING not in message, message
    assert _HELP_EARLY_EXIT_WORDING not in message, message
    assert _ABBREVIATION_WORDING not in message, message
    assert _EVIDENCE_DIR_GATE_WORDING not in message, message
    assert _CAPTURE_EQUALITY_ERROR not in message, message


def _plan_capture_argv(bundle: dict[str, Any], kind: str) -> list[str]:
    """The plan-side capture argv as it stands, so index expectations stay derived."""

    plan = _read_ref(bundle["execution"]["run_plan"])
    return list(next(item for item in plan["captures"] if item["kind"] == kind)["argv"])


def _refusal_message(bundle: dict[str, Any]) -> str:
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    return str(excinfo.value)


def test_verifier_rejects_a_capture_argv_carrying_an_unregistered_short_cluster(
    tmp_path: Path,
) -> None:
    """`-xh`: the issue's first measured shape, PASS-verifying until the pair grammar.

    argparse reads a single-dash token as a cluster of short options; the capture CLI
    registers none beyond the auto `-h`, so `-xh` is `unrecognized arguments`, exit 2,
    zero snapshots.  The help family cannot own it (`-xh` does not lead with `-h`, so no
    help action ever runs) and no abbreviation arm sees it -- it is refused as a
    flag-position token the closed world does not contain.
    """

    bundle = _bundle(tmp_path)
    _inject_capture_seam(bundle, tmp_path, kind="catalog_before", tokens=["-xh"])
    message = _refusal_message(bundle)
    assert "-xh" in message
    _assert_grammar_refusal_class(message, "unregistered")
    # The unknown-option tail, not the unpaired-option one: `-xh` is the LAST token, so a
    # grammar that checked pairing before registration would misattribute it.
    assert "knows no such option" in message


def test_verifier_rejects_a_capture_argv_carrying_an_unregistered_long_option(
    tmp_path: Path,
) -> None:
    """`--evidence-dirx`: a SUPERstring of a pinned option, so no abbreviation arm sees it.

    The abbreviation closure refuses proper PREFIXES (`--ev`, `--e`); `--evidence-dirx`
    goes the other way, and argparse refuses it as unrecognized -- exit 2 with the
    relational `--evidence-dir` equality still perfectly satisfied by the real pair.
    """

    bundle = _bundle(tmp_path)
    _inject_capture_seam(
        bundle, tmp_path, kind="catalog_before", tokens=["--evidence-dirx", str(tmp_path / "elsewhere")]
    )
    message = _refusal_message(bundle)
    assert "--evidence-dirx" in message
    _assert_grammar_refusal_class(message, "unregistered")


def test_verifier_rejects_a_capture_argv_with_a_trailing_options_terminator(
    tmp_path: Path,
) -> None:
    """A trailing `-- /tmp/whatever`: capture.py registers no positional, so exit 2.

    The refusal names the token AND its argv index; the index is what tells this shape
    apart from the moved-pair shape below, whose separator sits two tokens earlier.
    """

    bundle = _bundle(tmp_path)
    argv = _plan_capture_argv(bundle, "catalog_before")
    _inject_capture_seam(bundle, tmp_path, kind="catalog_before", tokens=["--", "/tmp/whatever"])
    message = _refusal_message(bundle)
    assert f"bare -- end-of-options separator at argv[{len(argv)}]" in message
    # Distinguishable from the moved-pair placement, which reports two tokens earlier.
    assert f"argv[{len(argv) - 2}]" not in message
    _assert_grammar_refusal_class(message, "separator")


def test_verifier_rejects_a_capture_argv_whose_correct_pair_hides_behind_the_terminator(
    tmp_path: Path,
) -> None:
    """The ugliest shape: a perfectly correct `--evidence-dir <expected>` pair, moved.

    Nothing about the pair is wrong -- it binds exactly the directory the relational gate
    derives.  It is simply behind a `--`, where argparse binds nothing, so the recorded
    producer exits 2 for a MISSING required option.  The pre-posed separator refusal fires
    first: the evidence-dir gate must not be the one speaking here, because reading this
    argv through the (stop-at-`--`) scanner would report the pair as absent and blame the
    author for something they did bind.
    """

    bundle = _bundle(tmp_path)
    argv = _plan_capture_argv(bundle, "catalog_before")
    at = argv.index("--evidence-dir")
    moved = [*argv[:at], *argv[at + 2 :], "--", *argv[at : at + 2]]
    # Non-vacuity: the pair really is intact and really did move, so the refusal below is
    # about its PLACEMENT and nothing else.
    assert sorted(moved) == sorted([*argv, "--"])
    assert moved[-2:] == argv[at : at + 2]
    _replace_capture_argv(bundle, tmp_path, kind="catalog_before", argv=moved)
    message = _refusal_message(bundle)
    assert f"bare -- end-of-options separator at argv[{len(argv) - 2}]" in message
    assert f"argv[{len(argv)}]" not in message
    _assert_grammar_refusal_class(message, "separator")


def test_verifier_rejects_a_dash_leading_value_on_an_unpinned_capture_option(
    tmp_path: Path,
) -> None:
    """`--schema-dump-host -xh`: the exit-2 family's last survivor, by construction.

    The two `--schema-dump-*` options are deliberately value-UNPINNED (their consuming
    pg_dump/docker command identities are pinned on the command side), so no equality gate
    inspects what they bind.  To the real parser this is `expected one argument`, exit 2;
    without the value-position rule the pair grammar would have called it a legitimate
    pair and waved the argv through.
    """

    bundle = _bundle(tmp_path)
    _inject_capture_seam(
        bundle, tmp_path, kind="schema_dump_list", tokens=["--schema-dump-host", "-xh"]
    )
    message = _refusal_message(bundle)
    # Both ends of the offending binding are named: which option, and what it was aimed at.
    assert "--schema-dump-host" in message
    assert "-xh" in message
    _assert_grammar_refusal_class(message, "value_position")


def test_verifier_rejects_a_value_position_terminator_that_would_blind_the_tool_pins(
    tmp_path: Path,
) -> None:
    """P1 regression lock: a value-position `--` must not buy a free tool rebinding.

    `--schema-dump-host -- --psql /tmp/stub` is the shape that makes the gate ORDER
    load-bearing.  A flag-position-only grammar consumes the `--` as an (unpinned)
    schema-dump value, while `_argv_option_values` stops scanning at it -- so the
    exactly-once `--psql` equality would read only the pinned `/usr/bin/psql` before the
    separator and never see the producer being re-pointed at a stub afterwards.  That is
    strictly worse than the position-independent scan this module had before, which is
    why the `--` refusal is pre-posed ahead of every value gate.
    """

    bundle = _bundle(tmp_path)
    argv = _plan_capture_argv(bundle, "schema_dump_list")
    tokens = ["--schema-dump-host", "--", "--psql", "/tmp/stub"]
    _inject_capture_seam(bundle, tmp_path, kind="schema_dump_list", tokens=tokens)
    # The hazard itself, measured on the very argv under test: the option-value scanner
    # alone reports the argv as still bound to the committed psql, so the ONLY thing
    # standing between this bundle and a PASS is the pre-posed separator refusal.
    assert evidence._argv_option_values([*argv, *tokens], "--psql") == ["/usr/bin/psql"]
    assert "/tmp/stub" in [*argv, *tokens]
    message = _refusal_message(bundle)
    assert f"bare -- end-of-options separator at argv[{len(argv) + 1}]" in message
    _assert_grammar_refusal_class(message, "separator")


def test_verifier_rejects_a_capture_argv_ending_with_an_unpaired_registered_option(
    tmp_path: Path,
) -> None:
    """A dangling registered flag is not a pair either -- same refusal class.

    `--schema-dump-host` is used because it is the only registered family whose value no
    equality gate pins: a dangling `--psql` would be caught by its exactly-once value
    check long before the grammar, so it could not exercise this branch.
    """

    bundle = _bundle(tmp_path)
    _inject_capture_seam(bundle, tmp_path, kind="schema_dump_list", tokens=["--schema-dump-host"])
    message = _refusal_message(bundle)
    assert "--schema-dump-host" in message
    _assert_grammar_refusal_class(message, "unregistered")
    # The unpaired tail, not the unknown-option one: this option IS registered.
    assert "ends the argv with no value bound" in message
    assert "knows no such option" not in message


def test_capture_grammar_flag_set_equals_the_capture_cli_registered_surface() -> None:
    """The structural premise the closed world stands on, pinned against the real parser.

    The verifier restates its flag set as LITERALS rather than importing capture.py (the
    module's non-derived-oracle posture), so nothing makes the two follow each other --
    except this test.  Add a flag to the capture CLI without adding it here and the
    grammar starts refusing a legitimate production spelling; delete one and the grammar
    keeps admitting a token the producer no longer understands.  Either way it reddens
    HERE, at the seam, instead of in a forensic verdict months later.

    The union spells out exactly what the grammar deliberately does NOT admit: argparse's
    auto `-h`/`--help` pair (owned by the help early-exit scan) and the `--self-test-*`
    seams (owned by the #1250 seam scan) -- both refused BEFORE the grammar runs, so
    granting them standing in its allow-set would state the opposite of what those gates
    decided.
    """

    parser = _capture._parser()
    registered = {option for action in parser._actions for option in action.option_strings}
    seams = {option for option in registered if option.startswith(evidence.SELF_TEST_SEAM_PREFIX)}
    assert set(evidence.REGISTERED_CAPTURE_FLAGS) | {"-h", "--help"} | seams == registered
    # Measured surface today, so a change in either direction has to be conscious.
    assert len(registered) == 17
    assert len(evidence.REGISTERED_CAPTURE_FLAGS) == 13
    # Non-vacuity: the tuple has no duplicates (a dupe would let the equality above hold
    # with a missing flag) and grants no seam any standing.
    assert len(set(evidence.REGISTERED_CAPTURE_FLAGS)) == len(evidence.REGISTERED_CAPTURE_FLAGS)
    assert not set(evidence.REGISTERED_CAPTURE_FLAGS) & seams
    assert seams == {"--self-test-free-bytes", "--self-test-docker-seam"}
    # Premise pinned: the capture CLI's surface is PURELY OPTIONAL -- it registers no
    # positional argument.  The set derivation above reads `option_strings` only, so a
    # positional (whose `option_strings` is empty) is invisible to every assertion above;
    # adding one would keep them all green while flipping a trailing `-- /tmp/whatever`
    # from exit 2 to a clean parse, making the separator refusal's "registers no
    # positional argument" claim factually false at the moment it is printed.
    assert all(action.option_strings for action in parser._actions)
    # Premise pinned: every flag the grammar admits consumes EXACTLY ONE value.  The pair
    # grammar advances two tokens per registered flag, so a registered flag that took zero
    # values (`store_true`/`store_const`) or two (`nargs=2`) would desynchronize the
    # verifier's pairing from the producer's actual parsing -- a legitimate production
    # spelling refused, or an illegitimate one admitted.  `-h`/`--help` and the two seams
    # are exempt by design: they are refused BEFORE the grammar runs, and
    # `--self-test-docker-seam` is deliberately a `store_true`.
    arity_pinned = 0
    for action in parser._actions:
        if not set(action.option_strings) & set(evidence.REGISTERED_CAPTURE_FLAGS):
            continue
        arity_pinned += 1
        assert action.nargs is None, action.option_strings
        # `_StoreTrueAction` subclasses `_StoreConstAction`, so this one check covers both
        # zero-value spellings regardless of which one a future edit reaches for.
        assert not isinstance(action, argparse._StoreConstAction), action.option_strings
    # Non-vacuity: the loop really visited every registered flag, not a subset.
    assert arity_pinned == len(evidence.REGISTERED_CAPTURE_FLAGS)


def test_argv_option_values_stops_scanning_at_the_options_terminator() -> None:
    """`--` means "no more options" to argparse, so it must mean that here too.

    Definition consistency, exercised directly rather than only through the gate stack:
    a binding BEFORE the separator counts, an identical binding after it does not, in
    both argparse spellings.  The match is exact equality -- every registered flag starts
    with `--` as well, so a prefix test would stop the scan on the first real option.
    """

    assert evidence._argv_option_values(["--psql", "/usr/bin/psql", "--", "--psql", "/tmp/stub"], "--psql") == [
        "/usr/bin/psql"
    ]
    assert evidence._argv_option_values(["--", "--psql", "/tmp/stub"], "--psql") == []
    assert evidence._argv_option_values(
        ["--psql=/usr/bin/psql", "--", "--psql=/tmp/stub"], "--psql"
    ) == ["/usr/bin/psql"]
    # Not a prefix rule: a real option is not a terminator, and neither is `--=x`.
    assert evidence._argv_option_values(["--psql", "/usr/bin/psql"], "--psql") == ["/usr/bin/psql"]
    assert evidence._argv_option_values(["--=x", "--psql", "/tmp/stub"], "--psql") == ["/tmp/stub"]


# --------------------------------------------------------------------------- #
# #1265: path canonicality is a PRODUCER-side precondition.  The verifier renders
# ledger-side artifact refs through `str(Path(...))` but compares the plan side
# VERBATIM, so a non-canonical `--root`/`--repo` used to author a perfectly
# shape-valid plan whose bundle then deterministically FAILED the forensic gate
# with "supervisor capture output path differs" -- a message about nothing the
# operator actually did.  `plan_author` now refuses such input at the entrance;
# the verifier's verbatim posture is untouched (that is the point of the route).
# --------------------------------------------------------------------------- #

# The forensic refusal a non-canonical root used to end up at, minutes later and with
# an unrelated message.  Asserted ABSENT from the authoring refusals below: the guard
# must be non-vacuously the new failure mode, not a rename of the old one.
_CAPTURE_OUTPUT_PATH_ERROR = "supervisor capture output path differs"
_NON_CANONICAL_PATHS = {
    "trailing_slash": "/x/y/",
    "duplicate_slash": "/x//y",
    "dot_segment": "/x/./y",
    # The two normalization-stable-yet-slash-terminated strings the guard's second
    # conjunct exists for (`str(Path("//")) == "//"`): a `root="//"` would emit
    # `///capture-<kind>.json`, which BOTH verifier-side normalizations collapse to
    # `/capture-<kind>.json` -- recreating exactly the false refusal this guard kills.
    "bare_slash_root": "/",
    "double_slash_root": "//",
    # The third conjunct's own red.  `..` IS normalization-stable and symmetric on both
    # verifier sides, so neither of the first two conjuncts sees it -- but the no-follow
    # walkers (`safe_fs._absolute_parts`) refuse any `..` component, on the supervisor's
    # first capture write AND on the verifier's artifact reads, while prearm normalizes
    # first and passes.  Unguarded, such a root authors fine, prearms green, and aborts
    # INSIDE the one-shot replay window with "Unsafe path component: '..'".
    "dot_dot_segment": "/x/../y",
}


@pytest.mark.parametrize("label", ["root", "repo", "schema_dump_host"])
@pytest.mark.parametrize("shape", sorted(_NON_CANONICAL_PATHS))
def test_plan_author_rejects_non_canonical_repo_and_root(label: str, shape: str) -> None:
    """All three labels, all six shapes: refused at authoring with an ACCURATE message.

    `repo` matters as much as `root`: it f-strings the command argv paths the verifier
    pins against `expected_executable` literals, so a trailing-slash repo poisons the
    command side exactly the way a trailing-slash root poisons the capture side.  The
    message must name the label, the offending value and its canonical rendering --
    an operator who mistypes a slash should learn that from the author, not from a
    forensic verdict about capture output paths half an hour later.

    `schema_dump_host` (#1268) is the same disease one field over: the value goes
    verbatim into the pg_dump argv and into that command's `artifact_associations`, while
    the verifier renders the ledger-side artifact ref through `str(Path(...))` and
    compares the plan side VERBATIM in `_validate_supervisor_execution` -- so a `//`-bearing
    host dump path authored a plan whose bundle could only ever fail with "supervisor
    observed artifact path differs from run plan output".  A `..` component takes a
    different route to the same class: prearm passes it (`is_absolute` only), and the
    supervisor's produced-artifact no-follow inspect aborts the moment pg_dump exits,
    inside the one-shot replay window.
    """

    value = _NON_CANONICAL_PATHS[shape]
    with pytest.raises(plan_author.PlanAuthorError) as excinfo:
        plan_author.build_run_plan(mutation_head_sha=HEAD, **{label: value})
    message = str(excinfo.value)
    assert label in message
    assert value in message
    assert str(Path(value)) in message
    assert _CAPTURE_OUTPUT_PATH_ERROR not in message


def test_plan_author_accepts_a_canonical_root(tmp_path: Path) -> None:
    """Positive: a canonical root still authors, and every path it records is canonical.

    The end-to-end control is
    `test_default_plan_author_capture_argvs_pass_the_whole_capture_gate_stack`, which
    authors with `root=str(tmp_path)` and verifies all twelve capture argvs to PASS.
    This one adds the attribution that control cannot give on its own: authoring raises
    nothing, and each recorded `output_path` is Path-normalization-stable -- the exact
    property the verifier's verbatim plan-side comparisons depend on.
    """

    plan = plan_author.build_run_plan(mutation_head_sha=HEAD, root=str(tmp_path))
    assert len(plan["captures"]) == len(evidence.EXPECTED_CAPTURE_SEQUENCE)
    for capture in plan["captures"]:
        output_path = str(capture["output_path"])
        assert output_path == str(Path(output_path)), output_path


def test_plan_author_accepts_a_canonical_custom_schema_dump_host(tmp_path: Path) -> None:
    """Positive for the #1268 label: a canonical host dump path authors, VERBATIM.

    The guard's job is to refuse, never to rewrite: the value the operator passed must
    land byte-identical in the pg_dump argv and in that command's
    `artifact_associations["schema_dump"]`, because the verifier compares exactly those
    recorded bytes against the Path-normalized ledger ref (the association comparison in
    `_validate_supervisor_execution`).  A guard that silently canonicalized instead of
    refusing would make the plan's recorded bytes differ from what the operator reviewed
    -- a different, quieter defect.
    """

    dump = str(tmp_path / "schema-before.dump")
    plan = plan_author.build_run_plan(mutation_head_sha=HEAD, schema_dump_host=dump)
    command = next(item for item in plan["commands"] if item["kind"] == "pg_dump")
    assert command["artifact_associations"]["schema_dump"] == dump
    assert command["argv"][-1] == dump


def test_plan_author_accepts_the_boundary_double_slash_schema_dump_host() -> None:
    """The `//x` boundary, extended to the third guarded label (#1268).

    `test_plan_author_accepts_the_recorded_boundary_root` pins the LEADING double slash
    for `root`; the guard is one shared loop, so the carve-out is transitive in the code
    but only executable for one label.  This makes it executable for `schema_dump_host`
    as well: POSIX (and pathlib) preserve exactly two leading slashes, so
    `//x/schema-before.dump` is normalization-stable, and both sides that compare this
    value -- the verifier's verbatim plan-side association read and the `str(Path(...))`
    ledger-side rendering -- land on the same bytes.  Asserts the recording is VERBATIM
    on both the pg_dump argv slot and the association, the same posture the canonical
    positive above pins.
    """

    dump = "//x/schema-before.dump"
    assert str(Path(dump)) == dump
    plan = plan_author.build_run_plan(mutation_head_sha=HEAD, schema_dump_host=dump)
    command = next(item for item in plan["commands"] if item["kind"] == "pg_dump")
    assert command["artifact_associations"]["schema_dump"] == dump
    assert command["argv"][-1] == dump


def test_plan_author_module_defaults_are_canonical() -> None:
    """Structural: the guard can never refuse the module's own defaults.

    The runbook's authorized command passes neither `--root` nor `--repo` -- and no
    `--schema-dump-host` either -- so all three defaults go straight through the clause
    on every production authoring.  The negatives above prove the clause really does
    refuse; without this pin a stray trailing slash in any of the literals would ship
    an author that cannot author at all.
    """

    for name in ("DEFAULT_ROOT", "DEFAULT_REPO", "DEFAULT_SCHEMA_DUMP_HOST"):
        value = getattr(plan_author, name)
        assert value == str(Path(value)), name
        assert not value.endswith("/"), name


def test_plan_author_accepts_the_recorded_boundary_root() -> None:
    """The one boundary the guard comment records as DELIBERATELY still accepted.

    A LEADING double slash: POSIX (and pathlib) preserve exactly two leading slashes,
    so `//x` is normalization-stable and its f-string expansions stay symmetric on
    both verifier sides -- unlike the bare `//` root, whose `///…` expansion collapses
    (that asymmetry is why `//` is in the negatives above and `//x` is here).  The
    third conjunct leaves it alone too: `PurePosixPath("//x").parts` is `("//", "x")`,
    and the no-follow walkers filter the anchor out before looking for `..`.  Asserts
    the probe property the guard buys: for every accepted root R,
    `str(Path(f"{R}/x")) == f"{R}/x"`.  (A `..` root was recorded as a second accepted
    boundary until fix round 1 of #1265 showed it aborts mid-replay-window on the
    no-follow walkers; it is now one of the negatives above.)
    """

    root = "//x"
    plan = plan_author.build_run_plan(mutation_head_sha=HEAD, root=root)
    capture = next(item for item in plan["captures"] if item["kind"] == "sizes_post")
    assert capture["output_path"] == f"{root}/capture-sizes_post.json"
    assert str(Path(capture["output_path"])) == capture["output_path"]


def test_plan_author_leaves_the_container_dump_path_unguarded_by_adjudication() -> None:
    """The recorded adjudication (#1268), in executable form: container path NOT guarded.

    `--schema-dump-container` names a path inside the DB container, and the ruling that
    it stays outside the canonicality guard rests on SYMMETRY ALONE -- not on any "no
    verifier checks it" claim, which is false: the supervisor extracts it and
    `sha256sum`s it in the container.  The complete consumer set is (a) plan_author's
    pg_restore `--list` command block, whose command records NO artifact associations, so
    the verbatim-vs-normalized association comparison in `_validate_supervisor_execution`
    never sees it; (b) the verifier's containment+shape argv gate
    (`_validate_exact_command_argv`, `pg_restore_list` branch) and the same containment
    check on the captured listing (`_validate_dump_listing`); (c) the supervisor's mirror
    gates -- `_assert_exact_argv` and `resolve_container_pg_restore_identity` (invoked
    from `execute_producer_state_machine`), which takes
    `argv[-1]` verbatim, asserts the same mount containment and hashes that exact string;
    and (d) the CAPTURE argv route -- plan_author's `schema_dump_list` capture argv
    carries `--schema-dump-container` too, the supervisor's pre-spawn capture gate
    (`_assert_capture_producer_argv`) asserts the same containment on that bound value,
    capture.py :531/:533 then executes `docker exec pg_restore --list` on it and records
    `list_argv` into the forensic bundle, and the capture-argv equality inside
    `_validate_supervisor_execution` compares the WHOLE capture argv by EXACT equality.
    (Cross-file sites are named by SYMBOL, not by line number: #1269 shifted both gate
    modules and staled every number this docstring used to carry.)  Since #1269 all five
    of those gates ask the shared
    `container_dump_path_within_mount` predicate (mount prefix AND no `..` component),
    which JUDGES and never rewrites -- so every one of them stays textual with zero
    `Path()` normalization on either side, and the false-refusal disease this guard
    exists for still cannot reach this field.  This test is the "no third silent state"
    pin: an interior `//` container path (which the predicate admits, `PurePosixPath`
    dropping the empty component) AUTHORS, and lands verbatim as the pg_restore list
    argv's last element.  If a future change decides to guard the container path AT
    AUTHORING TIME, THIS is the test that must be flipped consciously -- the ruling
    cannot erode by accident.
    """

    container_dump = "/var/lib/postgresql//evidence/schema-before.dump"
    plan = plan_author.build_run_plan(
        mutation_head_sha=HEAD, schema_dump_container=container_dump
    )
    command = next(item for item in plan["commands"] if item["kind"] == "pg_restore_list")
    assert command["argv"][-1] == container_dump
    assert command["artifact_associations"] == {}


@pytest.mark.parametrize("label", ["root", "repo", "schema_dump_host"])
def test_plan_author_rejects_relative_paths_for_every_guarded_label(label: str) -> None:
    """The fifth shape: a RELATIVE value, refused by the pre-existing absolute branch.

    That branch (`{label} must be an absolute path`) runs first inside the same loop, so
    extending the loop's domain extended it to `schema_dump_host` for free.  Its message
    posture is deliberately WEAKER than the canonicality branch's: it names the label but
    NOT the offending value and NOT a canonical rendering -- there is no meaningful
    canonical rendering for a relative path, and this test records that difference rather
    than claiming the two messages are equivalent.  Pinning the branch per label means a
    future refactor that moves the `is_absolute` check out of the loop cannot silently
    drop the refusal for one of them.
    """

    value = "relative/schema.dump"
    with pytest.raises(plan_author.PlanAuthorError) as excinfo:
        plan_author.build_run_plan(mutation_head_sha=HEAD, **{label: value})
    message = str(excinfo.value)
    assert label in message
    assert "must be an absolute path" in message


# --------------------------------------------------------------------------- #
# #1269: the container dump path gates judge CONTAINMENT, not a string opening.
# FOUR of today's five gates used to spell "inside the DB container data mount"
# as a bare `startswith("/var/lib/postgresql/")`; the fifth -- the supervisor's
# pre-spawn capture-argv gate -- did not judge the path at all until this change
# gave it a check.  So `/var/lib/postgresql/../../../etc/shadow` passed every
# route and then got `docker exec sha256sum`-ed (its digest recorded as
# `dump_sha256`) and `docker exec pg_restore --list`-ed inside the container.  Of
# the four inline copies only `resolve_container_pg_restore_identity`'s refusal
# claimed containment ("pg_restore dump path is outside the DB container data
# mount"); the other three say only "argv differs" / "argv/output ownership
# differs" / "not verifiable".  One shared predicate now answers the question for
# all five, and it JUDGES ONLY -- no normalization enters the lane, so the
# verbatim posture and the #1268 authoring adjudication are both untouched.
# --------------------------------------------------------------------------- #

# The predicate is exercised through the verifier module's by-name binding rather than
# through a direct `node27_container_contract` import: this suite is the pinned
# TRANSITIVE-ONLY member of the contract's CI dependent closure
# (tests/test_select_ci_tests.py's anti-vacuity floor), so a direct import line here
# would quietly demote that floor to a grep-findable one.  The binding is the same
# function object either way, and the drift guard below asserts both gate planes hold
# exactly that object.

# One leaves straight from the mount root, the other from a real subdirectory.
_MOUNT_ROOT_TRAVERSAL = "/var/lib/postgresql/../../../etc/shadow"
_SUBDIR_TRAVERSAL = "/var/lib/postgresql/evidence/../../../../etc/passwd"
_CONTAINER_DUMP_TRAVERSALS = (_MOUNT_ROOT_TRAVERSAL, _SUBDIR_TRAVERSAL)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("/var/lib/postgresql/evidence/schema-before.dump", id="plan_author_default"),
        # #1268's adjudicated shape: `PurePosixPath` drops the empty component, so the
        # gates see no `..` and admit it exactly as the bare prefix did.
        pytest.param("/var/lib/postgresql//evidence/schema.dump", id="interior_double_slash"),
        # `parts` drops the trailing empty component too -- today's gate behaviour,
        # and normalization leaves this row admitted as well, so it discriminates
        # nothing between implementations.
        pytest.param("/var/lib/postgresql/evidence/", id="trailing_slash"),
        # THE discriminating accept row: a `resolve()`/`normpath`-based
        # implementation turns this into `/var/lib/postgresql`, which no longer
        # carries the trailing-slash prefix, so this is the one accept row such an
        # implementation would silently flip to a refusal.
        pytest.param("/var/lib/postgresql/", id="bare_mount_root"),
        # `..` is a whole-COMPONENT test, not a substring one: a filename may contain it.
        pytest.param("/var/lib/postgresql/evidence/a..b.dump", id="dots_inside_a_filename"),
    ],
)
def test_container_dump_path_within_mount_accepts_in_mount_values(value: str) -> None:
    assert evidence.container_dump_path_within_mount(value) is True


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_MOUNT_ROOT_TRAVERSAL, id="traversal_from_mount_root"),
        pytest.param(_SUBDIR_TRAVERSAL, id="traversal_from_subdirectory"),
        pytest.param("/var/lib/postgresql/..", id="bare_parent_of_the_mount"),
        # The shortest escape that still looks like it descends first.
        pytest.param("/var/lib/postgresql/x/..", id="escape_after_one_real_segment"),
        # The dangling-flag sentinel gate 5 hands over; it fails the prefix conjunct.
        pytest.param("", id="empty_string_sentinel"),
        pytest.param("/tmp/schema.dump", id="prefix_miss"),
    ],
)
def test_container_dump_path_within_mount_rejects_escaping_values(value: str) -> None:
    assert evidence.container_dump_path_within_mount(value) is False


@pytest.mark.parametrize("dump_path", _CONTAINER_DUMP_TRAVERSALS)
def test_verifier_pg_restore_list_argv_gate_refuses_a_traversal_dump_path(dump_path: str) -> None:
    """Gate 1, reached directly: the plan-side argv gate refuses on its own.

    Hand-crafted argv, no upstream gate involved -- a bundle whose plan never passed
    through `plan_author` is exactly the case these gates exist for.
    """

    with pytest.raises(evidence.EvidenceError, match="pg_restore list argv differs"):
        evidence._validate_exact_command_argv(
            ["/usr/bin/docker", "exec", "nhms-db", "/usr/bin/pg_restore", "--list", dump_path],
            kind="pg_restore_list",
            associations={},
            label="run plan command[2]",
        )


@pytest.mark.parametrize(
    "dump_path",
    [
        "/var/lib/postgresql/evidence/schema.dump",
        "/var/lib/postgresql//evidence/schema.dump",
    ],
)
def test_verifier_pg_restore_list_argv_gate_admits_in_mount_dump_paths(dump_path: str) -> None:
    """Non-vacuity for the gate-1 refusal, including the #1268 interior-`//` shape."""

    evidence._validate_exact_command_argv(
        ["/usr/bin/docker", "exec", "nhms-db", "/usr/bin/pg_restore", "--list", dump_path],
        kind="pg_restore_list",
        associations={},
        label="run plan command[2]",
    )


def _dump_listing(dump_path: str) -> dict[str, Any]:
    """A schema-dump-list document whose only variable is the container dump path."""

    listing = {
        "captured_at": "2026-07-15T11:20:00Z",
        "snapshot_id": "schema-dump-list",
        "mutation_head_sha": HEAD,
        **_pg_restore_record("3" * 64),
    }
    listing["list_argv"][-1] = dump_path
    return listing


@pytest.mark.parametrize("dump_path", _CONTAINER_DUMP_TRAVERSALS)
def test_captured_listing_gate_refuses_a_traversal_dump_path(dump_path: str) -> None:
    """Gate 2, reached directly with a hand-crafted listing document.

    This is the gate that judges what the capture producer ALREADY ran, so it has to
    refuse independently of whether the plan-side gate above ever saw the same value.
    """

    with pytest.raises(
        evidence.EvidenceError, match="schema forensic dump/list identity is not verifiable"
    ):
        evidence._validate_dump_listing(
            _dump_listing(dump_path),
            dump_ref={"sha256": "3" * 64},
            mutation_head_sha=HEAD,
        )


@pytest.mark.parametrize(
    "dump_path",
    [
        "/var/lib/postgresql/evidence/schema.dump",
        "/var/lib/postgresql//evidence/schema.dump",
    ],
)
def test_captured_listing_gate_admits_in_mount_dump_paths(dump_path: str) -> None:
    """Non-vacuity for gate 2, and the verbatim posture: the value comes back unrewritten."""

    validated = evidence._validate_dump_listing(
        _dump_listing(dump_path),
        dump_ref={"sha256": "3" * 64},
        mutation_head_sha=HEAD,
    )
    assert validated["list_argv"][-1] == dump_path


def test_verifier_plan_capture_gate_refuses_a_container_dump_abbreviation(tmp_path: Path) -> None:
    """The mirrored anchored tuple's own behaviour change, on the verifier plane.

    `--schema-dump-container` joined `ANCHORED_CAPTURE_OPTIONS` so an abbreviation cannot
    smuggle the binding past the supervisor's exact-base value scan; because the tuples
    are pinned equal cross-plane, the verifier's plan-capture gate newly refuses the same
    spelling.  No committed producer emits abbreviations, so nothing legitimate moves.
    """

    bundle = _bundle(tmp_path)
    _inject_capture_seam(
        bundle,
        tmp_path,
        kind="schema_dump_list",
        tokens=[f"--schema-dump-c={_MOUNT_ROOT_TRAVERSAL}"],
    )
    with pytest.raises(evidence.EvidenceError) as excinfo:
        evidence.verify_bundle(bundle, receipt_schema=RECEIPT_SCHEMA, verifier_head_sha=VERIFIER_HEAD)
    message = str(excinfo.value)
    assert "abbreviation of --schema-dump-container" in message


def test_no_gate_module_retains_an_inline_container_mount_prefix_check() -> None:
    """Single-source drift guard: the predicate has exactly one home.

    A future edit that "just adds the prefix check back" at one gate would reopen the
    hole at that gate alone.  This guard is a SPELLING scan, so state exactly what it
    buys: it catches the three spellings such an edit would plausibly reach for -- the
    double-quoted literal the four old gates used, its single-quoted twin (ruff selects
    only E,F,I here, so no rule forces one quote style), and a `startswith` of the now
    exported `CONTAINER_DB_MOUNT_PREFIX`.  It cannot catch every possible re-spelling
    (`value[: len(prefix)] == prefix`, a locally re-declared literal, ...); the real
    backstop against a reopened hole is the per-gate behavioural traversal refusals --
    the verifier gates above, the supervisor gates in
    `test_node27_timeseries_compression_supervisor.py` -- which judge what each gate
    DOES rather than how it is written.
    Scoped to the two GATE modules: `plan_author`'s DEFAULT container path is a value
    literal, not a containment check, and stays deliberately out of scope.
    """

    inline_checks = (
        'startswith("/var/lib/postgresql/")',
        "startswith('/var/lib/postgresql/')",
        "startswith(CONTAINER_DB_MOUNT_PREFIX)",
    )
    for name in (
        "scripts/node27_timeseries_compression_live_evidence.py",
        "scripts/node27_timeseries_compression_supervisor.py",
    ):
        source = (ROOT / name).read_text(encoding="utf-8")
        for inline_check in inline_checks:
            assert source.count(inline_check) == 0, (name, inline_check)
        assert "container_dump_path_within_mount" in source, name
    # Single SOURCE, not merely a single spelling: both planes hold the one function
    # object the contract module exports, so there is no second copy to drift.
    assert supervisor.container_dump_path_within_mount is evidence.container_dump_path_within_mount


# ---------------------------------------------------------------------------
# Issue #1351: the runner's receipts gained a `budget` block at schema_version
# "2.1". The consumer TOLERATES the new shape through the schema file it
# already loads; it derives nothing from it, and the two frozen archival
# contracts below stay frozen.
# ---------------------------------------------------------------------------


def test_load_receipt_accepts_a_2_1_receipt_carrying_budget(tmp_path: Path) -> None:
    """The structural gate follows the schema file — no verifier edit needed.

    Deliberately exercises `_load_receipt` alone rather than `verify_bundle`:
    the bundle gate pins `schema_version == "2.0"` because the #1069 bundle it
    verifies is a frozen historical capture, and that pin is not this issue's
    to relax.
    """

    receipt = {
        **_receipt(enforce=False),
        "schema_version": "2.1",
        "budget": {
            "compress_timeout_ms": 3_600_000,
            "wrapper_wall_seconds": 3_900,
            "systemd_wall_seconds": 3_940,
        },
    }
    ref = _json_ref(tmp_path, "receipt-2.1.json", receipt)

    loaded_ref, loaded = evidence._load_receipt(ref, "dry-run receipt", RECEIPT_SCHEMA)

    assert loaded_ref["sha256"] == ref["sha256"]
    assert loaded["schema_version"] == "2.1"
    assert loaded["budget"]["compress_timeout_ms"] == 3_600_000

    half = copy.deepcopy(receipt)
    del half["budget"]["systemd_wall_seconds"]
    with pytest.raises(evidence.EvidenceError):
        evidence._load_receipt(_json_ref(tmp_path, "receipt-half.json", half), "dry-run receipt", RECEIPT_SCHEMA)


def test_expected_timeout_seconds_stays_a_frozen_literal() -> None:
    """#1351 added the wall to the receipt; this expectation must NOT follow it.

    Deriving the archival expectation from a receipt field (or from the
    runner's operator-configurable default) would re-validate historical
    bundles against whatever the current configuration happens to be.
    """

    assert evidence.EXPECTED_TIMEOUT_SECONDS == 900
    source = (ROOT / "scripts/node27_timeseries_compression_live_evidence.py").read_text(encoding="utf-8")
    definitions = [
        line for line in source.splitlines() if line.startswith("EXPECTED_TIMEOUT_SECONDS")
    ]
    assert definitions == ["EXPECTED_TIMEOUT_SECONDS = 900"]
    assert "receipt" not in definitions[0]
    assert "budget" not in definitions[0]


def test_verify_bundle_keeps_the_frozen_2_0_semantic_pin() -> None:
    """The bundle gate must not be "helpfully" widened to accept "2.1"."""

    source = (ROOT / "scripts/node27_timeseries_compression_live_evidence.py").read_text(encoding="utf-8")
    assert 'dry["schema_version"] != "2.0"' in source
    assert 'enforce["schema_version"] != "2.0"' in source
