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
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import jsonschema

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


def _artifact_ref(value: Any, label: str) -> dict[str, Any]:
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
    try:
        info = path.lstat()
    except OSError as error:
        raise EvidenceError(f"{label} is unavailable: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EvidenceError(f"{label} must reference a regular non-symlink file")
    raw = path.read_bytes()
    if len(raw) != size or _sha256(raw) != digest:
        raise EvidenceError(f"{label} byte count or sha256 mismatch")
    return {"path": str(path), "sha256": digest, "bytes": size}


def _json_artifact(value: Any, label: str) -> tuple[dict[str, Any], Any]:
    ref = _artifact_ref(value, label)
    try:
        document = json.loads(Path(ref["path"]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"{label} is not valid UTF-8 JSON: {error}") from error
    _reject_secrets(document, label)
    return ref, document


def _reject_secrets(value: Any, label: str) -> None:
    """Reject credential-bearing evidence instead of trying to redact it later."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"password", "database_url", "dsn"} or lowered.endswith("_password"):
                raise EvidenceError(f"{label} contains forbidden credential field")
            _reject_secrets(item, label)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item, label)
    elif isinstance(value, str) and re.search(r"postgres(?:ql)?://[^/\s]*@", value):
        raise EvidenceError(f"{label} contains a credential-bearing database URL")


def _parse_utc(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise EvidenceError(f"{label} must carry a timezone")
    return parsed.astimezone(UTC)


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


def _table_snapshot(raw: Any, label: str) -> Mapping[str, Any]:
    snapshot = _require_mapping(raw, label)
    _require_exact_keys(snapshot, {"tables"}, label)
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
    return tables


def _validate_selection_snapshot(raw: Any, label: str) -> dict[str, Any]:
    snapshot = _require_mapping(raw, f"{label} document")
    _require_exact_keys(
        snapshot,
        {"observed_at", "cutoff", "free_bytes", "candidates", "selected"},
        f"{label} document",
    )
    observed_at = _parse_utc(snapshot["observed_at"], f"{label}.observed_at")
    cutoff = _parse_utc(snapshot["cutoff"], f"{label}.cutoff")
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


def _plan_contains(plan: Any, needles: Sequence[str]) -> bool:
    lowered = json.dumps(plan, sort_keys=True).lower()
    return all(needle.lower() in lowered for needle in needles)


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
    if not activities:
        raise EvidenceError(f"benchmark {query_name} {phase_name} lacks activity samples")
    normalized_activities: list[dict[str, Any]] = []
    for index, value_ in enumerate(activities):
        activity = _require_mapping(value_, f"benchmark activity[{index}]")
        _require_exact_keys(
            activity,
            {"captured_at", "active_sessions", "material_load_stable"},
            f"benchmark activity[{index}]",
        )
        _parse_utc(activity["captured_at"], f"benchmark activity[{index}].captured_at")
        if (
            not isinstance(activity["active_sessions"], int)
            or isinstance(activity["active_sessions"], bool)
            or activity["active_sessions"] < 0
            or activity["material_load_stable"] is not True
        ):
            raise EvidenceError(f"benchmark {query_name} {phase_name} has load drift")
        normalized_activities.append(dict(activity))
    if phase_name == "after":
        for index, measurement in enumerate(measurements):
            if not _plan_contains(measurement["plan"], ["DecompressChunk"]) or not any(
                _plan_contains(measurement["plan"], [name]) for name in selected_relation_names
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
        "samples_ms": samples,
        "median_ms": median,
        "p95_ms": p95,
    }


def _validate_benchmarks(
    raw: Any,
    selected: Mapping[str, Any],
    *,
    selected_relation_names: set[str],
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
                "source_refs",
                "query_sha256",
                "query_text",
                "binding",
                "before",
                "after",
            },
            f"benchmark {query.get('name')}",
        )
        source_refs = [
            _artifact_ref(value, f"benchmark {query['name']} source[{index}]")
            for index, value in enumerate(_require_list(query["source_refs"], "source_refs"))
        ]
        if not source_refs:
            raise EvidenceError("benchmark source_refs must not be empty")
        source_paths = {str(ref["path"]) for ref in source_refs}
        if query["name"] == "curve":
            required_source_suffixes = {"/packages/common/forecast_store.py"}
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
            required_source_suffixes = {
                "/services/tiles/mvt.py",
                "/apps/api/routes/hydro_display.py",
            }
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
        if not all(any(path.endswith(suffix) for path in source_paths) for suffix in required_source_suffixes):
            raise EvidenceError(f"benchmark {query['name']} is not bound to production source files")
        binding = _require_mapping(query["binding"], f"benchmark {query['name']} binding")
        if query["name"] == "curve":
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
        else:
            if set(binding) != required_parameter_keys:
                raise EvidenceError("benchmark mvt binding is not the exact production parameter map")
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


def verify_bundle(
    bundle: Mapping[str, Any], *, receipt_schema: Mapping[str, Any], verifier_head_sha: str
) -> dict[str, Any]:
    """Recompute all task-4.5 derivable gates and return the terminal envelope."""
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
    preflight_ref, preflight = _json_artifact(preflight_bundle.get("evidence"), "preflight.evidence")
    preflight_summary = _validate_preflight(preflight, str(bundle["mutation_head_sha"]))
    recovery_bundle = _require_mapping(bundle["recovery"], "recovery")
    _require_exact_keys(recovery_bundle, {"preflight", "receipt"}, "recovery")
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
    dump_ref = _artifact_ref(preflight_bundle.get("schema_dump"), "preflight.schema_dump")
    catalog_before_ref, _ = _json_artifact(
        preflight_bundle.get("catalog_before"), "preflight.catalog_before"
    )
    if preflight_bundle.get("pg_restore_list_exit_code") != 0:
        raise EvidenceError("schema forensic dump pg_restore --list did not pass")

    migration = _require_mapping(bundle["migration"], "migration")
    migration_ref = _artifact_ref(migration.get("migration_file"), "migration.migration_file")
    first_ref, first_catalog = _json_artifact(
        migration.get("catalog_after_first"), "migration.catalog_after_first"
    )
    second_ref, second_catalog = _json_artifact(
        migration.get("catalog_after_second"), "migration.catalog_after_second"
    )
    if migration.get("first_exit_code") != 0 or migration.get("second_exit_code") != 0:
        raise EvidenceError("both migration applies must exit zero")
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
    dry_ref, dry = _load_receipt(receipts_bundle.get("dry_run"), "receipts.dry_run", receipt_schema)
    enforce_ref, enforce = _load_receipt(
        receipts_bundle.get("enforce"), "receipts.enforce", receipt_schema
    )
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
        or any(row["after_bytes"] is not None for row in dry["selected"])
    ):
        raise EvidenceError("dry-run receipt fails exact bound-1 semantics")
    if (
        enforce["mode"] != "enforce"
        or enforce["outcome"] != "clean"
        or enforce["lag_seconds"] != EXPECTED_LAG_SECONDS
        or enforce["per_tick_bound"] != EXPECTED_BOUND
        or len(enforce["selected"]) != 1
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
    sizes_pre = _table_snapshot(sizes_pre_raw, "sizes.pre")
    sizes_post = _table_snapshot(sizes_post_raw, "sizes.post")
    pre_combined = sum(sizes_pre[key]["hypertable_size"] for key in HYPERTABLE_KEYS)
    post_combined = sum(sizes_post[key]["hypertable_size"] for key in HYPERTABLE_KEYS)
    compressed_delta = sum(
        sizes_post[key]["compressed_chunks"] - sizes_pre[key]["compressed_chunks"]
        for key in HYPERTABLE_KEYS
    )
    if post_combined >= pre_combined or compressed_delta != 1:
        raise EvidenceError("size/compressed-count acceptance arithmetic failed")
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
    catalog_post_ref, catalog_post = _json_artifact(catalog_bundle.get("post"), "catalog.post")
    _validate_d3_catalog(catalog_post, "catalog.post")
    compressed_identities = _require_list(
        catalog_bundle.get("compressed_chunk_identities"), "catalog.compressed_chunk_identities"
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
    )

    cleanup_bundle = _require_mapping(bundle["cleanup"], "cleanup")
    cleanup_ref, cleanup_raw = _json_artifact(cleanup_bundle.get("evidence"), "cleanup.evidence")
    cleanup = _require_mapping(cleanup_raw, "cleanup.evidence document")
    required_cleanup = {
        "autopipe_timer_restored": True,
        "compression_timer_enabled": True,
        "compression_timer_active": False,
        "compression_service_active": False,
        "compression_service_activation_count": 0,
        "installed_service_matches_repo": True,
        "installed_timer_matches_repo": True,
    }
    if any(cleanup.get(key) != value for key, value in required_cleanup.items()):
        raise EvidenceError("cleanup does not prove restored autopipe and inactive timer boundary")

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
            **preflight_summary,
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
        "cleanup": {"evidence": cleanup_ref, **required_cleanup},
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
        bundle = _require_mapping(
            json.loads(args.bundle_path.read_text(encoding="utf-8")), "bundle"
        )
        receipt_schema = _require_mapping(
            json.loads(args.receipt_schema_path.read_text(encoding="utf-8")),
            "receipt schema",
        )
        evidence_schema = _require_mapping(
            json.loads(args.evidence_schema_path.read_text(encoding="utf-8")),
            "evidence schema",
        )
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
    except (EvidenceError, OSError, UnicodeError, json.JSONDecodeError, jsonschema.ValidationError) as error:
        print(
            json.dumps({"status": "failed", "reason": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"status": "passed", "verdict": PASS_VERDICT}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
