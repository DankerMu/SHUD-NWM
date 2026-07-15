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
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import jsonschema

from packages.common.safe_fs import atomic_write_bytes_no_follow

SCHEMA_VERSION = "1.0"
ISSUE = 1069
PASS_VERDICT = "PASS_TASK_4_5"
HYPERTABLE_KEYS = ("hydro.river_timeseries", "met.forcing_station_timeseries")
MAX_SELECTED_BYTES = 8 * 1024**3
MIN_FREE_BYTES = 300 * 1024**3
EXPECTED_LAG_SECONDS = 604_800
EXPECTED_BOUND = 1
EXPECTED_TIMEOUT_SECONDS = 900

# This constant is deliberately executable documentation: tests and the
# runbook pin the same capture contract without inventing a host-only format.
BUNDLE_CONTRACT: Mapping[str, str] = {
    "preflight.evidence": "JSON: host/repo/head/role/quiescence/unit state facts",
    "preflight.schema_dump": "custom-format pg_dump file reference",
    "preflight.catalog_before": "JSON: canonical pre-migration catalog rows",
    "migration.catalog_after_first": "JSON: exact D3 catalog after first apply",
    "migration.catalog_after_second": "JSON: exact D3 catalog after second apply",
    "selection.snapshot": "JSON: now/cutoff/free bytes and ordered selected tuples",
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


def _validate_preflight(raw: Any, head_sha: str) -> None:
    preflight = _require_mapping(raw, "preflight.evidence document")
    required = {
        "node",
        "repo_path",
        "head_sha",
        "worktree_clean",
        "database_identity",
        "role",
        "env_mode",
        "write_guards_present",
        "autopipe_quiescent",
        "database_writes_quiescent",
        "conflicting_locks_absent",
        "compression_timer_active",
        "compression_service_active",
    }
    if not required <= set(preflight):
        raise EvidenceError("preflight.evidence is missing required live facts")
    if (
        preflight["node"] != "node-27"
        or preflight["head_sha"] != head_sha
        or preflight["worktree_clean"] is not True
        or preflight["env_mode"] != "0600"
        or preflight["write_guards_present"] is not True
        or preflight["autopipe_quiescent"] is not True
        or preflight["database_writes_quiescent"] is not True
        or preflight["conflicting_locks_absent"] is not True
        or preflight["compression_timer_active"] is not False
        or preflight["compression_service_active"] is not False
    ):
        raise EvidenceError("preflight.evidence does not prove the controlled quiescent boundary")
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
        required = {
            "name",
            "source_refs",
            "query_sha256",
            "query_text",
            "parameters",
            "before",
            "after",
            "after_plan",
            "concurrent_load_stable",
        }
        if not required <= set(query):
            raise EvidenceError(f"benchmark {query.get('name')} missing required fields")
        source_refs = [
            _artifact_ref(value, f"benchmark {query['name']} source[{index}]")
            for index, value in enumerate(_require_list(query["source_refs"], "source_refs"))
        ]
        if not source_refs:
            raise EvidenceError("benchmark source_refs must not be empty")
        source_paths = {str(ref["path"]) for ref in source_refs}
        if query["name"] == "curve":
            required_source_suffixes = {"/packages/common/forecast_store.py"}
            required_parameter_keys = {
                "basin_version_id",
                "river_segment_id",
                "river_network_version_id",
                "issue_time",
                "run_type",
                "scenario",
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
                "valid_time",
                "z",
                "x",
                "y",
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
        parameters = _require_mapping(query["parameters"], f"benchmark {query['name']} parameters")
        if not required_parameter_keys <= set(parameters):
            raise EvidenceError(f"benchmark {query['name']} lacks production identity parameters")
        if re.fullmatch(r"[0-9a-f]{64}", str(query["query_sha256"])) is None:
            raise EvidenceError("benchmark query_sha256 is invalid")
        if not isinstance(query["query_text"], str) or not query["query_text"]:
            raise EvidenceError("benchmark query_text must be non-empty")
        if not all(token in query["query_text"] for token in required_query_tokens):
            raise EvidenceError(f"benchmark {query['name']} query is not the production SQL shape")
        if _sha256(query["query_text"].encode("utf-8")) != query["query_sha256"]:
            raise EvidenceError(f"benchmark {query['name']} query hash mismatch")
        before = _require_mapping(query["before"], "benchmark before")
        after = _require_mapping(query["after"], "benchmark after")
        phase_keys = {
            "result_sha256",
            "rows",
            "bytes",
            "cache_class",
            "samples_ms",
            "planning_ms",
            "execution_ms",
            "shared_hit_blocks",
            "shared_read_blocks",
            "result_payload",
        }
        if not phase_keys <= set(before) or not phase_keys <= set(after):
            raise EvidenceError("benchmark phase is missing raw result/cache/timing evidence")
        for phase, phase_name in ((before, "before"), (after, "after")):
            result_bytes, result_rows = _result_bytes(str(query["name"]), phase["result_payload"])
            if (
                _sha256(result_bytes) != phase["result_sha256"]
                or len(result_bytes) != phase["bytes"]
                or result_rows != phase["rows"]
            ):
                raise EvidenceError(
                    f"benchmark {query['name']} {phase_name} result hash/count mismatch"
                )
        for field in ("result_sha256", "rows", "bytes"):
            if before[field] != after[field]:
                raise EvidenceError(f"benchmark {query['name']} changed {field}")
        if before["cache_class"] not in {"warm-cache", "mixed-cache"}:
            raise EvidenceError("benchmark cache_class is invalid")
        if before["cache_class"] != after["cache_class"]:
            raise EvidenceError("benchmark before/after cache classes differ")
        for phase, phase_name in ((before, "before"), (after, "after")):
            read_blocks = phase["shared_read_blocks"]
            if not isinstance(read_blocks, int) or isinstance(read_blocks, bool) or read_blocks < 0:
                raise EvidenceError(f"benchmark {query['name']} {phase_name} read blocks invalid")
            if (phase["cache_class"] == "warm-cache") != (read_blocks == 0):
                raise EvidenceError(f"benchmark {query['name']} {phase_name} cache class mismatch")
        _, before_median, before_p95 = _stats(
            before["samples_ms"], f"benchmark {query['name']} before samples"
        )
        _, after_median, after_p95 = _stats(
            after["samples_ms"], f"benchmark {query['name']} after samples"
        )
        median_threshold = max(1.5 * before_median, before_median + 100.0)
        p95_threshold = max(2.0 * before_p95, before_p95 + 250.0)
        if after_median > median_threshold or after_p95 > p95_threshold:
            raise EvidenceError(f"benchmark {query['name']} exceeds timing threshold")
        if query["concurrent_load_stable"] is not True:
            raise EvidenceError(f"benchmark {query['name']} has concurrent-load drift")
        if not _plan_contains(query["after_plan"], ["DecompressChunk"]) or not any(
            _plan_contains(query["after_plan"], [name]) for name in selected_relation_names
        ):
            raise EvidenceError(
                f"benchmark {query['name']} after plan does not bind selected DecompressChunk"
            )
        output.append(
            {
                "name": query["name"],
                "source_refs": source_refs,
                "query_sha256": query["query_sha256"],
                "result_sha256": before["result_sha256"],
                "rows": before["rows"],
                "bytes": before["bytes"],
                "cache_class": before["cache_class"],
                "before_samples_ms": before["samples_ms"],
                "after_samples_ms": after["samples_ms"],
                "before_median_ms": before_median,
                "after_median_ms": after_median,
                "median_threshold_ms": median_threshold,
                "before_p95_ms": before_p95,
                "after_p95_ms": after_p95,
                "p95_threshold_ms": p95_threshold,
                "decompress_chunk_plan_bound": True,
                "concurrent_load_stable": True,
            }
        )
    return output


def verify_bundle(
    bundle: Mapping[str, Any], *, receipt_schema: Mapping[str, Any]
) -> dict[str, Any]:
    """Recompute all task-4.5 derivable gates and return the terminal envelope."""
    top_keys = {
        "schema_version",
        "issue",
        "generated_at",
        "node",
        "head_sha",
        "database_identity",
        "authorization",
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
    if bundle["node"] != "node-27" or re.fullmatch(r"[0-9a-f]{40}", str(bundle["head_sha"])) is None:
        raise EvidenceError("bundle node/head_sha mismatch")
    _parse_utc(bundle["generated_at"], "generated_at")
    authorization = _require_mapping(bundle["authorization"], "authorization")
    expected_authorization = {
        "lag_seconds": EXPECTED_LAG_SECONDS,
        "bound": EXPECTED_BOUND,
        "max_selected_bytes": MAX_SELECTED_BYTES,
        "min_free_bytes": MIN_FREE_BYTES,
        "timeout_seconds": EXPECTED_TIMEOUT_SECONDS,
        "enforce_invocations": 1,
    }
    if authorization != expected_authorization:
        raise EvidenceError("authorization differs from the issue #1069 bound-1 envelope")

    preflight_bundle = _require_mapping(bundle["preflight"], "preflight")
    preflight_ref, preflight = _json_artifact(preflight_bundle.get("evidence"), "preflight.evidence")
    _validate_preflight(preflight, str(bundle["head_sha"]))
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
    selection_ref, selection_raw = _json_artifact(
        selection_bundle.get("snapshot"), "selection.snapshot"
    )
    selection = _require_mapping(selection_raw, "selection.snapshot document")
    selected_rows = _require_list(selection.get("selected"), "selection.selected")
    if len(selected_rows) != EXPECTED_BOUND:
        raise EvidenceError("selection must contain exactly one chunk")
    selected = _require_mapping(selected_rows[0], "selection.selected[0]")
    if (
        selected.get("hypertable_schema") != "hydro"
        or selected.get("hypertable_name") != "river_timeseries"
    ):
        raise EvidenceError("bound-1 live selection must be a hydro river chunk")
    if selection.get("free_bytes", -1) < MIN_FREE_BYTES:
        raise EvidenceError("selection free-space headroom is below 300 GiB")
    selected_before = selected.get("before_bytes")
    if not isinstance(selected_before, int) or selected_before > MAX_SELECTED_BYTES:
        raise EvidenceError("selected chunk exceeds the 8 GiB cap")
    cutoff = _parse_utc(selection.get("cutoff"), "selection.cutoff")
    range_end = _parse_utc(selected.get("range_end"), "selection.selected.range_end")
    cutoff_margin = int((cutoff - range_end).total_seconds())
    if cutoff_margin < 600:
        raise EvidenceError("selected chunk is within ten minutes of cutoff")
    identities = [_selected_identity(row) for row in selected_rows]
    selector_sha = _sha256(_canonical_json_bytes(identities))
    if selection_bundle.get("dry_run_selector_sha256") != selector_sha:
        raise EvidenceError("dry-run selector hash mismatch")
    if selection_bundle.get("pre_enforce_selector_sha256") != selector_sha:
        raise EvidenceError("pre-enforce selector hash mismatch")

    receipts_bundle = _require_mapping(bundle["receipts"], "receipts")
    dry_ref, dry = _load_receipt(receipts_bundle.get("dry_run"), "receipts.dry_run", receipt_schema)
    enforce_ref, enforce = _load_receipt(
        receipts_bundle.get("enforce"), "receipts.enforce", receipt_schema
    )
    if (
        dry["mode"] != "dry-run"
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
    if dry_identity != identities or enforce_identity != identities:
        raise EvidenceError("selection/dry-run/enforce selected tuples differ")
    expected_totals = {
        key: {"before_bytes": 0, "after_bytes": None, "chunks_compressed": 0}
        for key in HYPERTABLE_KEYS
    }
    enforce_row = _require_mapping(enforce["selected"][0], "enforce selected")
    if selected_before != enforce_row["before_bytes"]:
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
    if len(selected_relations) != 1 or selected_relations[0]["bytes"] != enforce_row["after_bytes"]:
        raise EvidenceError("post size snapshot does not bind the selected compressed sibling")
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
        "decompress_run": False,
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
        "head_sha": bundle["head_sha"],
        "database_identity": database_identity,
        "authorization": authorization,
        "preflight": {
            "evidence": preflight_ref,
            "schema_dump": dump_ref,
            "catalog_before": catalog_before_ref,
            "pg_restore_list_exit_code": 0,
            "role": preflight["role"],
            "quiescent": True,
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
            "snapshot": selection_ref,
            "selector_sha256": selector_sha,
            "bound": 1,
            "selected": identities,
            "selected_before_bytes": selected_before,
            "free_bytes": selection["free_bytes"],
            "cutoff_margin_seconds": cutoff_margin,
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
        terminal = verify_bundle(bundle, receipt_schema=receipt_schema)
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
