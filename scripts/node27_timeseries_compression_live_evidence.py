#!/usr/bin/env python3
"""Independently validate and publish issue #1069 live compression evidence.

The input bundle is an operator-authored JSON document whose artifact fields
are absolute ``{path, sha256, bytes}`` references.  Referenced JSON files are
re-read, size/hash checked, and interpreted here.  This verifier never imports
the compression runner and never executes SQL: it can publish evidence, but it
cannot migrate, compress, decompress, drop chunks, or mutate roles.

Required referenced JSON shapes are documented by ``BUNDLE_CONTRACT`` below
and in the node-27 storage runbook.  JSON hashes use canonical compact sorted
UTF-8 plus one trailing newline, equivalent to ``jq -cS`` for these objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import jsonschema

from packages.common.evidence_io import (
    BoundedEvidenceError,
    read_bounded_bytes_no_follow,
    read_bounded_json_no_follow,
)
from packages.common.safe_fs import atomic_write_bytes_no_follow

SCHEMA_VERSION = "2.0"
ISSUE = 1069
PASS_VERDICT = "PASS_TASK_4_5"
HYPERTABLE_KEYS = ("hydro.river_timeseries", "met.forcing_station_timeseries")
MAX_SELECTED_BYTES = 8 * 1024**3
MIN_FREE_BYTES = 300 * 1024**3
EXPECTED_LAG_SECONDS = 604_800
EXPECTED_BOUND = 1
EXPECTED_TIMEOUT_SECONDS = 900
MAX_POST_MEASUREMENT_DRIFT_BYTES = 1024**2
MAX_PREFLIGHT_TO_ENFORCE_SECONDS = 60
MAX_JSON_ARTIFACT_BYTES = 16 * 1024**2
MAX_PLAN_DEPTH = 48
MAX_JSON_NODES = 250_000
MAX_JSON_ARRAY_ITEMS = 25_000
MAX_BINARY_ARTIFACT_BYTES = 1024**3
REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_RECEIPT_SCHEMA = REPO_ROOT / "schemas/timeseries_compression_receipt.schema.json"
CANONICAL_EVIDENCE_SCHEMA = REPO_ROOT / "schemas/timeseries_compression_live_evidence.schema.json"
CANONICAL_MIGRATION = REPO_ROOT / "db/migrations/000047_hypertable_compression_settings.sql"
EXPECTED_UNITS = (
    "nhms-node27-autopipe.timer",
    "nhms-node27-autopipe.service",
    "nhms-node27-timeseries-compression.timer",
    "nhms-node27-timeseries-compression.service",
)
PREFLIGHT_KEYS = frozenset(
    {
        "captured_at",
        "node",
        "repo_path",
        "mutation_head_sha",
        "worktree_clean",
        "database_identity",
        "container_state",
        "role",
        "env_mode",
        "write_guards_present",
        "autopipe_quiescent",
        "database_writes_quiescent",
        "conflicting_locks_absent",
        "units",
        "prior_autopipe_state",
    }
)
RECOVERY_TARGET: Mapping[str, str] = {
    "hypertable_schema": "hydro",
    "hypertable_name": "river_timeseries",
    "chunk_schema": "_timescaledb_internal",
    "chunk_name": "_hyper_3_7_chunk",
    "range_start": "2026-05-28T00:00:00Z",
    "range_end": "2026-06-04T00:00:00Z",
}
RECOVERY_RETURN_RELATION = "_timescaledb_internal._hyper_3_7_chunk"

# This constant is deliberately executable documentation: tests and the
# runbook pin the same capture contract without inventing a host-only format.
BUNDLE_CONTRACT: Mapping[str, str] = {
    "recovery.preflight": "JSON: separately authorized exact-chunk decompression preflight",
    "recovery.receipt": "JSON: exact-chunk decompression result, row parity, and chronology",
    "preflight.evidence": "JSON: captured mutation SHA, container and four-unit state facts",
    "preflight.schema_dump": "custom-format pg_dump file reference",
    "preflight.catalog_before": "JSON: canonical pre-migration catalog rows",
    "migration.catalog_after_first": "JSON: exact D3 catalog after first apply",
    "migration.catalog_after_second": "JSON: exact D3 catalog after second apply",
    "selection.post_dry_run": "JSON: timestamp/cutoff/free bytes and complete ordered candidates",
    "selection.pre_enforce": "distinct JSON captured <=60s before enforce invocation",
    "receipts.dry_run": "JSON: runner dry-run receipt",
    "receipts.enforce": "JSON: runner enforce receipt",
    "sizes.pre": "JSON: both-table pre size/count snapshot",
    "sizes.post": "JSON: both-table post size/count snapshot",
    "catalog.post": "JSON: exact post-run D3 settings and policy jobs",
    "benchmarks.evidence": "JSON: raw curve/MVT plans, samples, result identities",
    "cleanup.evidence": "JSON: restored autopipe and enabled/inactive compression timer",
}

INVOCATION_ARGV: Mapping[str, list[str]] = {
    "migration_apply": [
        "psql",
        "--set",
        "ON_ERROR_STOP=1",
        "--file",
        "db/migrations/000047_hypertable_compression_settings.sql",
    ],
    "recovery_decompress": [
        "psql",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        "SELECT decompress_" "chunk($1::regclass);",
    ],
    "compression_dry_run": [
        "scripts/node27_timeseries_compression_once.sh",
        "--receipt-path",
        "<receipt-path>",
    ],
    "compression_enforce": [
        "scripts/node27_timeseries_compression_once.sh",
        "--enforce",
        "--receipt-path",
        "<receipt-path>",
    ],
}


class EvidenceError(RuntimeError):
    """Fail-closed bundle, artifact, schema, or semantic error."""


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be an array")
    return value


def _require_exact_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    actual = set(value)
    if actual != keys:
        raise EvidenceError(
            f"{label} keys differ: missing={sorted(keys - actual)} extra={sorted(actual - keys)}"
        )


def _artifact_ref(
    value: Any,
    label: str,
    *,
    max_bytes: int = MAX_BINARY_ARTIFACT_BYTES,
) -> dict[str, Any]:
    ref, _ = _artifact_bytes(value, label, max_bytes=max_bytes)
    return ref


def _artifact_bytes(
    value: Any,
    label: str,
    *,
    max_bytes: int = MAX_BINARY_ARTIFACT_BYTES,
) -> tuple[dict[str, Any], bytes]:
    ref = _require_mapping(value, label)
    _require_exact_keys(ref, {"path", "sha256", "bytes"}, label)
    path = Path(str(ref["path"]))
    if not path.is_absolute():
        raise EvidenceError(f"{label}.path must be absolute")
    digest = str(ref["sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise EvidenceError(f"{label}.sha256 must be lowercase sha256")
    size = ref["bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise EvidenceError(f"{label}.bytes must be a non-negative integer")
    if size > max_bytes:
        raise EvidenceError(f"{label} exceeds the byte ceiling")
    try:
        raw = read_bounded_bytes_no_follow(path, max_bytes=max_bytes, label=label)
    except BoundedEvidenceError as error:
        raise EvidenceError(str(error)) from error
    if len(raw) != size or _sha256(raw) != digest:
        raise EvidenceError(f"{label} byte count or sha256 mismatch")
    return {"path": str(path), "sha256": digest, "bytes": size}, raw


def _json_artifact(
    value: Any,
    label: str,
    *,
    max_bytes: int = MAX_JSON_ARTIFACT_BYTES,
) -> tuple[dict[str, Any], Any]:
    ref_value = _require_mapping(value, label)
    declared_size = ref_value.get("bytes")
    if not isinstance(declared_size, int) or isinstance(declared_size, bool):
        raise EvidenceError(f"{label}.bytes must be a non-negative integer")
    if declared_size > max_bytes:
        raise EvidenceError(f"{label} exceeds the byte ceiling")
    try:
        raw, document = read_bounded_json_no_follow(
            Path(str(ref_value.get("path", ""))),
            max_bytes=max_bytes,
            label=label,
            max_depth=MAX_PLAN_DEPTH,
            max_nodes=MAX_JSON_NODES,
            max_array_items=MAX_JSON_ARRAY_ITEMS,
        )
    except BoundedEvidenceError as error:
        raise EvidenceError(str(error)) from error
    ref = _artifact_ref_from_raw(ref_value, label, raw)
    _reject_secrets(document, label)
    return ref, document


def _artifact_ref_from_raw(value: Any, label: str, raw: bytes) -> dict[str, Any]:
    """Validate a ref against bytes already read from its pinned descriptor."""

    ref = _require_mapping(value, label)
    _require_exact_keys(ref, {"path", "sha256", "bytes"}, label)
    path = Path(str(ref["path"]))
    if not path.is_absolute():
        raise EvidenceError(f"{label}.path must be absolute")
    digest = str(ref["sha256"])
    size = ref["bytes"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise EvidenceError(f"{label}.sha256 must be lowercase sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise EvidenceError(f"{label}.bytes must be a non-negative integer")
    if len(raw) != size or _sha256(raw) != digest:
        raise EvidenceError(f"{label} byte count or sha256 mismatch")
    return {"path": str(path), "sha256": digest, "bytes": size}


def _reject_secrets(value: Any, label: str) -> None:
    """Reject credential-bearing evidence instead of trying to redact it later."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
            if (
                normalized in {
                    "password",
                    "passwd",
                    "database_url",
                    "dsn",
                    "api_key",
                    "apikey",
                    "token",
                    "access_token",
                    "refresh_token",
                    "private_key",
                    "client_secret",
                    "authorization",
                    "auth_credential",
                    "credential",
                }
                or normalized.endswith(("_password", "_token", "_secret", "_api_key", "_private_key"))
            ):
                raise EvidenceError(f"{label} contains forbidden credential field")
            _reject_secrets(item, label)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item, label)
    elif isinstance(value, str) and re.search(
        r"(?i)(?:postgres(?:ql)?://[^/\s]*@|-----BEGIN [A-Z ]*PRIVATE KEY-----|bearer\s+[a-z0-9._~+/=-]+)",
        value,
    ):
        raise EvidenceError(f"{label} contains forbidden credential material")


def _git_blob_bytes(head_sha: str, relative_path: str, label: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{head_sha}:{relative_path}"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise EvidenceError(f"{label} cannot be bound to mutation SHA")
    return result.stdout


def _validate_reviewed_file_ref(
    value: Any,
    *,
    label: str,
    mutation_head_sha: str,
    relative_path: str,
) -> dict[str, Any]:
    expected_path = REPO_ROOT / relative_path
    ref = _artifact_ref(value, label, max_bytes=MAX_JSON_ARTIFACT_BYTES)
    if Path(ref["path"]) != expected_path:
        raise EvidenceError(f"{label} path is not the canonical checkout path")
    expected = _git_blob_bytes(mutation_head_sha, relative_path, label)
    if ref["bytes"] != len(expected) or ref["sha256"] != _sha256(expected):
        raise EvidenceError(f"{label} differs from the reviewed mutation-SHA blob")
    return ref


def _parse_utc(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise EvidenceError(f"{label} must carry a timezone")
    return parsed.astimezone(UTC)


def _validate_invocation_record(
    raw: Any,
    *,
    label: str,
    kind: str,
    mutation_head_sha: str,
    expected_binding: Mapping[str, Any],
) -> dict[str, Any]:
    record = _require_mapping(raw, label)
    _require_exact_keys(
        record,
        {
            "kind",
            "argv",
            "timeout_seconds",
            "started_at",
            "finished_at",
            "exit_code",
            "mutation_head_sha",
            "artifact_bindings",
        },
        label,
    )
    _reject_secrets(record, label)
    if (
        record["kind"] != kind
        or record["argv"] != INVOCATION_ARGV[kind]
        or record["timeout_seconds"] != EXPECTED_TIMEOUT_SECONDS
        or record["mutation_head_sha"] != mutation_head_sha
    ):
        raise EvidenceError(f"{label} command identity, timeout, or mutation SHA differs")
    started = _parse_utc(record["started_at"], f"{label}.started_at")
    finished = _parse_utc(record["finished_at"], f"{label}.finished_at")
    if (
        not started <= finished
        or (finished - started).total_seconds() > EXPECTED_TIMEOUT_SECONDS
        or record["exit_code"] != 0
    ):
        raise EvidenceError(f"{label} timing/exit does not prove bounded success")
    bindings = _require_mapping(record["artifact_bindings"], f"{label}.artifact_bindings")
    if dict(bindings) != dict(expected_binding):
        raise EvidenceError(f"{label} artifact association differs")
    return {
        **dict(record),
        "started_at_dt": started,
        "finished_at_dt": finished,
    }


def _selected_identity(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "hypertable_schema",
        "hypertable_name",
        "chunk_schema",
        "chunk_name",
        "range_start",
        "range_end",
    )
    try:
        return {key: descriptor[key] for key in keys}
    except KeyError as error:
        raise EvidenceError(f"selected descriptor missing {error.args[0]}") from error


def _load_receipt(
    ref_value: Any, label: str, receipt_schema: Mapping[str, Any]
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    ref, raw = _json_artifact(ref_value, label)
    receipt = _require_mapping(raw, label)
    try:
        jsonschema.Draft7Validator(receipt_schema, format_checker=jsonschema.FormatChecker()).validate(
            receipt
        )
    except jsonschema.ValidationError as error:
        raise EvidenceError(f"{label} fails runner receipt schema: {error.message}") from error
    return ref, receipt


def _validate_d3_catalog(raw: Any, label: str) -> None:
    catalog = _require_mapping(raw, label)
    _require_exact_keys(catalog, {"hypertables", "compression_settings", "policy_jobs"}, label)
    hypertables = _require_mapping(catalog["hypertables"], f"{label}.hypertables")
    if set(hypertables) != set(HYPERTABLE_KEYS) or not all(
        hypertables[key] is True for key in HYPERTABLE_KEYS
    ):
        raise EvidenceError(f"{label} must enable compression on exactly both hypertables")
    expected = [
        ["hydro", "river_timeseries", "run_id", 1, None, None, None],
        ["hydro", "river_timeseries", "river_network_version_id", 2, None, None, None],
        ["hydro", "river_timeseries", "river_segment_id", 3, None, None, None],
        ["hydro", "river_timeseries", "variable", None, 1, True, False],
        ["hydro", "river_timeseries", "valid_time", None, 2, True, False],
        ["met", "forcing_station_timeseries", "forcing_version_id", 1, None, None, None],
        ["met", "forcing_station_timeseries", "station_id", 2, None, None, None],
        ["met", "forcing_station_timeseries", "variable", None, 1, True, False],
        ["met", "forcing_station_timeseries", "valid_time", None, 2, True, False],
    ]
    settings = _require_list(catalog["compression_settings"], f"{label}.compression_settings")
    actual = []
    for index, row_value in enumerate(settings):
        row = _require_mapping(row_value, f"{label}.compression_settings[{index}]")
        _require_exact_keys(
            row,
            {
                "hypertable_schema",
                "hypertable_name",
                "attname",
                "segmentby_column_index",
                "orderby_column_index",
                "orderby_asc",
                "orderby_nullsfirst",
            },
            f"{label}.compression_settings[{index}]",
        )
        actual.append(
            [
                row["hypertable_schema"],
                row["hypertable_name"],
                row["attname"],
                row["segmentby_column_index"],
                row["orderby_column_index"],
                row["orderby_asc"],
                row["orderby_nullsfirst"],
            ]
        )
    if actual != expected or catalog["policy_jobs"] != []:
        raise EvidenceError(f"{label} does not match exact D3 settings/no-policy contract")


def _validate_pre_migration_catalog(raw: Any, label: str) -> None:
    catalog = _require_mapping(raw, label)
    _require_exact_keys(catalog, {"hypertables", "compression_settings", "policy_jobs"}, label)
    hypertables = _require_mapping(catalog["hypertables"], f"{label}.hypertables")
    if set(hypertables) != set(HYPERTABLE_KEYS) or any(
        value is not False for value in hypertables.values()
    ):
        raise EvidenceError(f"{label} does not prove the exact pre-migration catalog")
    if catalog["compression_settings"] != [] or catalog["policy_jobs"] != []:
        raise EvidenceError(f"{label} contains unexpected pre-migration settings/jobs")


def _validate_dump_listing(raw: Any, *, dump_ref: Mapping[str, Any]) -> dict[str, Any]:
    listing = _require_mapping(raw, "preflight.schema_dump_list document")
    _require_exact_keys(
        listing,
        {"dump_sha256", "argv", "exit_code", "entries"},
        "preflight.schema_dump_list document",
    )
    entries = _require_list(listing["entries"], "preflight.schema_dump_list.entries")
    if (
        listing["dump_sha256"] != dump_ref["sha256"]
        or listing["argv"] != ["pg_restore", "--list", "<schema-dump-path>"]
        or listing["exit_code"] != 0
        or not entries
        or any(not isinstance(item, str) or not item for item in entries)
        or not all(any(table.split(".")[1] in item for item in entries) for table in HYPERTABLE_KEYS)
    ):
        raise EvidenceError("schema forensic dump/list identity is not verifiable")
    return dict(listing)


def _validate_preflight(raw: Any, mutation_head_sha: str) -> dict[str, Any]:
    preflight = _require_mapping(raw, "preflight.evidence document")
    _require_exact_keys(
        preflight,
        set(PREFLIGHT_KEYS),
        "preflight.evidence document",
    )
    captured_at = _parse_utc(preflight["captured_at"], "preflight.captured_at")
    if (
        preflight["node"] != "node-27"
        or preflight["mutation_head_sha"] != mutation_head_sha
        or preflight["worktree_clean"] is not True
        or preflight["env_mode"] != "0600"
        or preflight["write_guards_present"] is not True
        or preflight["autopipe_quiescent"] is not True
        or preflight["database_writes_quiescent"] is not True
        or preflight["conflicting_locks_absent"] is not True
    ):
        raise EvidenceError("preflight.evidence does not prove the mutation-head boundary")
    container = _require_mapping(preflight["container_state"], "preflight.container_state")
    _require_exact_keys(container, {"name", "container_id", "image", "status", "running"}, "preflight.container_state")
    if (
        container["name"] != "nhms-db"
        or container["running"] is not True
        or not all(isinstance(container[key], str) and container[key] for key in ("container_id", "image", "status"))
    ):
        raise EvidenceError("preflight container state is not the running nhms-db instance")
    units = _require_mapping(preflight["units"], "preflight.units")
    if set(units) != set(EXPECTED_UNITS):
        raise EvidenceError("preflight must capture exactly the four governed units")
    unit_summary: dict[str, Any] = {}
    for unit_name in EXPECTED_UNITS:
        unit = _require_mapping(units[unit_name], f"preflight.units.{unit_name}")
        _require_exact_keys(
            unit,
            {"enabled", "active", "sub", "result", "main_pid", "journal"},
            f"preflight.units.{unit_name}",
        )
        if not all(isinstance(unit[key], str) and unit[key] for key in ("enabled", "active", "sub", "result")):
            raise EvidenceError(f"preflight unit {unit_name} has incomplete state fields")
        if not isinstance(unit["main_pid"], int) or isinstance(unit["main_pid"], bool) or unit["main_pid"] < 0:
            raise EvidenceError(f"preflight unit {unit_name} has invalid MainPID")
        journal_ref = _artifact_ref(unit["journal"], f"preflight.units.{unit_name}.journal")
        unit_summary[unit_name] = {
            "enabled": unit["enabled"],
            "active": unit["active"],
            "sub": unit["sub"],
            "result": unit["result"],
            "main_pid": unit["main_pid"],
            "journal": journal_ref,
        }
    for unit_name in EXPECTED_UNITS:
        unit = units[unit_name]
        if unit_name.endswith(".service") and unit["main_pid"] != 0:
            raise EvidenceError(f"preflight service {unit_name} is not quiescent")
    for unit_name in EXPECTED_UNITS[2:]:
        unit = units[unit_name]
        if unit["active"] != "inactive" or unit["main_pid"] != 0:
            raise EvidenceError(f"preflight compression unit {unit_name} must remain inactive")
    prior_autopipe = _require_mapping(
        preflight["prior_autopipe_state"], "preflight.prior_autopipe_state"
    )
    _require_exact_keys(
        prior_autopipe,
        {"enabled", "active", "sub", "result"},
        "preflight.prior_autopipe_state",
    )
    if not all(isinstance(value, str) and value for value in prior_autopipe.values()):
        raise EvidenceError("preflight prior autopipe state is incomplete")
    database = _require_mapping(preflight["database_identity"], "database_identity")
    if database.get("dbname") != "nhms" or database.get("instance") != "node27-primary-pg15":
        raise EvidenceError("preflight database identity is not the node-27 nhms primary")
    role = _require_mapping(preflight["role"], "preflight.role")
    exact_role = {
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
    if role != exact_role:
        raise EvidenceError("preflight role facts/authority boundary differ from the fixture")
    return {
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "mutation_head_sha": mutation_head_sha,
        "container_state": dict(container),
        "units": unit_summary,
        "prior_autopipe_state": dict(prior_autopipe),
    }


def _validate_recovery(
    preflight_raw: Any,
    receipt_raw: Any,
    *,
    mutation_head_sha: str,
    database_identity: Mapping[str, Any],
    compression_preflight_captured_at: datetime,
) -> dict[str, Any]:
    recovery_preflight = _require_mapping(
        preflight_raw, "recovery.preflight document"
    )
    _require_exact_keys(
        recovery_preflight,
        set(PREFLIGHT_KEYS)
        | {
            "target",
            "free_bytes",
            "before_compressed",
            "before_row_count",
        },
        "recovery.preflight document",
    )
    recovery_safety = {
        key: recovery_preflight[key]
        for key in PREFLIGHT_KEYS
    }
    recovery_safety_summary = _validate_preflight(
        recovery_safety, mutation_head_sha
    )
    recovery_receipt = _require_mapping(receipt_raw, "recovery.receipt document")
    _require_exact_keys(
        recovery_receipt,
        {
            "started_at",
            "finished_at",
            "node",
            "mutation_head_sha",
            "database_identity",
            "target",
            "exit_code",
            "decompress_return_relation",
            "after_compressed",
            "after_row_count",
        },
        "recovery.receipt document",
    )
    preflight_target = _require_mapping(
        recovery_preflight["target"], "recovery.preflight.target"
    )
    receipt_target = _require_mapping(
        recovery_receipt["target"], "recovery.receipt.target"
    )
    target_keys = set(RECOVERY_TARGET)
    _require_exact_keys(preflight_target, target_keys, "recovery.preflight.target")
    _require_exact_keys(receipt_target, target_keys, "recovery.receipt.target")
    if dict(preflight_target) != dict(RECOVERY_TARGET) or dict(receipt_target) != dict(
        RECOVERY_TARGET
    ):
        raise EvidenceError("recovery target differs from the exact authorized chunk")
    if (
        recovery_preflight["node"] != "node-27"
        or recovery_receipt["node"] != "node-27"
        or recovery_preflight["mutation_head_sha"] != mutation_head_sha
        or recovery_receipt["mutation_head_sha"] != mutation_head_sha
    ):
        raise EvidenceError("recovery node/mutation_head_sha boundary differs")
    if (
        recovery_preflight["database_identity"] != database_identity
        or recovery_receipt["database_identity"] != database_identity
    ):
        raise EvidenceError("recovery database identity differs from compression preflight")
    free_bytes = recovery_preflight["free_bytes"]
    if (
        not isinstance(free_bytes, int)
        or isinstance(free_bytes, bool)
        or free_bytes < MIN_FREE_BYTES
    ):
        raise EvidenceError("recovery free-space headroom is below 300 GiB")
    before_rows = recovery_preflight["before_row_count"]
    after_rows = recovery_receipt["after_row_count"]
    if (
        recovery_preflight["before_compressed"] is not True
        or recovery_receipt["after_compressed"] is not False
    ):
        raise EvidenceError("recovery does not prove compressed-to-decompressed state")
    if (
        not isinstance(before_rows, int)
        or isinstance(before_rows, bool)
        or before_rows < 1
        or not isinstance(after_rows, int)
        or isinstance(after_rows, bool)
        or after_rows < 1
        or before_rows != after_rows
    ):
        raise EvidenceError("recovery row parity failed")
    if (
        recovery_receipt["exit_code"] != 0
        or recovery_receipt["decompress_return_relation"]
        != RECOVERY_RETURN_RELATION
    ):
        raise EvidenceError("recovery decompression result is not the exact target relation")
    preflight_at = _parse_utc(
        recovery_safety_summary["captured_at"], "recovery.preflight.captured_at"
    )
    started_at = _parse_utc(
        recovery_receipt["started_at"], "recovery.receipt.started_at"
    )
    finished_at = _parse_utc(
        recovery_receipt["finished_at"], "recovery.receipt.finished_at"
    )
    if not (
        preflight_at <= started_at <= finished_at <= compression_preflight_captured_at
    ):
        raise EvidenceError(
            "recovery chronology must precede the compression preflight"
        )
    return {
        "node": "node-27",
        "mutation_head_sha": mutation_head_sha,
        "target": dict(RECOVERY_TARGET),
        "preflight_captured_at": preflight_at.isoformat().replace("+00:00", "Z"),
        "decompress_started_at": started_at.isoformat().replace("+00:00", "Z"),
        "decompress_finished_at": finished_at.isoformat().replace("+00:00", "Z"),
        "compression_preflight_captured_at": compression_preflight_captured_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "free_bytes_before": free_bytes,
        "before_compressed": True,
        "after_compressed": False,
        "before_row_count": before_rows,
        "after_row_count": after_rows,
        "row_parity": True,
        "decompress_return_relation": RECOVERY_RETURN_RELATION,
        "exit_code": 0,
    }


def _table_snapshot(
    raw: Any,
    label: str,
    *,
    mutation_head_sha: str,
    expected_phase: str,
) -> dict[str, Any]:
    snapshot = _require_mapping(raw, label)
    _require_exact_keys(
        snapshot,
        {"captured_at", "snapshot_id", "phase", "mutation_head_sha", "tables"},
        label,
    )
    captured_at = _parse_utc(snapshot["captured_at"], f"{label}.captured_at")
    if (
        snapshot["phase"] != expected_phase
        or snapshot["mutation_head_sha"] != mutation_head_sha
        or not isinstance(snapshot["snapshot_id"], str)
        or not snapshot["snapshot_id"]
    ):
        raise EvidenceError(f"{label} snapshot identity differs")
    tables = _require_mapping(snapshot["tables"], f"{label}.tables")
    if set(tables) != set(HYPERTABLE_KEYS):
        raise EvidenceError(f"{label} must contain exactly both hypertables")
    for key in HYPERTABLE_KEYS:
        row = _require_mapping(tables[key], f"{label}.{key}")
        _require_exact_keys(
            row,
            {
                "hypertable_size",
                "parent_relation_size",
                "compressed_chunks",
                "uncompressed_chunks",
                "compressed_relations",
            },
            f"{label}.{key}",
        )
        for field in (
            "hypertable_size",
            "parent_relation_size",
            "compressed_chunks",
            "uncompressed_chunks",
        ):
            if not isinstance(row[field], int) or isinstance(row[field], bool) or row[field] < 0:
                raise EvidenceError(f"{label}.{key}.{field} must be non-negative integer")
        _require_list(row["compressed_relations"], f"{label}.{key}.compressed_relations")
        for index, relation_value in enumerate(row["compressed_relations"]):
            relation = _require_mapping(
                relation_value, f"{label}.{key}.compressed_relations[{index}]"
            )
            _require_exact_keys(
                relation,
                {"origin_chunk_schema", "origin_chunk_name", "schema", "name", "bytes"},
                f"{label}.{key}.compressed_relations[{index}]",
            )
            if not isinstance(relation["bytes"], int) or relation["bytes"] < 0:
                raise EvidenceError(f"{label}.{key}.compressed_relations[{index}].bytes invalid")
    return {
        "captured_at_dt": captured_at,
        "snapshot_id": snapshot["snapshot_id"],
        "tables": tables,
    }


def _validate_selection_snapshot(raw: Any, label: str) -> dict[str, Any]:
    snapshot = _require_mapping(raw, f"{label} document")
    _require_exact_keys(
        snapshot,
        {"observed_at", "cutoff", "free_bytes", "candidates", "selected"},
        f"{label} document",
    )
    observed_at = _parse_utc(snapshot["observed_at"], f"{label}.observed_at")
    cutoff = _parse_utc(snapshot["cutoff"], f"{label}.cutoff")
    derived_cutoff = observed_at - timedelta(seconds=EXPECTED_LAG_SECONDS)
    if cutoff != derived_cutoff:
        raise EvidenceError(f"{label} cutoff is not observed_at minus the authorized lag")
    free_bytes = snapshot["free_bytes"]
    if not isinstance(free_bytes, int) or isinstance(free_bytes, bool) or free_bytes < MIN_FREE_BYTES:
        raise EvidenceError(f"{label} free-space headroom is below 300 GiB")
    candidates_raw = _require_list(snapshot["candidates"], f"{label}.candidates")
    selected_raw = _require_list(snapshot["selected"], f"{label}.selected")
    if not candidates_raw or len(selected_raw) != EXPECTED_BOUND:
        raise EvidenceError(f"{label} must contain full candidates and exactly bound-1 selected")
    candidates: list[dict[str, Any]] = []
    for index, value in enumerate(candidates_raw):
        candidate = _require_mapping(value, f"{label}.candidates[{index}]")
        _require_exact_keys(
            candidate,
            {
                "hypertable_schema",
                "hypertable_name",
                "chunk_schema",
                "chunk_name",
                "range_start",
                "range_end",
                "is_compressed",
                "before_bytes",
            },
            f"{label}.candidates[{index}]",
        )
        key = f"{candidate['hypertable_schema']}.{candidate['hypertable_name']}"
        if key not in HYPERTABLE_KEYS or candidate["is_compressed"] is not False:
            raise EvidenceError(f"{label} candidate is outside the uncompressed D3 allowlist")
        before_bytes = candidate["before_bytes"]
        if not isinstance(before_bytes, int) or isinstance(before_bytes, bool) or before_bytes < 1:
            raise EvidenceError(f"{label} candidate before_bytes must be positive")
        range_start = _parse_utc(candidate["range_start"], f"{label} candidate range_start")
        range_end = _parse_utc(candidate["range_end"], f"{label} candidate range_end")
        if range_start >= range_end or range_end >= cutoff:
            raise EvidenceError(f"{label} candidate is not strictly terminal")
        candidates.append(dict(candidate))
    ordered = sorted(
        candidates,
        key=lambda item: (
            item["hypertable_schema"],
            item["hypertable_name"],
            _parse_utc(item["range_end"], "candidate range_end"),
            item["chunk_schema"],
            item["chunk_name"],
        ),
    )
    if candidates != ordered:
        raise EvidenceError(f"{label} candidates are not in complete stable runner order")
    selected = [_require_mapping(value, f"{label}.selected") for value in selected_raw]
    if [dict(value) for value in selected] != candidates[:EXPECTED_BOUND]:
        raise EvidenceError(f"{label} selected is not the bound-1 prefix of candidates")
    selected_row = candidates[0]
    if (
        selected_row["hypertable_schema"] != "hydro"
        or selected_row["hypertable_name"] != "river_timeseries"
        or selected_row["before_bytes"] > MAX_SELECTED_BYTES
    ):
        raise EvidenceError(f"{label} selected row violates hydro/bound/8-GiB authorization")
    cutoff_margin = int(
        (cutoff - _parse_utc(selected_row["range_end"], f"{label} selected range_end")).total_seconds()
    )
    if cutoff_margin < 600:
        raise EvidenceError(f"{label} selected chunk is within ten minutes of cutoff")
    identities = [_selected_identity(value) for value in selected]
    return {
        "observed_at_dt": observed_at,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "free_bytes": free_bytes,
        "candidates": candidates,
        "selected": [dict(value) for value in selected],
        "identities": identities,
        "candidates_sha256": _sha256(_canonical_json_bytes(candidates)),
        "selector_sha256": _sha256(_canonical_json_bytes(identities)),
        "cutoff_margin_seconds": cutoff_margin,
    }


def _plan_binds_selected_decompress(
    plan: Any, *, selected_relation_names: set[str]
) -> bool:
    """Require provider and relation in one Custom Scan node/direct subtree."""

    stack = [plan]
    lowered_names = {name.lower() for name in selected_relation_names}
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
            continue
        if not isinstance(value, Mapping):
            continue
        node_type = str(value.get("Node Type", "")).lower()
        provider = str(value.get("Custom Plan Provider", "")).lower()
        if node_type == "custom scan" and "decompresschunk" in provider:
            # TimescaleDB versions expose the origin/sibling in different
            # fields. Restrict the search to this node and its direct Plans,
            # never unrelated branches elsewhere in the EXPLAIN tree.
            local_values = [
                item
                for key, item in value.items()
                if key != "Plans"
            ]
            direct = value.get("Plans", [])
            if isinstance(direct, list):
                local_values.extend(direct)
            rendered = json.dumps(local_values, sort_keys=True).lower()
            if any(name in rendered for name in lowered_names):
                return True
        stack.extend(value.values())
    return False


def _stats(samples_value: Any, label: str) -> tuple[list[float], float, float]:
    samples = _require_list(samples_value, label)
    if len(samples) != 7 or any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < 0
        for value in samples
    ):
        raise EvidenceError(f"{label} must contain seven finite non-negative samples")
    normalized = [float(value) for value in samples]
    ordered = sorted(normalized)
    return normalized, ordered[3], ordered[math.ceil(0.95 * len(ordered)) - 1]


def _result_bytes(name: str, payload: Any) -> tuple[bytes, int]:
    if name == "curve":
        rows = _require_list(payload, "curve result_payload")
        return _canonical_json_bytes(rows), len(rows)
    if name == "mvt":
        if not isinstance(payload, str) or re.fullmatch(r"(?:[0-9a-fA-F]{2})+", payload) is None:
            raise EvidenceError("mvt result_payload must be a non-empty even-length hex string")
        return bytes.fromhex(payload), 1
    raise EvidenceError(f"unknown benchmark query {name!r}")


def _validate_measurement(value: Any, label: str) -> dict[str, Any]:
    measurement = _require_mapping(value, label)
    _require_exact_keys(
        measurement,
        {"plan", "planning_ms", "execution_ms", "shared_hit_blocks", "shared_read_blocks"},
        label,
    )
    if not isinstance(measurement["plan"], (Mapping, list)) or not measurement["plan"]:
        raise EvidenceError(f"{label}.plan must preserve the full EXPLAIN JSON plan")
    for field in ("planning_ms", "execution_ms"):
        value_ = measurement[field]
        if (
            not isinstance(value_, (int, float))
            or isinstance(value_, bool)
            or not math.isfinite(float(value_))
            or value_ < 0
        ):
            raise EvidenceError(f"{label}.{field} must be finite and non-negative")
    for field in ("shared_hit_blocks", "shared_read_blocks"):
        value_ = measurement[field]
        if not isinstance(value_, int) or isinstance(value_, bool) or value_ < 0:
            raise EvidenceError(f"{label}.{field} must be a non-negative integer")
    return dict(measurement)


def _validate_phase(
    value: Any,
    *,
    query_name: str,
    phase_name: str,
    selected_relation_names: set[str],
) -> dict[str, Any]:
    phase = _require_mapping(value, f"benchmark {query_name} {phase_name}")
    _require_exact_keys(
        phase,
        {
            "result_payload",
            "result_sha256",
            "rows",
            "bytes",
            "cache_class",
            "cold",
            "warmups",
            "measurements",
            "activity_samples",
            "execution_bounds",
        },
        f"benchmark {query_name} {phase_name}",
    )
    result_bytes, result_rows = _result_bytes(query_name, phase["result_payload"])
    if (
        _sha256(result_bytes) != phase["result_sha256"]
        or len(result_bytes) != phase["bytes"]
        or result_rows != phase["rows"]
    ):
        raise EvidenceError(f"benchmark {query_name} {phase_name} result hash/count mismatch")
    cold = _validate_measurement(phase["cold"], f"benchmark {query_name} {phase_name}.cold")
    warmups = [
        _validate_measurement(item, f"benchmark {query_name} {phase_name}.warmups[{index}]")
        for index, item in enumerate(_require_list(phase["warmups"], "benchmark warmups"))
    ]
    if not 2 <= len(warmups) <= 5:
        raise EvidenceError(f"benchmark {query_name} {phase_name} needs 2-5 warmups")
    measurements = [
        _validate_measurement(item, f"benchmark {query_name} {phase_name}.measurements[{index}]")
        for index, item in enumerate(
            _require_list(phase["measurements"], "benchmark measurements")
        )
    ]
    if len(measurements) != 7:
        raise EvidenceError(f"benchmark {query_name} {phase_name} needs seven measurements")
    last_warmup_reads = warmups[-1]["shared_read_blocks"]
    derived_cache_class = "warm-cache" if last_warmup_reads == 0 else "mixed-cache"
    if derived_cache_class == "mixed-cache" and len(warmups) != 5:
        raise EvidenceError(f"benchmark {query_name} {phase_name} stopped warmups before five")
    if phase["cache_class"] != derived_cache_class:
        raise EvidenceError(f"benchmark {query_name} {phase_name} cache class mismatch")
    activities = _require_list(phase["activity_samples"], "benchmark activity_samples")
    expected_stages = [
        "before_cold",
        "after_cold",
        "before_measurements",
        "mid_measurements",
        "after_result",
    ]
    if len(activities) != len(expected_stages):
        raise EvidenceError(f"benchmark {query_name} {phase_name} lacks activity checkpoints")
    normalized_activities: list[dict[str, Any]] = []
    for index, value_ in enumerate(activities):
        activity = _require_mapping(value_, f"benchmark activity[{index}]")
        _require_exact_keys(
            activity,
            {"captured_at", "stage", "sessions", "material_load_stable"},
            f"benchmark activity[{index}]",
        )
        _parse_utc(activity["captured_at"], f"benchmark activity[{index}].captured_at")
        if (
            activity["stage"] != expected_stages[index]
            or activity["material_load_stable"] is not True
        ):
            raise EvidenceError(f"benchmark {query_name} {phase_name} has load drift")
        sessions = _require_list(activity["sessions"], f"benchmark activity[{index}].sessions")
        for session_index, session_value in enumerate(sessions):
            session = _require_mapping(
                session_value, f"benchmark activity[{index}].sessions[{session_index}]"
            )
            _require_exact_keys(
                session,
                {
                    "pid",
                    "backend_start",
                    "xact_start",
                    "query_start",
                    "state",
                    "wait_event_type",
                    "query_signature",
                },
                f"benchmark activity[{index}].sessions[{session_index}]",
            )
        normalized_activities.append(dict(activity))
    if any(
        item["sessions"] != normalized_activities[0]["sessions"]
        for item in normalized_activities[1:]
    ):
        raise EvidenceError(f"benchmark {query_name} {phase_name} has session-identity drift")
    bounds = _require_mapping(
        phase["execution_bounds"], f"benchmark {query_name} {phase_name}.execution_bounds"
    )
    _require_exact_keys(
        bounds,
        {
            "statement_timeout_ms",
            "lock_timeout_ms",
            "phase_timeout_seconds",
            "started_at",
            "finished_at",
        },
        f"benchmark {query_name} {phase_name}.execution_bounds",
    )
    started_at = _parse_utc(bounds["started_at"], "benchmark phase started_at")
    finished_at = _parse_utc(bounds["finished_at"], "benchmark phase finished_at")
    if (
        bounds["statement_timeout_ms"] != 60_000
        or bounds["lock_timeout_ms"] != 5_000
        or bounds["phase_timeout_seconds"] != EXPECTED_TIMEOUT_SECONDS
        or not started_at <= finished_at
        or (finished_at - started_at).total_seconds() > EXPECTED_TIMEOUT_SECONDS
    ):
        raise EvidenceError(f"benchmark {query_name} {phase_name} execution bounds differ")
    if query_name == "curve" and phase["rows"] < 1:
        raise EvidenceError("benchmark curve must return at least one row")
    if phase_name == "before":
        for index, measurement in enumerate(measurements):
            if _plan_binds_selected_decompress(
                measurement["plan"], selected_relation_names=selected_relation_names
            ):
                raise EvidenceError(
                    f"benchmark {query_name} before measurement {index} already uses selected DecompressChunk"
                )
    else:
        for index, measurement in enumerate(measurements):
            if not _plan_binds_selected_decompress(
                measurement["plan"], selected_relation_names=selected_relation_names
            ):
                raise EvidenceError(
                    f"benchmark {query_name} after measurement {index} lacks selected DecompressChunk"
                )
    samples = [float(measurement["execution_ms"]) for measurement in measurements]
    _, median, p95 = _stats(samples, f"benchmark {query_name} {phase_name} samples")
    return {
        "result_payload": phase["result_payload"],
        "result_sha256": phase["result_sha256"],
        "rows": phase["rows"],
        "bytes": phase["bytes"],
        "cache_class": derived_cache_class,
        "cold": cold,
        "warmups": warmups,
        "measurements": measurements,
        "activity_samples": normalized_activities,
        "execution_bounds": dict(bounds),
        "samples_ms": samples,
        "median_ms": median,
        "p95_ms": p95,
    }


def _validate_benchmarks(
    raw: Any,
    selected: Mapping[str, Any],
    *,
    selected_relation_names: set[str],
    mutation_head_sha: str,
) -> list[dict[str, Any]]:
    benchmark = _require_mapping(raw, "benchmarks.evidence document")
    queries = _require_list(benchmark.get("queries"), "benchmarks.queries")
    if [query.get("name") for query in queries if isinstance(query, Mapping)] != ["curve", "mvt"]:
        raise EvidenceError("benchmarks must contain curve then mvt")
    output: list[dict[str, Any]] = []
    for query_value in queries:
        query = _require_mapping(query_value, "benchmark query")
        _require_exact_keys(
            query,
            {
                "name",
                "request",
                "source_refs",
                "query_sha256",
                "query_text",
                "binding",
                "before",
                "after",
            },
            f"benchmark {query.get('name')}",
        )
        if query["name"] == "curve":
            source_paths = ["packages/common/forecast_store.py"]
            required_parameter_names = {
                "basin_version_id",
                "river_segment_id",
                "river_network_version_id",
                "issue_time",
                "start_time",
                "end_time",
            }
            required_query_tokens = {
                "FROM hydro.river_timeseries",
                "JOIN hydro.hydro_run",
                "rt.basin_version_id",
                "rt.river_segment_id",
                "rt.river_network_version_id",
                "rt.variable = 'q_down'",
                "h.run_type = 'forecast'",
                "h.cycle_time",
                "rt.valid_time",
            }
        else:
            source_paths = ["services/tiles/mvt.py", "apps/api/routes/hydro_display.py"]
            required_parameter_keys = {
                "run_id",
                "basin_version_id",
                "river_network_version_id",
                "variable",
                "valid_time",
                "z",
                "x",
                "y",
                "feature_limit",
                "feature_coordinate_limit",
                "collection_coordinate_limit",
                "max_coordinate_dimensions",
                "extent",
                "buffer",
                "simplification_tolerance_m",
            }
            required_query_tokens = {
                "FROM hydro.river_timeseries",
                "JOIN core.river_segment",
                ":run_id",
                ":basin_version_id",
                ":river_network_version_id",
                ":variable",
                ":valid_time",
                "ST_TileEnvelope",
            }
        raw_source_refs = _require_list(query["source_refs"], "source_refs")
        if len(raw_source_refs) != len(source_paths):
            raise EvidenceError(f"benchmark {query['name']} source set differs from production owners")
        source_refs = [
            _validate_reviewed_file_ref(
                value,
                label=f"benchmark {query['name']} source[{index}]",
                mutation_head_sha=mutation_head_sha,
                relative_path=source_paths[index],
            )
            for index, value in enumerate(raw_source_refs)
        ]
        binding = _require_mapping(query["binding"], f"benchmark {query['name']} binding")
        request = _require_mapping(query["request"], f"benchmark {query['name']} request")
        if query["name"] == "curve":
            from scripts.node27_timeseries_compression_benchmark import (
                _curve_query_and_binding,
                _json_value,
            )

            _require_exact_keys(
                request,
                {
                    "basin_version_id",
                    "river_segment_id",
                    "river_network_version_id",
                    "issue_time",
                    "end_time",
                    "scenario",
                },
                "benchmark curve request",
            )
            expected_query, expected_names, expected_parameters = _curve_query_and_binding(
                basin_version_id=str(request["basin_version_id"]),
                river_segment_id=str(request["river_segment_id"]),
                river_network_version_id=str(request["river_network_version_id"]),
                issue_time=_parse_utc(request["issue_time"], "curve request issue_time"),
                end_time=_parse_utc(request["end_time"], "curve request end_time"),
                scenario=str(request["scenario"]),
            )
            _require_exact_keys(
                binding,
                {"parameter_names", "bound_parameters"},
                "benchmark curve binding",
            )
            parameter_names = _require_list(
                binding["parameter_names"], "benchmark curve parameter_names"
            )
            bound_parameters = _require_list(
                binding["bound_parameters"], "benchmark curve bound_parameters"
            )
            placeholder_count = len(re.findall(r"(?<!%)%s", str(query["query_text"])))
            if (
                len(parameter_names) != placeholder_count
                or len(bound_parameters) != placeholder_count
                or not required_parameter_names <= set(parameter_names)
            ):
                raise EvidenceError("benchmark curve positional binding does not match %s count")
            if (
                query["query_text"] != expected_query
                or parameter_names != expected_names
                or _canonical_json_bytes(bound_parameters)
                != _canonical_json_bytes(_json_value(list(expected_parameters)))
            ):
                raise EvidenceError("benchmark curve query/binding differs from the public production owner")
            bound = dict(zip(parameter_names, bound_parameters, strict=True))
            request_start = _parse_utc(bound["start_time"], "benchmark curve start_time")
            request_end = _parse_utc(bound["end_time"], "benchmark curve end_time")
            selected_start = _parse_utc(selected["range_start"], "selected range_start")
            selected_end = _parse_utc(selected["range_end"], "selected range_end")
            if not (
                selected_start <= request_start < request_end <= selected_end
                and bound["basin_version_id"]
                and bound["river_segment_id"]
                and bound["river_network_version_id"]
            ):
                raise EvidenceError("benchmark curve request is not bound to the selected chunk range")
        else:
            from apps.api.routes.hydro_display import _postgis_tile_params
            from services.tiles.mvt import postgis_tile_sql

            _require_exact_keys(
                request,
                {
                    "run_id",
                    "basin_version_id",
                    "river_network_version_id",
                    "valid_time",
                    "z",
                    "x",
                    "y",
                },
                "benchmark mvt request",
            )
            if set(binding) != required_parameter_keys:
                raise EvidenceError("benchmark mvt binding is not the exact production parameter map")
            expected_binding = _postgis_tile_params(
                {
                    "run_id": request["run_id"],
                    "basin_version_id": request["basin_version_id"],
                    "river_network_version_id": request["river_network_version_id"],
                    "variable": "q_down",
                    "valid_time": _parse_utc(request["valid_time"], "mvt request valid_time"),
                },
                z=int(request["z"]),
                x=int(request["x"]),
                y=int(request["y"]),
            )
            if query["query_text"] != postgis_tile_sql("hydro") or _canonical_json_bytes(
                dict(binding)
            ) != _canonical_json_bytes(_json_value(dict(expected_binding))):
                raise EvidenceError("benchmark mvt query/binding differs from the public production owner")
            expected_mvt_values = {
                "variable": "q_down",
                "z": 9,
                "feature_limit": 10_000,
                "feature_coordinate_limit": 50_000,
                "collection_coordinate_limit": 50_000,
                "max_coordinate_dimensions": 3,
                "extent": 4096,
                "buffer": 64,
            }
            if any(binding[key] != value for key, value in expected_mvt_values.items()):
                raise EvidenceError("benchmark mvt budget/identity binding differs from production")
            valid_time = _parse_utc(binding["valid_time"], "benchmark mvt valid_time")
            if not (
                _parse_utc(selected["range_start"], "selected range_start")
                <= valid_time
                < _parse_utc(selected["range_end"], "selected range_end")
            ):
                raise EvidenceError("benchmark mvt request is not bound to the selected chunk range")
            z = binding["z"]
            expected_tolerance = min(
                256.0,
                max(0.5, ((40_075_016.68557849 / float(1 << z)) / 4096.0) / 2.0),
            )
            if not math.isclose(
                float(binding["simplification_tolerance_m"]),
                expected_tolerance,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise EvidenceError("benchmark mvt simplification binding differs from production")
        if re.fullmatch(r"[0-9a-f]{64}", str(query["query_sha256"])) is None:
            raise EvidenceError("benchmark query_sha256 is invalid")
        if not isinstance(query["query_text"], str) or not query["query_text"]:
            raise EvidenceError("benchmark query_text must be non-empty")
        if not all(token in query["query_text"] for token in required_query_tokens):
            raise EvidenceError(f"benchmark {query['name']} query is not the production SQL shape")
        if _sha256(query["query_text"].encode("utf-8")) != query["query_sha256"]:
            raise EvidenceError(f"benchmark {query['name']} query hash mismatch")
        before = _validate_phase(
            query["before"],
            query_name=str(query["name"]),
            phase_name="before",
            selected_relation_names=selected_relation_names,
        )
        after = _validate_phase(
            query["after"],
            query_name=str(query["name"]),
            phase_name="after",
            selected_relation_names=selected_relation_names,
        )
        for field in ("result_sha256", "rows", "bytes"):
            if before[field] != after[field]:
                raise EvidenceError(f"benchmark {query['name']} changed {field}")
        if before["cache_class"] != after["cache_class"]:
            raise EvidenceError("benchmark before/after cache classes differ")
        before_median, before_p95 = before["median_ms"], before["p95_ms"]
        after_median, after_p95 = after["median_ms"], after["p95_ms"]
        median_threshold = max(1.5 * before_median, before_median + 100.0)
        p95_threshold = max(2.0 * before_p95, before_p95 + 250.0)
        if after_median > median_threshold or after_p95 > p95_threshold:
            raise EvidenceError(f"benchmark {query['name']} exceeds timing threshold")
        output.append(
            {
                "name": query["name"],
                "source_refs": source_refs,
                "query_sha256": query["query_sha256"],
                "request": dict(request),
                "binding": dict(binding),
                "result_sha256": before["result_sha256"],
                "rows": before["rows"],
                "bytes": before["bytes"],
                "cache_class": before["cache_class"],
                "before_capture": before,
                "after_capture": after,
                "before_median_ms": before_median,
                "after_median_ms": after_median,
                "median_threshold_ms": median_threshold,
                "before_p95_ms": before_p95,
                "after_p95_ms": after_p95,
                "p95_threshold_ms": p95_threshold,
                "decompress_chunk_plan_bound": True,
            }
        )
    return output


def _validate_cleanup(
    raw: Any,
    *,
    mutation_head_sha: str,
    prior_autopipe_state: Mapping[str, Any],
    window_started_at: datetime,
) -> dict[str, Any]:
    cleanup = _require_mapping(raw, "cleanup.evidence document")
    _require_exact_keys(
        cleanup,
        {
            "captured_at",
            "window_started_at",
            "window_finished_at",
            "repo_units",
            "installed_units",
            "resolved_exec_start",
            "final_units",
            "compression_service_activations",
        },
        "cleanup.evidence document",
    )
    started = _parse_utc(cleanup["window_started_at"], "cleanup.window_started_at")
    finished = _parse_utc(cleanup["window_finished_at"], "cleanup.window_finished_at")
    captured = _parse_utc(cleanup["captured_at"], "cleanup.captured_at")
    if not window_started_at <= started <= finished <= captured:
        raise EvidenceError("cleanup activation-window chronology differs")
    expected_repo = {
        "service": "infra/systemd/nhms-node27-timeseries-compression.service",
        "timer": "infra/systemd/nhms-node27-timeseries-compression.timer",
    }
    repo_units = _require_mapping(cleanup["repo_units"], "cleanup.repo_units")
    installed_units = _require_mapping(cleanup["installed_units"], "cleanup.installed_units")
    if set(repo_units) != set(expected_repo) or set(installed_units) != set(expected_repo):
        raise EvidenceError("cleanup unit evidence set differs")
    repo_refs: dict[str, Any] = {}
    installed_refs: dict[str, Any] = {}
    for key, relative_path in expected_repo.items():
        repo_refs[key] = _validate_reviewed_file_ref(
            repo_units[key],
            label=f"cleanup.repo_units.{key}",
            mutation_head_sha=mutation_head_sha,
            relative_path=relative_path,
        )
        installed_refs[key] = _artifact_ref(
            installed_units[key], f"cleanup.installed_units.{key}", max_bytes=1024**2
        )
        if (
            repo_refs[key]["sha256"] != installed_refs[key]["sha256"]
            or repo_refs[key]["bytes"] != installed_refs[key]["bytes"]
        ):
            raise EvidenceError(f"cleanup installed {key} differs from reviewed repo bytes")
    exec_start = _require_list(cleanup["resolved_exec_start"], "cleanup.resolved_exec_start")
    if (
        sum(value == "--enforce" for value in exec_start) != 1
        or not exec_start
        or not str(exec_start[0]).endswith("scripts/node27_timeseries_compression_once.sh")
    ):
        raise EvidenceError("cleanup resolved ExecStart does not contain exactly one --enforce")
    final_units = _require_mapping(cleanup["final_units"], "cleanup.final_units")
    if set(final_units) != set(EXPECTED_UNITS):
        raise EvidenceError("cleanup must capture exactly four final unit states")
    normalized_units: dict[str, Any] = {}
    for unit_name in EXPECTED_UNITS:
        state = _require_mapping(final_units[unit_name], f"cleanup.final_units.{unit_name}")
        _require_exact_keys(
            state,
            {"enabled", "active", "sub", "result", "main_pid", "journal"},
            f"cleanup.final_units.{unit_name}",
        )
        journal = _artifact_ref(
            state["journal"], f"cleanup.final_units.{unit_name}.journal", max_bytes=4 * 1024**2
        )
        normalized_units[unit_name] = {**dict(state), "journal": journal}
    autopipe = normalized_units["nhms-node27-autopipe.timer"]
    if any(autopipe[key] != prior_autopipe_state[key] for key in prior_autopipe_state):
        raise EvidenceError("cleanup did not restore the exact prior autopipe timer state")
    timer = normalized_units["nhms-node27-timeseries-compression.timer"]
    service = normalized_units["nhms-node27-timeseries-compression.service"]
    if (
        timer["enabled"] != "enabled"
        or timer["active"] != "inactive"
        or timer["main_pid"] != 0
        or service["active"] != "inactive"
        or service["main_pid"] != 0
    ):
        raise EvidenceError("cleanup final compression timer/service state differs")
    activations = _require_list(
        cleanup["compression_service_activations"],
        "cleanup.compression_service_activations",
    )
    if activations:
        raise EvidenceError("cleanup observed compression service activation in the governed window")
    return {
        "captured_at": captured.isoformat().replace("+00:00", "Z"),
        "repo_units": repo_refs,
        "installed_units": installed_refs,
        "resolved_exec_start": exec_start,
        "final_units": normalized_units,
        "compression_service_activation_count": 0,
    }


def verify_bundle(
    bundle: Mapping[str, Any], *, receipt_schema: Mapping[str, Any], verifier_head_sha: str
) -> dict[str, Any]:
    """Recompute all task-4.5 derivable gates and return the terminal envelope."""
    try:
        _, canonical_receipt_schema = read_bounded_json_no_follow(
            CANONICAL_RECEIPT_SCHEMA,
            max_bytes=1024**2,
            label="canonical receipt schema",
            max_depth=32,
            max_nodes=50_000,
            max_array_items=10_000,
        )
    except BoundedEvidenceError as error:
        raise EvidenceError(str(error)) from error
    if _canonical_json_bytes(receipt_schema) != _canonical_json_bytes(canonical_receipt_schema):
        raise EvidenceError("receipt schema differs from the canonical verifier checkout schema")
    top_keys = {
        "schema_version",
        "issue",
        "generated_at",
        "node",
        "mutation_head_sha",
        "verifier_head_sha",
        "database_identity",
        "authorization",
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
    _require_exact_keys(bundle, top_keys, "bundle")
    if bundle["schema_version"] != SCHEMA_VERSION or bundle["issue"] != ISSUE:
        raise EvidenceError("bundle schema_version/issue mismatch")
    if (
        bundle["node"] != "node-27"
        or re.fullmatch(r"[0-9a-f]{40}", str(bundle["mutation_head_sha"])) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(bundle["verifier_head_sha"])) is None
        or bundle["verifier_head_sha"] != verifier_head_sha
    ):
        raise EvidenceError("bundle node/mutation/verifier SHA mismatch")
    _parse_utc(bundle["generated_at"], "generated_at")
    authorization = _require_mapping(bundle["authorization"], "authorization")
    expected_authorization = {
        "lag_seconds": EXPECTED_LAG_SECONDS,
        "bound": EXPECTED_BOUND,
        "max_selected_bytes": MAX_SELECTED_BYTES,
        "min_free_bytes": MIN_FREE_BYTES,
        "timeout_seconds": EXPECTED_TIMEOUT_SECONDS,
        "enforce_invocations": 1,
        "replay_decompression": True,
        "decompress_invocations": 1,
    }
    if authorization != expected_authorization:
        raise EvidenceError("authorization differs from the issue #1069 bound-1 envelope")

    preflight_bundle = _require_mapping(bundle["preflight"], "preflight")
    _require_exact_keys(
        preflight_bundle,
        {"evidence", "schema_dump", "schema_dump_list", "catalog_before"},
        "preflight",
    )
    preflight_ref, preflight = _json_artifact(preflight_bundle.get("evidence"), "preflight.evidence")
    preflight_summary = _validate_preflight(preflight, str(bundle["mutation_head_sha"]))
    recovery_bundle = _require_mapping(bundle["recovery"], "recovery")
    _require_exact_keys(recovery_bundle, {"preflight", "receipt", "invocation"}, "recovery")
    recovery_preflight_ref, recovery_preflight_raw = _json_artifact(
        recovery_bundle["preflight"], "recovery.preflight"
    )
    recovery_receipt_ref, recovery_receipt_raw = _json_artifact(
        recovery_bundle["receipt"], "recovery.receipt"
    )
    if (
        recovery_preflight_ref["path"] == recovery_receipt_ref["path"]
        or recovery_preflight_ref["sha256"] == recovery_receipt_ref["sha256"]
    ):
        raise EvidenceError("recovery preflight and receipt must be distinct artifacts")
    recovery_summary = _validate_recovery(
        recovery_preflight_raw,
        recovery_receipt_raw,
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        database_identity=_require_mapping(
            preflight["database_identity"], "preflight.database_identity"
        ),
        compression_preflight_captured_at=_parse_utc(
            preflight_summary["captured_at"], "preflight captured_at"
        ),
    )
    dump_ref, dump_raw = _artifact_bytes(
        preflight_bundle.get("schema_dump"), "preflight.schema_dump"
    )
    if not dump_raw.startswith(b"PGDMP"):
        raise EvidenceError("schema forensic dump is not PostgreSQL custom format")
    dump_list_ref, dump_list_raw = _json_artifact(
        preflight_bundle.get("schema_dump_list"), "preflight.schema_dump_list"
    )
    _validate_dump_listing(dump_list_raw, dump_ref=dump_ref)
    catalog_before_ref, catalog_before = _json_artifact(
        preflight_bundle.get("catalog_before"), "preflight.catalog_before"
    )
    _validate_pre_migration_catalog(catalog_before, "preflight.catalog_before")
    recovery_invocation_ref, recovery_invocation_raw = _json_artifact(
        recovery_bundle["invocation"], "recovery.invocation"
    )
    recovery_invocation = _validate_invocation_record(
        recovery_invocation_raw,
        label="recovery.invocation",
        kind="recovery_decompress",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        expected_binding={
            "receipt_sha256": recovery_receipt_ref["sha256"],
            "target": dict(RECOVERY_TARGET),
        },
    )
    if (
        recovery_invocation["started_at_dt"]
        != _parse_utc(recovery_receipt_raw["started_at"], "recovery receipt started")
        or recovery_invocation["finished_at_dt"]
        != _parse_utc(recovery_receipt_raw["finished_at"], "recovery receipt finished")
    ):
        raise EvidenceError("recovery invocation does not bind receipt chronology")

    migration = _require_mapping(bundle["migration"], "migration")
    _require_exact_keys(
        migration,
        {
            "migration_file",
            "first_invocation",
            "catalog_after_first",
            "second_invocation",
            "catalog_after_second",
        },
        "migration",
    )
    migration_ref = _validate_reviewed_file_ref(
        migration.get("migration_file"),
        label="migration.migration_file",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        relative_path="db/migrations/000047_hypertable_compression_settings.sql",
    )
    first_ref, first_catalog = _json_artifact(
        migration.get("catalog_after_first"), "migration.catalog_after_first"
    )
    second_ref, second_catalog = _json_artifact(
        migration.get("catalog_after_second"), "migration.catalog_after_second"
    )
    first_invocation_ref, first_invocation_raw = _json_artifact(
        migration["first_invocation"], "migration.first_invocation"
    )
    second_invocation_ref, second_invocation_raw = _json_artifact(
        migration["second_invocation"], "migration.second_invocation"
    )
    first_invocation = _validate_invocation_record(
        first_invocation_raw,
        label="migration.first_invocation",
        kind="migration_apply",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        expected_binding={
            "migration_sha256": migration_ref["sha256"],
            "catalog_sha256": first_ref["sha256"],
        },
    )
    second_invocation = _validate_invocation_record(
        second_invocation_raw,
        label="migration.second_invocation",
        kind="migration_apply",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        expected_binding={
            "migration_sha256": migration_ref["sha256"],
            "catalog_sha256": second_ref["sha256"],
        },
    )
    if (
        first_invocation_ref["sha256"] == second_invocation_ref["sha256"]
        or first_invocation["finished_at_dt"] > second_invocation["started_at_dt"]
    ):
        raise EvidenceError("migration applies are not distinct ordered execution artifacts")
    _validate_d3_catalog(first_catalog, "migration first catalog")
    _validate_d3_catalog(second_catalog, "migration second catalog")
    if _canonical_json_bytes(first_catalog) != _canonical_json_bytes(second_catalog):
        raise EvidenceError("first/second migration catalog snapshots differ")

    selection_bundle = _require_mapping(bundle["selection"], "selection")
    _require_exact_keys(
        selection_bundle, {"post_dry_run", "pre_enforce"}, "selection"
    )
    post_dry_ref, post_dry_raw = _json_artifact(
        selection_bundle["post_dry_run"], "selection.post_dry_run"
    )
    pre_enforce_ref, pre_enforce_raw = _json_artifact(
        selection_bundle["pre_enforce"], "selection.pre_enforce"
    )
    if (
        post_dry_ref["path"] == pre_enforce_ref["path"]
        or post_dry_ref["sha256"] == pre_enforce_ref["sha256"]
    ):
        raise EvidenceError("selection snapshots must be distinct observations")
    post_dry = _validate_selection_snapshot(post_dry_raw, "selection.post_dry_run")
    pre_enforce = _validate_selection_snapshot(pre_enforce_raw, "selection.pre_enforce")
    if post_dry["identities"] != pre_enforce["identities"]:
        raise EvidenceError("post-dry-run and pre-enforce selected tuples differ")
    if _parse_utc(preflight_summary["captured_at"], "preflight captured_at") > post_dry[
        "observed_at_dt"
    ]:
        raise EvidenceError("preflight was captured after the post-dry-run observation")
    identities = pre_enforce["identities"]
    if identities != [dict(RECOVERY_TARGET)]:
        raise EvidenceError("selection did not reselect the exact recovered chunk")
    selected = pre_enforce["selected"][0]
    selected_before = selected["before_bytes"]

    receipts_bundle = _require_mapping(bundle["receipts"], "receipts")
    _require_exact_keys(
        receipts_bundle,
        {"dry_run", "dry_run_invocation", "enforce", "enforce_invocation"},
        "receipts",
    )
    dry_ref, dry = _load_receipt(receipts_bundle.get("dry_run"), "receipts.dry_run", receipt_schema)
    enforce_ref, enforce = _load_receipt(
        receipts_bundle.get("enforce"), "receipts.enforce", receipt_schema
    )
    dry_invocation_ref, dry_invocation_raw = _json_artifact(
        receipts_bundle["dry_run_invocation"], "receipts.dry_run_invocation"
    )
    enforce_invocation_ref, enforce_invocation_raw = _json_artifact(
        receipts_bundle["enforce_invocation"], "receipts.enforce_invocation"
    )
    dry_invocation = _validate_invocation_record(
        dry_invocation_raw,
        label="receipts.dry_run_invocation",
        kind="compression_dry_run",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        expected_binding={"receipt_sha256": dry_ref["sha256"]},
    )
    enforce_invocation = _validate_invocation_record(
        enforce_invocation_raw,
        label="receipts.enforce_invocation",
        kind="compression_enforce",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        expected_binding={"receipt_sha256": enforce_ref["sha256"]},
    )
    if dry_invocation_ref["sha256"] == enforce_invocation_ref["sha256"]:
        raise EvidenceError("dry-run and enforce need distinct invocation records")
    if not (
        dry_invocation["started_at_dt"]
        <= _parse_utc(dry["generated_at"], "dry generated_at")
        <= dry_invocation["finished_at_dt"]
        <= enforce_invocation["started_at_dt"]
        <= _parse_utc(enforce["generated_at"], "enforce generated_at")
        <= enforce_invocation["finished_at_dt"]
    ):
        raise EvidenceError("dry-run/enforce invocation chronology differs")
    dry_generated_at = _parse_utc(dry["generated_at"], "dry-run generated_at")
    enforce_started_at = _parse_utc(enforce["now_utc"], "enforce now_utc")
    if post_dry["observed_at_dt"] < dry_generated_at:
        raise EvidenceError("post-dry-run selection was observed before dry-run completion")
    pre_enforce_delta = (
        enforce_started_at - pre_enforce["observed_at_dt"]
    ).total_seconds()
    if not 0 <= pre_enforce_delta <= MAX_PREFLIGHT_TO_ENFORCE_SECONDS:
        raise EvidenceError("pre-enforce selection is not within 60 seconds of enforce")
    if post_dry["observed_at_dt"] > pre_enforce["observed_at_dt"]:
        raise EvidenceError("selection observation order is reversed")
    if (
        dry["schema_version"] != "2.0"
        or enforce["schema_version"] != "2.0"
        or dry.get("head_sha") != bundle["mutation_head_sha"]
        or enforce.get("head_sha") != bundle["mutation_head_sha"]
        or dry["mode"] != "dry-run"
        or dry["outcome"] != "clean"
        or dry["lag_seconds"] != EXPECTED_LAG_SECONDS
        or dry["per_tick_bound"] != EXPECTED_BOUND
        or len(dry["selected"]) != 1
        or any(row.get("mutation_state") != "not_applicable" for row in dry["selected"])
        or any(row["after_bytes"] is not None for row in dry["selected"])
    ):
        raise EvidenceError("dry-run receipt fails exact bound-1 semantics")
    if (
        enforce["mode"] != "enforce"
        or enforce["outcome"] != "clean"
        or enforce["lag_seconds"] != EXPECTED_LAG_SECONDS
        or enforce["per_tick_bound"] != EXPECTED_BOUND
        or len(enforce["selected"]) != 1
        or any(row.get("mutation_state") != "committed" for row in enforce["selected"])
        or any(row.get("error") or row["after_bytes"] is None for row in enforce["selected"])
    ):
        raise EvidenceError("enforce receipt fails exact bound-1 clean semantics")
    dry_identity = [_selected_identity(_require_mapping(row, "dry selected")) for row in dry["selected"]]
    enforce_identity = [
        _selected_identity(_require_mapping(row, "enforce selected")) for row in enforce["selected"]
    ]
    if dry_identity != post_dry["identities"] or enforce_identity != identities:
        raise EvidenceError("selection/dry-run/enforce selected tuples differ")
    dry_candidate_identities = [
        _selected_identity(_require_mapping(row, "dry candidate"))
        for row in [*dry["selected"], *dry["deferred"]]
    ]
    enforce_candidate_identities = [
        _selected_identity(_require_mapping(row, "enforce candidate"))
        for row in [*enforce["selected"], *enforce["deferred"]]
    ]
    if (
        [_selected_identity(row) for row in post_dry["candidates"]]
        != dry_candidate_identities
        or [_selected_identity(row) for row in pre_enforce["candidates"]]
        != enforce_candidate_identities
    ):
        raise EvidenceError("selection candidates do not cover the complete ordered receipt scope")
    expected_totals = {
        key: {"before_bytes": 0, "after_bytes": None, "chunks_compressed": 0}
        for key in HYPERTABLE_KEYS
    }
    enforce_row = _require_mapping(enforce["selected"][0], "enforce selected")
    if (
        post_dry["selected"][0]["before_bytes"] != dry["selected"][0]["before_bytes"]
        or selected_before != enforce_row["before_bytes"]
    ):
        raise EvidenceError("selection and enforce before_bytes differ")
    selected_key = f"{enforce_row['hypertable_schema']}.{enforce_row['hypertable_name']}"
    expected_dry_totals = {
        key: {"before_bytes": 0, "after_bytes": None, "chunks_compressed": 0}
        for key in HYPERTABLE_KEYS
    }
    expected_dry_totals[selected_key]["before_bytes"] = dry["selected"][0]["before_bytes"]
    if dry["per_table_totals"] != expected_dry_totals:
        raise EvidenceError("dry-run per_table_totals arithmetic mismatch")
    expected_totals[selected_key] = {
        "before_bytes": enforce_row["before_bytes"],
        "after_bytes": enforce_row["after_bytes"],
        "chunks_compressed": 1,
    }
    if enforce["per_table_totals"] != expected_totals:
        raise EvidenceError("enforce per_table_totals arithmetic mismatch")
    if enforce_row["after_bytes"] >= enforce_row["before_bytes"]:
        raise EvidenceError("selected compressed bytes did not decrease")

    sizes_bundle = _require_mapping(bundle["sizes"], "sizes")
    sizes_pre_ref, sizes_pre_raw = _json_artifact(sizes_bundle.get("pre"), "sizes.pre")
    sizes_post_ref, sizes_post_raw = _json_artifact(sizes_bundle.get("post"), "sizes.post")
    sizes_pre_snapshot = _table_snapshot(
        sizes_pre_raw,
        "sizes.pre",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        expected_phase="pre-enforce",
    )
    sizes_post_snapshot = _table_snapshot(
        sizes_post_raw,
        "sizes.post",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        expected_phase="post-enforce",
    )
    if (
        sizes_pre_snapshot["snapshot_id"] == sizes_post_snapshot["snapshot_id"]
        or not sizes_pre_snapshot["captured_at_dt"]
        <= enforce_invocation["started_at_dt"]
        <= enforce_invocation["finished_at_dt"]
        <= sizes_post_snapshot["captured_at_dt"]
    ):
        raise EvidenceError("size snapshot/enforce chronology is invalid")
    sizes_pre = sizes_pre_snapshot["tables"]
    sizes_post = sizes_post_snapshot["tables"]
    pre_combined = sum(sizes_pre[key]["hypertable_size"] for key in HYPERTABLE_KEYS)
    post_combined = sum(sizes_post[key]["hypertable_size"] for key in HYPERTABLE_KEYS)
    compressed_delta = sum(
        sizes_post[key]["compressed_chunks"] - sizes_pre[key]["compressed_chunks"]
        for key in HYPERTABLE_KEYS
    )
    if post_combined >= pre_combined or compressed_delta != 1:
        raise EvidenceError("size/compressed-count acceptance arithmetic failed")
    if any(
        sizes_post[key]["compressed_chunks"] - sizes_pre[key]["compressed_chunks"]
        != (1 if key == selected_key else 0)
        for key in HYPERTABLE_KEYS
    ):
        raise EvidenceError("selected/sibling compressed-count transition differs")
    if any(
        relation.get("origin_chunk_schema") == selected["chunk_schema"]
        and relation.get("origin_chunk_name") == selected["chunk_name"]
        for relation in sizes_pre[selected_key]["compressed_relations"]
        if isinstance(relation, Mapping)
    ):
        raise EvidenceError("selected compressed relation already existed in pre snapshot")
    selected_relations = [
        _require_mapping(value, "selected compressed relation")
        for value in sizes_post[selected_key]["compressed_relations"]
        if isinstance(value, Mapping)
        and value.get("origin_chunk_schema") == selected["chunk_schema"]
        and value.get("origin_chunk_name") == selected["chunk_name"]
    ]
    if len(selected_relations) != 1:
        raise EvidenceError("post size snapshot does not bind the selected compressed sibling")
    post_relation_bytes = selected_relations[0]["bytes"]
    receipt_after_bytes = enforce_row["after_bytes"]
    if (
        post_relation_bytes >= enforce_row["before_bytes"]
        or abs(post_relation_bytes - receipt_after_bytes) > MAX_POST_MEASUREMENT_DRIFT_BYTES
    ):
        raise EvidenceError(
            "post compressed sibling size is not reduced or exceeds the 1 MiB "
            "measurement-time drift bound"
        )
    selected_relation_names = {
        str(selected["chunk_name"]),
        str(selected_relations[0]["name"]),
    }

    catalog_bundle = _require_mapping(bundle["catalog"], "catalog")
    _require_exact_keys(catalog_bundle, {"post"}, "catalog")
    catalog_post_ref, catalog_post_raw = _json_artifact(
        catalog_bundle.get("post"), "catalog.post"
    )
    catalog_post = _require_mapping(catalog_post_raw, "catalog.post document")
    _require_exact_keys(
        catalog_post,
        {
            "captured_at",
            "snapshot_id",
            "mutation_head_sha",
            "catalog",
            "compressed_chunk_identities",
        },
        "catalog.post document",
    )
    catalog_captured_at = _parse_utc(catalog_post["captured_at"], "catalog.post.captured_at")
    if (
        catalog_post["mutation_head_sha"] != bundle["mutation_head_sha"]
        or not isinstance(catalog_post["snapshot_id"], str)
        or not catalog_post["snapshot_id"]
        or catalog_captured_at < sizes_post_snapshot["captured_at_dt"]
    ):
        raise EvidenceError("catalog.post snapshot identity/chronology differs")
    _validate_d3_catalog(catalog_post["catalog"], "catalog.post.catalog")
    compressed_identities = _require_list(
        catalog_post["compressed_chunk_identities"],
        "catalog.post.compressed_chunk_identities",
    )
    if identities[0] not in compressed_identities:
        raise EvidenceError("post catalog does not prove selected chunk is compressed")

    benchmarks_bundle = _require_mapping(bundle["benchmarks"], "benchmarks")
    benchmark_ref, benchmark_raw = _json_artifact(
        benchmarks_bundle.get("evidence"), "benchmarks.evidence"
    )
    benchmark_results = _validate_benchmarks(
        benchmark_raw,
        selected,
        selected_relation_names=selected_relation_names,
        mutation_head_sha=str(bundle["mutation_head_sha"]),
    )

    cleanup_bundle = _require_mapping(bundle["cleanup"], "cleanup")
    cleanup_ref, cleanup_raw = _json_artifact(cleanup_bundle.get("evidence"), "cleanup.evidence")
    cleanup_summary = _validate_cleanup(
        cleanup_raw,
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        prior_autopipe_state=preflight_summary["prior_autopipe_state"],
        window_started_at=_parse_utc(
            recovery_summary["preflight_captured_at"], "recovery preflight captured_at"
        ),
    )
    derived_cleanup = {
        "autopipe_timer_restored": True,
        "compression_timer_enabled": True,
        "compression_timer_active": False,
        "compression_service_active": False,
        "compression_service_activation_count": cleanup_summary[
            "compression_service_activation_count"
        ],
        "installed_service_matches_repo": True,
        "installed_timer_matches_repo": True,
    }

    out_of_scope = _require_mapping(bundle["out_of_scope"], "out_of_scope")
    required_out_of_scope = {
        "retention_mutated": False,
        "drill_run": False,
        "node22_touched": False,
        "decompress_run": True,
        "role_mutated": False,
    }
    if out_of_scope != required_out_of_scope:
        raise EvidenceError("out_of_scope flags differ from the fixture")

    database_identity = _require_mapping(bundle["database_identity"], "database_identity")
    if database_identity != preflight["database_identity"]:
        raise EvidenceError("bundle/preflight database identity mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "issue": ISSUE,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "node": bundle["node"],
        "mutation_head_sha": bundle["mutation_head_sha"],
        "verifier_head_sha": verifier_head_sha,
        "database_identity": database_identity,
        "authorization": authorization,
        "recovery": {
            "preflight": recovery_preflight_ref,
            "receipt": recovery_receipt_ref,
            "authorized": True,
            **recovery_summary,
        },
        "preflight": {
            "evidence": preflight_ref,
            "schema_dump": dump_ref,
            "catalog_before": catalog_before_ref,
            "pg_restore_list_exit_code": 0,
            "role": preflight["role"],
            "quiescent": True,
            "captured_at": preflight_summary["captured_at"],
            "mutation_head_sha": preflight_summary["mutation_head_sha"],
            "container_state": preflight_summary["container_state"],
            "units": preflight_summary["units"],
        },
        "migration": {
            "migration_file": migration_ref,
            "first_exit_code": 0,
            "second_exit_code": 0,
            "catalog_after_first": first_ref,
            "catalog_after_second": second_ref,
            "catalogs_identical": True,
        },
        "selection": {
            "bound": 1,
            "selected": identities,
            "selected_before_bytes": selected_before,
            "post_dry_run": {
                "artifact": post_dry_ref,
                "observed_at": post_dry["observed_at"],
                "candidates_sha256": post_dry["candidates_sha256"],
                "selector_sha256": post_dry["selector_sha256"],
                "free_bytes": post_dry["free_bytes"],
                "cutoff_margin_seconds": post_dry["cutoff_margin_seconds"],
            },
            "pre_enforce": {
                "artifact": pre_enforce_ref,
                "observed_at": pre_enforce["observed_at"],
                "candidates_sha256": pre_enforce["candidates_sha256"],
                "selector_sha256": pre_enforce["selector_sha256"],
                "free_bytes": pre_enforce["free_bytes"],
                "cutoff_margin_seconds": pre_enforce["cutoff_margin_seconds"],
                "seconds_before_enforce": pre_enforce_delta,
            },
        },
        "receipts": {
            "dry_run": dry_ref,
            "enforce": enforce_ref,
            "dry_run_schema_valid": True,
            "dry_run_semantic_valid": True,
            "enforce_schema_valid": True,
            "enforce_semantic_valid": True,
        },
        "sizes": {
            "pre": sizes_pre_ref,
            "post": sizes_post_ref,
            "pre_combined_hypertable_size": pre_combined,
            "post_combined_hypertable_size": post_combined,
            "compressed_chunk_count_delta": compressed_delta,
            "selected_before_bytes": enforce_row["before_bytes"],
            "selected_after_bytes": enforce_row["after_bytes"],
        },
        "catalog": {
            "post": catalog_post_ref,
            "d3_exact": True,
            "no_compression_policy": True,
            "selected_chunks_compressed": True,
        },
        "benchmarks": {"evidence": benchmark_ref, "queries": benchmark_results},
        "cleanup": {"evidence": cleanup_ref, **derived_cleanup},
        "out_of_scope": out_of_scope,
        "verdict": PASS_VERDICT,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-path", required=True, type=Path)
    parser.add_argument("--output-path", required=True, type=Path)
    parser.add_argument(
        "--receipt-schema-path",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "schemas/timeseries_compression_receipt.schema.json",
    )
    parser.add_argument(
        "--evidence-schema-path",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "schemas/timeseries_compression_live_evidence.schema.json",
    )
    return parser


def _current_verifier_head() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    cleanliness = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--"],
        cwd=repo_root,
        check=False,
    )
    if cleanliness.returncode != 0:
        raise EvidenceError("executing verifier/schema differs from verifier_head_sha")
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    head = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise EvidenceError("cannot bind verifier_head_sha to the executing repository")
    return head


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.bundle_path.is_absolute() or not args.output_path.is_absolute():
            raise EvidenceError("bundle/output paths must be absolute")
        if args.receipt_schema_path != CANONICAL_RECEIPT_SCHEMA:
            raise EvidenceError("receipt schema path must be the canonical checkout path")
        if args.evidence_schema_path != CANONICAL_EVIDENCE_SCHEMA:
            raise EvidenceError("evidence schema path must be the canonical checkout path")
        _, bundle_raw = read_bounded_json_no_follow(
            args.bundle_path,
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
            label="bundle",
            max_depth=MAX_PLAN_DEPTH,
            max_nodes=MAX_JSON_NODES,
            max_array_items=MAX_JSON_ARRAY_ITEMS,
        )
        bundle = _require_mapping(bundle_raw, "bundle")
        _, receipt_schema_raw = read_bounded_json_no_follow(
            CANONICAL_RECEIPT_SCHEMA,
            max_bytes=1024**2,
            label="receipt schema",
        )
        receipt_schema = _require_mapping(receipt_schema_raw, "receipt schema")
        _, evidence_schema_raw = read_bounded_json_no_follow(
            CANONICAL_EVIDENCE_SCHEMA,
            max_bytes=1024**2,
            label="evidence schema",
        )
        evidence_schema = _require_mapping(evidence_schema_raw, "evidence schema")
        terminal = verify_bundle(
            bundle,
            receipt_schema=receipt_schema,
            verifier_head_sha=_current_verifier_head(),
        )
        jsonschema.Draft7Validator(
            evidence_schema, format_checker=jsonschema.FormatChecker()
        ).validate(terminal)
        atomic_write_bytes_no_follow(
            args.output_path,
            _canonical_json_bytes(terminal),
            mode=0o600,
            require_durable_replace=True,
        )
    except (
        BoundedEvidenceError,
        EvidenceError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        jsonschema.ValidationError,
    ) as error:
        print(
            json.dumps({"status": "failed", "reason": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"status": "passed", "verdict": PASS_VERDICT}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
