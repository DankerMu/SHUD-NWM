#!/usr/bin/env python3
"""Independently validate and publish issue #1069 live compression evidence.

The input bundle points to a supervisor-owned immutable run plan, append-only
ledger, and absolute ``{path, sha256, bytes}`` artifact references. Referenced
files are recursively resolved, size/hash checked, and interpreted here. This
verifier never imports the compression runner or executes DB/container/systemd
commands: it can publish evidence, but cannot migrate, compress, decompress,
drop chunks, or mutate roles.

Required referenced JSON shapes are documented by ``BUNDLE_CONTRACT`` below
and in the node-27 storage runbook.  JSON hashes use canonical compact sorted
UTF-8 plus one trailing newline, equivalent to ``jq -cS`` for these objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import jsonschema

from packages.common import compression_terminal_state as terminal_state
from packages.common.evidence_io import (
    ArtifactClosure,
    BoundedEvidenceError,
    FileIdentity,
    assert_output_disjoint_from_closure,
    assert_paths_disjoint,
    inspect_bounded_file_no_follow,
    read_bounded_bytes_with_identity_no_follow,
    read_bounded_json_no_follow,
    read_bounded_json_with_identity_no_follow,
    reject_secret_material,
    resolve_artifact_closure,
    reverify_artifact_closure,
    validate_json_complexity,
)

SCHEMA_VERSION = "3.0"
ISSUE = 1069
PASS_VERDICT = "PASS_TASK_4_5"
PASS_CLAIM = "controlled lane executed exactly once with no observed conflict"
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
MAX_BINARY_ARTIFACT_BYTES = 512 * 1024**2
MAX_GIT_BLOB_BYTES = 8 * 1024**2
MAX_SUBPROCESS_OUTPUT_BYTES = 16 * 1024**2
PUBLISH_LOCK_TIMEOUT_SECONDS = 5.0
QUALIFYING_SCHEMA_VERSION = "3.0"
EXPECTED_REPO_PATH = "/home/nwm/NWM"
EXPECTED_REMOTE_IDENTITY = "DankerMu/SHUD-NWM"
REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_RECEIPT_SCHEMA = REPO_ROOT / "schemas/timeseries_compression_receipt.schema.json"
CANONICAL_EVIDENCE_SCHEMA = REPO_ROOT / "schemas/timeseries_compression_live_evidence.schema.json"
CANONICAL_MIGRATION = REPO_ROOT / "db/migrations/000047_hypertable_compression_settings.sql"
EXPECTED_UNITS = (
    "nhms-node27-autopipe.timer",
    "nhms-node27-autopipe.service",
    "nhms-node27-timeseries-compression.timer",
    "nhms-node27-timeseries-compression.service",
    "nhms-node27-timeseries-compression-replay.service",
)
EXPECTED_LEDGER_COUNTS: Mapping[str, int] = {
    "migration_apply": 2,
    "decompress": 1,
    "compression_dry_run": 1,
    "compression_enforce": 1,
    "compression_service_activation": 0,
    "retention": 0,
    "drill": 0,
    "role": 0,
    "node22": 0,
    "pg_dump": 1,
    "pg_restore_version": 1,
    "pg_restore_list": 1,
    "benchmark_before": 1,
    "benchmark_after": 1,
    "replay_supervisor_activation": 1,
}
EXPECTED_COMMAND_COUNTS: Mapping[str, int] = {
    kind: count for kind, count in EXPECTED_LEDGER_COUNTS.items() if kind != "replay_supervisor_activation"
}
EXPECTED_LEDGER_SEQUENCE = (
    "pg_dump",
    "pg_restore_version",
    "pg_restore_list",
    "migration_apply",
    "migration_apply",
    "decompress",
    "compression_dry_run",
    "benchmark_before",
    "compression_enforce",
    "benchmark_after",
)
EXPECTED_CAPTURE_SEQUENCE = (
    "preflight_evidence",
    "schema_dump_list",
    "catalog_before",
    "catalog_after_first",
    "catalog_after_second",
    "recovery_preflight",
    "post_dry_selection",
    "pre_enforce_selection",
    "sizes_pre",
    "sizes_post",
    "catalog_post",
    "cleanup",
)
EXPECTED_PRODUCER_SEQUENCE = (
    "capture:preflight_evidence",
    "child:pg_dump:0",
    "child:pg_restore_version:0",
    "child:pg_restore_list:0",
    "capture:schema_dump_list",
    "capture:catalog_before",
    "child:migration_apply:0",
    "capture:catalog_after_first",
    "child:migration_apply:1",
    "capture:catalog_after_second",
    "capture:recovery_preflight",
    "child:decompress:0",
    "child:compression_dry_run:0",
    "capture:post_dry_selection",
    "child:benchmark_before:0",
    "capture:pre_enforce_selection",
    "capture:sizes_pre",
    "child:compression_enforce:0",
    "capture:sizes_post",
    "capture:catalog_post",
    "child:benchmark_after:0",
    "capture:cleanup",
)
EXPECTED_OUTPUT_OWNERS: Mapping[str, tuple[str, int]] = {
    "schema_dump": ("pg_dump", 0),
    "recovery_receipt": ("decompress", 0),
    "dry_run_receipt": ("compression_dry_run", 0),
    "benchmark_before": ("benchmark_before", 0),
    "enforce_receipt": ("compression_enforce", 0),
    "benchmarks": ("benchmark_after", 0),
    **{kind: (f"capture:{kind}", 0) for kind in EXPECTED_CAPTURE_SEQUENCE},
}
PREFLIGHT_KEYS = frozenset(
    {
        "captured_at",
        "node",
        "repo_path",
        "repo_remote_identity",
        "mutation_head_sha",
        "worktree_clean",
        "database_identity",
        "database_identity_probe",
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
    "execution.run_plan": "JSON: immutable concrete supervisor plan/cardinality/checkpoints",
    "execution.ledger": "JSONL: producer-owned children plus raw quiescence refs",
    "recovery.preflight": "JSON: separately authorized exact-chunk decompression preflight",
    "recovery.receipt": "JSON: exact-chunk decompression result, row parity, and chronology",
    "preflight.evidence": "JSON: captured mutation SHA, container and four-unit state facts",
    "preflight.schema_dump": "custom-format pg_dump file reference",
    "preflight.schema_dump_list": "JSON: descriptor-bound PG15 pg_restore output identity",
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
        "SELECT decompress_" + "chunk($1::regclass);",
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
_TIMEOUT_PREFIX = [
    "/usr/bin/timeout",
    "--signal=TERM",
    "--kill-after=30s",
    "900s",
]


def _invocation_execution_identity(kind: str) -> dict[str, Any]:
    repo = EXPECTED_REPO_PATH
    if kind in {"migration_apply", "recovery_decompress"}:
        return {
            "resolved_repo_path": repo,
            "resolved_interpreter": "/usr/bin/psql",
            "resolved_script": (
                f"{repo}/db/migrations/000047_hypertable_compression_settings.sql"
                if kind == "migration_apply"
                else None
            ),
            "resolved_wrapper": None,
            "resolved_env_file": f"{repo}/infra/env/node27-timeseries-compression.env",
            "launcher_argv": [*_TIMEOUT_PREFIX, *INVOCATION_ARGV[kind]],
        }
    return {
        "resolved_repo_path": repo,
        "resolved_interpreter": f"{repo}/.venv/bin/python",
        "resolved_script": f"{repo}/scripts/node27_timeseries_compression.py",
        "resolved_wrapper": f"{repo}/scripts/node27_timeseries_compression_once.sh",
        "resolved_env_file": f"{repo}/infra/env/node27-timeseries-compression.env",
        "launcher_argv": [
            *_TIMEOUT_PREFIX,
            f"{repo}/.venv/bin/python",
            f"{repo}/scripts/node27_timeseries_compression.py",
            *INVOCATION_ARGV[kind][1:],
        ],
    }


EvidenceError = terminal_state.TerminalStateError


_RETAINED_IDENTITIES: list[FileIdentity] = []
_ANY_EXPECTED_IDENTITY = terminal_state._ANY_EXPECTED_IDENTITY
INTENT_STATE_SCHEMA_VERSION = terminal_state.INTENT_STATE_SCHEMA_VERSION
MAX_INTENT_STATE_BYTES = terminal_state.MAX_INTENT_STATE_BYTES


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


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
        raise EvidenceError(f"{label} keys differ: missing={sorted(keys - actual)} extra={sorted(actual - keys)}")


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
    if (
        re.fullmatch(r"[0-9a-f]{64}", str(ref["sha256"])) is None
        or not isinstance(ref["bytes"], int)
        or isinstance(ref["bytes"], bool)
        or ref["bytes"] < 0
        or ref["bytes"] > max_bytes
    ):
        raise EvidenceError(f"{label} reference metadata is invalid")
    digest = str(ref["sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise EvidenceError(f"{label}.sha256 must be lowercase sha256")
    size = ref["bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise EvidenceError(f"{label}.bytes must be a non-negative integer")
    if size > max_bytes:
        raise EvidenceError(f"{label} exceeds the byte ceiling")
    try:
        raw, identity = read_bounded_bytes_with_identity_no_follow(path, max_bytes=max_bytes, label=label)
    except BoundedEvidenceError as error:
        raise EvidenceError(str(error)) from error
    if len(raw) != size or _sha256(raw) != digest:
        raise EvidenceError(f"{label} byte count or sha256 mismatch")
    _RETAINED_IDENTITIES.append(identity)
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
        raw, identity, document = read_bounded_json_with_identity_no_follow(
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
    _RETAINED_IDENTITIES.append(identity)
    _reject_secrets(document, label)
    return ref, document


def _text_artifact(value: Any, label: str, *, max_bytes: int = 4 * 1024**2) -> tuple[dict[str, Any], str]:
    ref, raw = _artifact_bytes(value, label, max_bytes=max_bytes)
    try:
        text_value = raw.decode("utf-8")
    except UnicodeError as error:
        raise EvidenceError(f"{label} is not valid UTF-8 text") from error
    _reject_secrets(text_value, label)
    return ref, text_value


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


def _streaming_artifact_ref(
    value: Any,
    label: str,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any], FileIdentity]:
    """Validate a large artifact by streaming once, retaining descriptor identity."""

    ref = _require_mapping(value, label)
    _require_exact_keys(ref, {"path", "sha256", "bytes"}, label)
    path = Path(str(ref["path"]))
    if not path.is_absolute():
        raise EvidenceError(f"{label}.path must be absolute")
    try:
        identity = inspect_bounded_file_no_follow(path, max_bytes=max_bytes, label=label)
    except BoundedEvidenceError as error:
        raise EvidenceError(str(error)) from error
    if ref["bytes"] != identity.size or ref["sha256"] != identity.sha256:
        raise EvidenceError(f"{label} byte count or sha256 mismatch")
    _RETAINED_IDENTITIES.append(identity)
    return {
        "path": str(path),
        "sha256": identity.sha256,
        "bytes": identity.size,
    }, identity


def _reject_secrets(value: Any, label: str) -> None:
    """Reject credential-bearing evidence instead of trying to redact it later."""

    try:
        reject_secret_material(value, label=label)
    except BoundedEvidenceError as error:
        raise EvidenceError(str(error)) from error


def _git_blob_bytes(head_sha: str, relative_path: str, label: str) -> bytes:
    object_name = f"{head_sha}:{relative_path}"
    try:
        size_result = subprocess.run(
            ["git", "cat-file", "-s", object_name],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceError(f"{label} Git blob size query timed out") from error
    if size_result.returncode != 0 or len(size_result.stdout) > 128:
        raise EvidenceError(f"{label} cannot be bound to mutation SHA")
    try:
        size = int(size_result.stdout.strip())
    except ValueError as error:
        raise EvidenceError(f"{label} Git blob size is invalid") from error
    if size < 0 or size > MAX_GIT_BLOB_BYTES:
        raise EvidenceError(f"{label} Git blob exceeds the byte ceiling")
    try:
        result = subprocess.run(
            ["git", "cat-file", "blob", object_name],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceError(f"{label} Git blob read timed out") from error
    if result.returncode != 0 or len(result.stdout) != size or len(result.stderr) > 4096:
        raise EvidenceError(f"{label} cannot be bound to mutation SHA")
    return result.stdout


def _remote_identity(url: str) -> str:
    normalized = url.strip().removesuffix(".git")
    if normalized.startswith("git@github.com:"):
        return normalized.split(":", 1)[1]
    marker = "github.com/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    return ""


def _validate_repository_provenance(*, mutation_head_sha: str, reviewed_remote_ref: str) -> None:
    if not reviewed_remote_ref.startswith("refs/remotes/origin/"):
        raise EvidenceError("reviewed mutation ref is not an origin remote-tracking ref")
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        reviewed = subprocess.run(
            ["git", "rev-parse", "--verify", reviewed_remote_ref],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceError("repository provenance query timed out") from error
    if (
        remote.returncode != 0
        or len(remote.stdout) > 4096
        or _remote_identity(remote.stdout) != EXPECTED_REMOTE_IDENTITY
        or reviewed.returncode != 0
        or reviewed.stdout.strip() != mutation_head_sha
    ):
        raise EvidenceError("mutation SHA is not the authorization-pinned origin lineage")


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
            "reviewed_remote_ref",
            "artifact_bindings",
            "resolved_repo_path",
            "resolved_interpreter",
            "resolved_script",
            "resolved_wrapper",
            "resolved_env_file",
            "launcher_argv",
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
    expected_execution = _invocation_execution_identity(kind)
    if any(record[key] != value for key, value in expected_execution.items()):
        raise EvidenceError(f"{label} resolved launcher provenance differs")
    started = _parse_utc(record["started_at"], f"{label}.started_at")
    finished = _parse_utc(record["finished_at"], f"{label}.finished_at")
    if (
        not started < finished
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


def _validate_execution_audit(
    raw: Any,
    *,
    expected_invocation_refs: list[Mapping[str, Any]],
    mutation_head_sha: str,
) -> dict[str, Any]:
    audit = _require_mapping(raw, "execution.audit document")
    _require_exact_keys(
        audit,
        {
            "captured_at",
            "window_started_at",
            "window_finished_at",
            "mutation_head_sha",
            "audit_source",
            "complete",
            "namespace_counts",
            "invocation_refs",
            "direct_db_mutation_statements",
            "journal",
        },
        "execution.audit document",
    )
    started = _parse_utc(audit["window_started_at"], "execution audit start")
    finished = _parse_utc(audit["window_finished_at"], "execution audit finish")
    captured = _parse_utc(audit["captured_at"], "execution audit captured")
    expected_counts = {
        "migration_apply": 2,
        "recovery_decompress": 1,
        "compression_dry_run": 1,
        "compression_enforce": 1,
    }
    refs = _require_list(audit["invocation_refs"], "execution invocation refs")
    expected_refs = [dict(value) for value in expected_invocation_refs]
    if (
        audit["mutation_head_sha"] != mutation_head_sha
        or audit["audit_source"] != "pgaudit+systemd-journal"
        or audit["complete"] is not True
        or audit["namespace_counts"] != expected_counts
        or refs != expected_refs
        or audit["direct_db_mutation_statements"] != []
        or not started < finished < captured
    ):
        raise EvidenceError("execution audit namespace or direct-DB boundary differs")
    journal_ref, journal = _text_artifact(audit["journal"], "execution.audit.journal")
    expected_lines = [
        f"kind={kind} invocation_sha256={ref['sha256']}"
        for kind, ref in zip(
            [
                "migration_apply",
                "migration_apply",
                "recovery_decompress",
                "compression_dry_run",
                "compression_enforce",
            ],
            expected_refs,
            strict=True,
        )
    ]
    observed_lines = [line.strip() for line in journal.splitlines() if line.strip()]
    if observed_lines != [*expected_lines, "direct_db_mutation_statements=0"]:
        raise EvidenceError("execution audit journal cardinality/commands differ")
    return {
        "artifact_window_started_at": started,
        "artifact_window_finished_at": finished,
        "captured_at": captured,
        "journal": journal_ref,
        "namespace_counts": expected_counts,
    }


def _concrete_argv(value: Any, label: str) -> list[str]:
    argv = _require_list(value, label)
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise EvidenceError(f"{label} must contain concrete executable arguments")
    forbidden = ("<", ">", "${", "$(", "{{", "}}", "*", "?")
    if any(any(marker in item for marker in forbidden) for item in argv):
        raise EvidenceError(f"{label} contains a placeholder or shell template")
    if not Path(argv[0]).is_absolute():
        raise EvidenceError(f"{label} executable is not absolute")
    return list(argv)


def _validate_exact_command_argv(argv: list[str], *, kind: str, associations: Mapping[str, Any], label: str) -> None:
    expected_executable = {
        "pg_dump": "/usr/bin/pg_dump",
        "pg_restore_version": "/usr/bin/docker",
        "pg_restore_list": "/usr/bin/docker",
        "migration_apply": "/usr/bin/psql",
        "decompress": f"{EXPECTED_REPO_PATH}/.venv/bin/python",
        "compression_dry_run": f"{EXPECTED_REPO_PATH}/scripts/node27_timeseries_compression_once.sh",
        "compression_enforce": f"{EXPECTED_REPO_PATH}/scripts/node27_timeseries_compression_once.sh",
        "benchmark_before": f"{EXPECTED_REPO_PATH}/.venv/bin/python",
        "benchmark_after": f"{EXPECTED_REPO_PATH}/.venv/bin/python",
    }[kind]
    if argv[0] != expected_executable:
        raise EvidenceError(f"{label} executable differs from the canonical {kind} contract")
    if kind == "pg_dump" and argv != [
        "/usr/bin/pg_dump",
        "--dbname",
        "nhms",
        "--format=custom",
        "--schema-only",
        "--file",
        str(associations.get("schema_dump", "")),
    ]:
        raise EvidenceError("pg_dump argv differs")
    if kind == "pg_restore_version" and argv != [
        "/usr/bin/docker",
        "exec",
        "nhms-db",
        "/usr/bin/pg_restore",
        "--version",
    ]:
        raise EvidenceError("pg_restore version argv differs")
    if kind == "pg_restore_list" and (
        argv[:5] != ["/usr/bin/docker", "exec", "nhms-db", "/usr/bin/pg_restore", "--list"]
        or len(argv) != 6
        or not argv[-1].startswith("/var/lib/postgresql/")
    ):
        raise EvidenceError("pg_restore list argv differs")
    migration_argv = [
        "/usr/bin/psql",
        "--dbname",
        "nhms",
        "--no-psqlrc",
        "--set",
        "ON_ERROR_STOP=1",
        "--file",
        f"{EXPECTED_REPO_PATH}/db/migrations/000047_hypertable_compression_settings.sql",
    ]
    if kind == "migration_apply" and argv != migration_argv:
        raise EvidenceError("migration argv differs")
    if kind == "decompress" and (
        len(argv) != 20
        or argv[:5]
        != [
            f"{EXPECTED_REPO_PATH}/.venv/bin/python",
            f"{EXPECTED_REPO_PATH}/scripts/node27_timeseries_decompression_replay.py",
            "--database",
            "nhms",
            "--mutation-head-sha",
        ]
        or re.fullmatch(r"[0-9a-f]{40}", argv[5]) is None
        or argv[6:]
        != [
            "--receipt-path",
            str(associations.get("recovery_receipt", "")),
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
        ]
    ):
        raise EvidenceError("decompress argv differs")
    if kind.startswith("compression_"):
        enforce = kind == "compression_enforce"
        prefix = [expected_executable, *(["--enforce"] if enforce else [])]
        if (
            argv[: len(prefix)] != prefix
            or len(argv) != len(prefix) + 4
            or set(argv[len(prefix) :: 2])
            != {
                "--receipt-path",
                "--lock-path",
            }
        ):
            raise EvidenceError(f"{kind} option contract differs")
        receipt = "enforce_receipt" if enforce else "dry_run_receipt"
        if argv[argv.index("--receipt-path") + 1] != associations.get(receipt):
            raise EvidenceError(f"{kind} receipt output differs")
    if kind.startswith("benchmark_"):
        if len(argv) < 4 or argv[1] != f"{EXPECTED_REPO_PATH}/scripts/node27_timeseries_compression_benchmark.py":
            raise EvidenceError(f"{kind} benchmark entrypoint differs")
        flags = argv[2::2]
        expected_flags = [
            "--phase",
            *(["--before-path"] if kind == "benchmark_after" else []),
            "--output",
            "--curve-basin-version-id",
            "--curve-river-segment-id",
            "--curve-river-network-version-id",
            "--curve-issue-time",
            "--curve-end-time",
            "--curve-scenario",
            "--mvt-run-id",
            "--mvt-basin-version-id",
            "--mvt-river-network-version-id",
            "--mvt-valid-time",
            "--mvt-z",
            "--mvt-x",
            "--mvt-y",
        ]
        if flags != expected_flags:
            raise EvidenceError(f"{kind} benchmark option order differs")


def _supervisor_run_plan_id(plan: Mapping[str, Any]) -> str:
    return _sha256(_canonical_json_bytes({**dict(plan), "run_plan_id": ""}))


def _observed_artifact(value: Any, label: str, *, json_value: bool = False) -> tuple[dict[str, Any], Any]:
    observed = _require_mapping(value, label)
    _require_exact_keys(observed, {"artifact", "device", "inode"}, label)
    if not isinstance(observed["device"], int) or not isinstance(observed["inode"], int):
        raise EvidenceError(f"{label} descriptor identity is invalid")
    if json_value:
        ref, raw = _json_artifact(observed["artifact"], f"{label}.artifact")
    else:
        ref, raw = _artifact_bytes(observed["artifact"], f"{label}.artifact", max_bytes=MAX_BINARY_ARTIFACT_BYTES)
    try:
        identity = inspect_bounded_file_no_follow(Path(ref["path"]), max_bytes=max(ref["bytes"], 1), label=label)
    except BoundedEvidenceError as error:
        raise EvidenceError(str(error)) from error
    if (identity.device, identity.inode) != (observed["device"], observed["inode"]):
        raise EvidenceError(f"{label} inode identity changed")
    return ref, raw


def _journal_governed_user_unit(row: Mapping[str, Any], *, label: str) -> str | None:
    governed = {
        "nhms-node27-timeseries-compression.service",
        "nhms-node27-timeseries-compression-replay.service",
    }
    user_fields_present = "_SYSTEMD_USER_UNIT" in row or "USER_UNIT" in row
    user_units = {
        str(row[key])
        for key in ("_SYSTEMD_USER_UNIT", "USER_UNIT")
        if key in row and str(row[key]) in governed
    }
    if len(user_units) > 1:
        raise EvidenceError(f"{label} journal user-unit fields conflict")
    if user_units:
        return next(iter(user_units))
    if user_fields_present:
        return None
    fallback_units = {
        str(row[key])
        for key in ("_SYSTEMD_UNIT", "UNIT")
        if key in row and str(row[key]) in governed
    }
    if len(fallback_units) > 1:
        raise EvidenceError(f"{label} journal fallback unit fields conflict")
    return next(iter(fallback_units)) if fallback_units else None


def _validate_checkpoint_artifacts(
    event: Mapping[str, Any], label: str, *, invocation_id: str, supervisor_pid: int
) -> dict[str, Any]:
    activity_ref, activity_raw = _observed_artifact(
        event["database_activity"], f"{label}.database_activity", json_value=True
    )
    locks_ref, locks_raw = _observed_artifact(event["relation_locks"], f"{label}.relation_locks", json_value=True)
    catalog_ref, catalog_raw = _observed_artifact(event["catalog"], f"{label}.catalog", json_value=True)
    show_ref, show_raw = _observed_artifact(event["systemd_show"], f"{label}.systemd_show", json_value=True)
    journal_ref, journal_raw = _observed_artifact(event["journal"], f"{label}.journal")
    try:
        journal = journal_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvidenceError(f"{label}.journal is not UTF-8") from error
    _reject_secrets(journal, f"{label}.journal")
    activity = _require_mapping(activity_raw, f"{label}.database_activity document")
    locks = _require_mapping(locks_raw, f"{label}.relation_locks document")
    show = _require_mapping(show_raw, f"{label}.systemd_show document")
    if activity != {"sessions": []} or locks != {"conflicts": []}:
        raise EvidenceError(f"{label} observed a conflicting writer or relation lock")
    _validate_d3_catalog(catalog_raw, f"{label}.catalog document")
    recurring = _require_mapping(show.get("recurring"), f"{label}.systemd_show.recurring")
    replay = _require_mapping(show.get("replay"), f"{label}.systemd_show.replay")
    if recurring != {
        "FragmentPath": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.service",
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": 0,
        "InvocationID": "",
        "ExecMainStartTimestamp": "",
        "ExecMainStartTimestampMonotonic": 0,
    }:
        raise EvidenceError(f"{label} recurring compression unit is not canonically inactive")
    if (
        replay.get("FragmentPath") != "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression-replay.service"
        or replay.get("ActiveState") != "activating"
        or replay.get("SubState") != "start"
        or replay.get("MainPID") != supervisor_pid
        or replay.get("InvocationID") != invocation_id
        or not isinstance(replay.get("ExecMainStartTimestamp"), str)
        or not replay["ExecMainStartTimestamp"]
        or not isinstance(replay.get("ExecMainStartTimestampMonotonic"), int)
        or replay["ExecMainStartTimestampMonotonic"] <= 0
    ):
        raise EvidenceError(f"{label} replay supervisor unit is not the active owner")
    start_cursor = event.get("journal_start_cursor")
    end_cursor = event.get("journal_end_cursor")
    if not isinstance(start_cursor, str) or not start_cursor or not isinstance(end_cursor, str) or not end_cursor:
        raise EvidenceError(f"{label} journal cursor boundary is missing")
    cursor_lines = [line.removeprefix("-- cursor: ") for line in journal.splitlines() if line.startswith("-- cursor: ")]
    if not cursor_lines or cursor_lines[-1] != end_cursor:
        raise EvidenceError(f"{label} journal ending cursor differs")
    replay_ids: set[str] = set()
    recurring_ids: set[str] = set()
    for line in journal.splitlines():
        if not line or line.startswith("-- cursor: "):
            continue
        try:
            row = _require_mapping(json.loads(line), f"{label}.journal row")
        except json.JSONDecodeError as error:
            raise EvidenceError(f"{label}.journal is not structured JSON") from error
        unit = _journal_governed_user_unit(row, label=label)
        observed_id = row.get("_SYSTEMD_INVOCATION_ID") or row.get("INVOCATION_ID")
        if unit == "nhms-node27-timeseries-compression-replay.service" and observed_id:
            replay_ids.add(str(observed_id))
        if unit == "nhms-node27-timeseries-compression.service" and observed_id:
            recurring_ids.add(str(observed_id))
    if recurring_ids:
        raise EvidenceError(f"{label} journal observed recurring compression activation")
    if any(value != invocation_id for value in replay_ids):
        raise EvidenceError(f"{label} journal observed an additional replay activation")
    return {
        "database_activity": activity_ref,
        "relation_locks": locks_ref,
        "catalog": catalog_ref,
        "systemd_show": show_ref,
        "journal": journal_ref,
        "journal_start_cursor": start_cursor,
        "journal_end_cursor": end_cursor,
        "replay_activation": {
            "InvocationID": replay["InvocationID"],
            "MainPID": replay["MainPID"],
            "ExecMainStartTimestamp": replay["ExecMainStartTimestamp"],
            "ExecMainStartTimestampMonotonic": replay["ExecMainStartTimestampMonotonic"],
        },
    }


def _validate_supervisor_execution(
    execution: Mapping[str, Any], *, mutation_head_sha: str, database: str
) -> dict[str, Any]:
    """Derive controlled-lane cardinality and quiescence from producer raw facts."""

    _require_exact_keys(execution, {"run_plan", "ledger"}, "execution")
    run_plan_ref, run_plan_raw = _json_artifact(execution["run_plan"], "execution.run_plan")
    plan = _require_mapping(run_plan_raw, "execution.run_plan document")
    _require_exact_keys(
        plan,
        {
            "plan_version",
            "run_plan_id",
            "mutation_head_sha",
            "reviewed_remote_ref",
            "database",
            "repo_path",
            "operator_attestation",
            "commands",
            "captures",
            "checkpoints",
        },
        "execution.run_plan document",
    )
    if (
        plan["plan_version"] != "1.0"
        or plan["run_plan_id"] != _supervisor_run_plan_id(plan)
        or plan["mutation_head_sha"] != mutation_head_sha
        or plan["database"] != database
        or plan["repo_path"] != EXPECTED_REPO_PATH
        or plan["reviewed_remote_ref"] != "refs/remotes/origin/feat/issue-1069-live-compression"
    ):
        raise EvidenceError("supervisor run plan provenance differs")
    attestation = _require_mapping(plan["operator_attestation"], "execution.run_plan.operator_attestation")
    expected_attestation = {
        "sole_db_user_during_window": True,
        "database_audit_proof": False,
        "trust_limit": "discrete observations; no absolute direct-SQL bypass proof",
    }
    if attestation != expected_attestation:
        raise EvidenceError("supervisor sole-user attestation/trust limit differs")
    commands = _require_list(plan["commands"], "execution.run_plan.commands")
    command_by_id: dict[str, Mapping[str, Any]] = {}
    planned_counts: dict[str, int] = {key: 0 for key in EXPECTED_COMMAND_COUNTS}
    planned_output_owners: dict[str, tuple[str, int]] = {}
    for index, command_value in enumerate(commands):
        command = _require_mapping(command_value, f"run plan command[{index}]")
        _require_exact_keys(
            command,
            {"command_id", "kind", "argv", "artifact_associations"},
            f"run plan command[{index}]",
        )
        command_id = str(command["command_id"])
        kind = str(command["kind"])
        if command_id in command_by_id or kind not in EXPECTED_LEDGER_SEQUENCE:
            raise EvidenceError("run plan command ID/kind is unowned")
        argv = _concrete_argv(command["argv"], f"run plan command[{index}].argv")
        associations = _require_mapping(command["artifact_associations"], f"run plan command[{index}].artifacts")
        if any(
            not isinstance(name, str) or not name or not isinstance(path, str) or not Path(path).is_absolute()
            for name, path in associations.items()
        ):
            raise EvidenceError("run plan produced-artifact paths are not concrete")
        _validate_exact_command_argv(argv, kind=kind, associations=associations, label=f"run plan command[{index}]")
        ordinal = planned_counts[kind]
        for name in associations:
            if name in planned_output_owners:
                raise EvidenceError("run plan output label has duplicate producers")
            planned_output_owners[name] = (kind, ordinal)
        command_by_id[command_id] = command
        planned_counts[kind] += 1
    if planned_counts != dict(EXPECTED_COMMAND_COUNTS):
        raise EvidenceError("supervisor run plan cardinality differs")
    if tuple(str(command["kind"]) for command in commands) != EXPECTED_LEDGER_SEQUENCE:
        raise EvidenceError("supervisor run plan command order differs")
    decompression = next(command for command in commands if command["kind"] == "decompress")
    if decompression["argv"][5] != mutation_head_sha:
        raise EvidenceError("decompression producer mutation SHA differs")
    captures = _require_list(plan["captures"], "execution.run_plan.captures")
    capture_by_id: dict[str, Mapping[str, Any]] = {}
    for index, capture_value in enumerate(captures):
        capture = _require_mapping(capture_value, f"run plan capture[{index}]")
        _require_exact_keys(
            capture,
            {"capture_id", "kind", "argv", "output_path"},
            f"run plan capture[{index}]",
        )
        capture_id = str(capture["capture_id"])
        kind = str(capture["kind"])
        output_path = capture["output_path"]
        if (
            not capture_id
            or capture_id in capture_by_id
            or not isinstance(output_path, str)
            or not Path(output_path).is_absolute()
        ):
            raise EvidenceError("run plan capture identity/output differs")
        _concrete_argv(capture["argv"], f"run plan capture[{index}].argv")
        if kind in planned_output_owners:
            raise EvidenceError("run plan output label has duplicate producers")
        planned_output_owners[kind] = (f"capture:{kind}", 0)
        capture_by_id[capture_id] = capture
    if tuple(str(item["kind"]) for item in captures) != EXPECTED_CAPTURE_SEQUENCE:
        raise EvidenceError("run plan capture order/cardinality differs")
    if planned_output_owners != dict(EXPECTED_OUTPUT_OWNERS):
        raise EvidenceError("run plan semantic output ownership bijection differs")
    checkpoints = _require_list(plan["checkpoints"], "execution.run_plan.checkpoints")
    planned_checkpoint_by_id: dict[str, Mapping[str, Any]] = {}
    for item in checkpoints:
        checkpoint = _require_mapping(item, "run plan checkpoint")
        _require_exact_keys(checkpoint, {"checkpoint_id", "phase", "command_id"}, "run plan checkpoint")
        checkpoint_id = str(checkpoint["checkpoint_id"])
        if checkpoint_id in planned_checkpoint_by_id:
            raise EvidenceError("supervisor checkpoint IDs are not unique")
        planned_checkpoint_by_id[checkpoint_id] = checkpoint
    planned_checkpoint_ids = set(planned_checkpoint_by_id)
    if len(planned_checkpoint_ids) != len(checkpoints):
        raise EvidenceError("supervisor checkpoint IDs are not unique")
    mutation_command_ids = {
        command_id
        for command_id, command in command_by_id.items()
        if command["kind"] in {"migration_apply", "decompress", "compression_enforce"}
    }
    global_phases = [
        checkpoint["phase"] for checkpoint in planned_checkpoint_by_id.values() if checkpoint["command_id"] is None
    ]
    before_ids = {
        str(checkpoint["command_id"])
        for checkpoint in planned_checkpoint_by_id.values()
        if checkpoint["phase"] == "before_mutation"
    }
    after_ids = {
        str(checkpoint["command_id"])
        for checkpoint in planned_checkpoint_by_id.values()
        if checkpoint["phase"] == "after_mutation"
    }
    if (
        sorted(global_phases) != ["cleanup", "postflight", "preflight"]
        or before_ids != mutation_command_ids
        or after_ids != mutation_command_ids
    ):
        raise EvidenceError("supervisor checkpoint/run-plan bijection differs")

    ledger_ref, ledger_text = _text_artifact(execution["ledger"], "execution.ledger", max_bytes=MAX_JSON_ARTIFACT_BYTES)
    try:
        events = [json.loads(line) for line in ledger_text.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise EvidenceError("supervisor ledger is not append-only JSONL") from error
    if not events:
        raise EvidenceError("supervisor ledger is empty")
    event_ids: set[str] = set()
    observed_commands: set[str] = set()
    observed_command_order: list[str] = []
    observed_captures: set[str] = set()
    observed_capture_order: list[str] = []
    observed_producer_sequence: list[str] = []
    observed_checkpoints: set[str] = set()
    observed_counts: dict[str, int] = {key: 0 for key in EXPECTED_LEDGER_COUNTS}
    run_ids: set[str] = set()
    invocation_ids: set[str] = set()
    supervisor_pids: set[int] = set()
    replay_activations: set[tuple[Any, ...]] = set()
    monotonic_finishes: list[float] = []
    child_intervals: dict[str, tuple[datetime, datetime]] = {}
    child_mono_intervals: dict[str, tuple[float, float]] = {}
    child_event_indexes: dict[str, int] = {}
    child_indexes_by_kind: dict[str, list[int]] = {kind: [] for kind in EXPECTED_LEDGER_SEQUENCE}
    capture_event_indexes: dict[str, int] = {}
    checkpoint_times: dict[str, datetime] = {}
    checkpoint_monos: dict[str, float] = {}
    checkpoint_event_indexes: dict[str, int] = {}
    checkpoint_refs: list[dict[str, Any]] = []
    events_by_kind: dict[str, list[dict[str, Any]]] = {key: [] for key in EXPECTED_LEDGER_COUNTS}
    capture_events_by_kind: dict[str, dict[str, Any]] = {}
    started: datetime | None = None
    finished: datetime | None = None
    previous_event_mono = float("-inf")
    previous_journal_end: str | None = None
    for index, event_value in enumerate(events):
        event = _require_mapping(event_value, f"supervisor ledger event[{index}]")
        event_id = str(event.get("event_id", ""))
        if not event_id or event_id in event_ids:
            raise EvidenceError("supervisor ledger event IDs are not unique")
        event_ids.add(event_id)
        if event.get("schema_version") != SCHEMA_VERSION or event.get("run_plan_id") != plan["run_plan_id"]:
            raise EvidenceError("supervisor ledger run-plan identity differs")
        run_ids.add(str(event.get("run_id", "")))
        invocation_id = str(event.get("invocation_id", ""))
        if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
            raise EvidenceError("supervisor ledger INVOCATION_ID is invalid")
        invocation_ids.add(invocation_id)
        supervisor_pid = event.get("supervisor_pid")
        if not isinstance(supervisor_pid, int) or supervisor_pid < 1:
            raise EvidenceError("supervisor ledger PID is invalid")
        supervisor_pids.add(supervisor_pid)
        event_type = event.get("event_type")
        if event_type == "child_exit":
            _require_exact_keys(
                event,
                {
                    "schema_version",
                    "run_id",
                    "run_plan_id",
                    "invocation_id",
                    "supervisor_pid",
                    "event_id",
                    "event_type",
                    "command_id",
                    "kind",
                    "argv",
                    "pid",
                    "started_at",
                    "finished_at",
                    "started_monotonic",
                    "finished_monotonic",
                    "exit_code",
                    "terminated_by_supervisor",
                    "possible_mutation",
                    "stdout",
                    "stderr",
                    "mutation_head_sha",
                    "database",
                    "artifact_associations",
                },
                f"supervisor child[{index}]",
            )
            command_id = str(event.get("command_id", ""))
            if command_id in observed_commands or command_id not in command_by_id:
                raise EvidenceError("supervisor ledger has a missing/extra/unowned child")
            command = command_by_id[command_id]
            if (
                event.get("kind") != command["kind"]
                or event.get("argv") != command["argv"]
                or event.get("mutation_head_sha") != mutation_head_sha
                or event.get("database") != database
                or not isinstance(event.get("pid"), int)
                or event["pid"] < 1
                or event.get("exit_code") != 0
                or event.get("terminated_by_supervisor") is not False
                or event.get("possible_mutation") is not False
            ):
                raise EvidenceError("supervisor child execution differs from the immutable plan")
            observed_associations = _require_mapping(
                event.get("artifact_associations"), f"supervisor child[{index}].artifact_associations"
            )
            planned_associations = _require_mapping(
                command["artifact_associations"], f"run plan command[{index}].artifact_associations"
            )
            if str(event["kind"]) == "pg_restore_version":
                required_identity = {"dump_sha256", "container_image_id", "binary_realpath", "binary_sha256"}
                if set(observed_associations) != required_identity:
                    raise EvidenceError("pg_restore observed identity is incomplete")
            else:
                identity_names = (
                    {"dump_sha256", "container_image_id", "binary_realpath", "binary_sha256"}
                    if str(event["kind"]) == "pg_restore_list"
                    else set()
                )
                if set(observed_associations) != set(planned_associations) | identity_names:
                    raise EvidenceError("supervisor produced-artifact association set differs")
                for name, path in planned_associations.items():
                    ref, _ = _observed_artifact(
                        observed_associations[name],
                        f"supervisor child[{index}].artifact_associations.{name}",
                    )
                    if ref["path"] != path:
                        raise EvidenceError("supervisor observed artifact path differs from run plan output")
            started_at = _parse_utc(event.get("started_at"), "supervisor child start")
            finished_at = _parse_utc(event.get("finished_at"), "supervisor child finish")
            started_mono = event.get("started_monotonic")
            finished_mono = event.get("finished_monotonic")
            if (
                not started_at < finished_at
                or not isinstance(started_mono, (int, float))
                or not isinstance(finished_mono, (int, float))
                or not started_mono < finished_mono
                or started_mono <= previous_event_mono
                or not math.isclose(
                    (finished_at - started_at).total_seconds(),
                    float(finished_mono) - float(started_mono),
                    abs_tol=0.5,
                )
            ):
                raise EvidenceError("supervisor child chronology is not strictly ordered")
            for stream in ("stdout", "stderr"):
                identity = _require_mapping(event.get(stream), f"supervisor child {stream}")
                stream_ref, stream_raw = _observed_artifact(
                    identity.get("artifact"),
                    f"supervisor child {stream}.artifact",
                )
                if (
                    set(identity) != {"bytes", "sha256", "truncated", "artifact"}
                    or not isinstance(identity["bytes"], int)
                    or not 0 <= identity["bytes"] <= MAX_SUBPROCESS_OUTPUT_BYTES
                    or re.fullmatch(r"[0-9a-f]{64}", str(identity["sha256"])) is None
                    or identity["truncated"] is not False
                    or identity["bytes"] != len(stream_raw)
                    or identity["sha256"] != _sha256(stream_raw)
                    or stream_ref["bytes"] != identity["bytes"]
                ):
                    raise EvidenceError("supervisor child output identity differs")
            monotonic_finishes.append(float(finished_mono))
            previous_event_mono = float(finished_mono)
            observed_commands.add(command_id)
            observed_command_order.append(command_id)
            child_intervals[command_id] = (started_at, finished_at)
            child_mono_intervals[command_id] = (float(started_mono), float(finished_mono))
            child_event_indexes[command_id] = index
            child_indexes_by_kind[str(event["kind"])].append(index)
            observed_counts[str(event["kind"])] += 1
            observed_producer_sequence.append(
                f"child:{event['kind']}:{observed_counts[str(event['kind'])] - 1}"
            )
            events_by_kind[str(event["kind"])].append(dict(event))
            started = min(started or started_at, started_at)
            finished = max(finished or finished_at, finished_at)
        elif event_type == "capture":
            _require_exact_keys(
                event,
                {
                    "schema_version",
                    "run_id",
                    "run_plan_id",
                    "invocation_id",
                    "supervisor_pid",
                    "event_id",
                    "event_type",
                    "capture_id",
                    "kind",
                    "argv",
                    "pid",
                    "started_at",
                    "finished_at",
                    "started_monotonic",
                    "finished_monotonic",
                    "exit_code",
                    "terminated_by_supervisor",
                    "stdout",
                    "stderr",
                    "artifact_association",
                },
                f"supervisor capture[{index}]",
            )
            capture_id = str(event["capture_id"])
            if capture_id in observed_captures or capture_id not in capture_by_id:
                raise EvidenceError("supervisor ledger has an extra/missing capture owner")
            capture = capture_by_id[capture_id]
            if (
                event["kind"] != capture["kind"]
                or event["argv"] != capture["argv"]
                or event["exit_code"] != 0
                or event["terminated_by_supervisor"] is not False
                or not isinstance(event["pid"], int)
                or event["pid"] < 1
            ):
                raise EvidenceError("supervisor capture execution differs from its plan")
            ref, _ = _observed_artifact(
                event["artifact_association"],
                f"supervisor capture[{index}].artifact_association",
            )
            if ref["path"] != capture["output_path"]:
                raise EvidenceError("supervisor capture output path differs")
            started_at = _parse_utc(event["started_at"], "supervisor capture start")
            finished_at = _parse_utc(event["finished_at"], "supervisor capture finish")
            started_mono = event["started_monotonic"]
            finished_mono = event["finished_monotonic"]
            if (
                not started_at < finished_at
                or not isinstance(started_mono, (int, float))
                or not isinstance(finished_mono, (int, float))
                or not started_mono < finished_mono
                or started_mono <= previous_event_mono
            ):
                raise EvidenceError("supervisor capture chronology is not strict")
            for stream in ("stdout", "stderr"):
                identity = _require_mapping(event[stream], f"supervisor capture {stream}")
                stream_ref, stream_raw = _observed_artifact(
                    identity.get("artifact"), f"supervisor capture {stream}.artifact"
                )
                if (
                    set(identity) != {"bytes", "sha256", "truncated", "artifact"}
                    or identity["truncated"] is not False
                    or identity["bytes"] != len(stream_raw)
                    or identity["sha256"] != _sha256(stream_raw)
                    or stream_ref["bytes"] != identity["bytes"]
                ):
                    raise EvidenceError("supervisor capture output identity differs")
            previous_event_mono = float(finished_mono)
            monotonic_finishes.append(float(finished_mono))
            observed_captures.add(capture_id)
            observed_capture_order.append(capture_id)
            observed_producer_sequence.append(f"capture:{event['kind']}")
            capture_event_indexes[str(event["kind"])] = index
            capture_events_by_kind[str(event["kind"])] = dict(event)
            started = min(started or started_at, started_at)
            finished = max(finished or finished_at, finished_at)
        elif event_type == "checkpoint":
            _require_exact_keys(
                event,
                {
                    "schema_version",
                    "run_id",
                    "run_plan_id",
                    "invocation_id",
                    "supervisor_pid",
                    "event_id",
                    "event_type",
                    "checkpoint_id",
                    "phase",
                    "command_id",
                    "captured_at",
                    "monotonic",
                    "database_activity",
                    "relation_locks",
                    "catalog",
                    "systemd_show",
                    "journal",
                    "journal_start_cursor",
                    "journal_end_cursor",
                },
                f"supervisor checkpoint[{index}]",
            )
            checkpoint_id = str(event.get("checkpoint_id", ""))
            if checkpoint_id in observed_checkpoints or checkpoint_id not in planned_checkpoint_ids:
                raise EvidenceError("supervisor ledger checkpoint differs from the run plan")
            planned_checkpoint = planned_checkpoint_by_id[checkpoint_id]
            if event["phase"] != planned_checkpoint["phase"] or event["command_id"] != planned_checkpoint["command_id"]:
                raise EvidenceError("supervisor checkpoint binding differs from the run plan")
            checkpoint_result = _validate_checkpoint_artifacts(
                event,
                f"supervisor checkpoint[{index}]",
                invocation_id=invocation_id,
                supervisor_pid=supervisor_pid,
            )
            activation = checkpoint_result["replay_activation"]
            replay_activations.add(
                (
                    activation["InvocationID"],
                    activation["MainPID"],
                    activation["ExecMainStartTimestamp"],
                    activation["ExecMainStartTimestampMonotonic"],
                )
            )
            if previous_journal_end is not None and checkpoint_result["journal_start_cursor"] != previous_journal_end:
                raise EvidenceError("supervisor journal cursor continuity differs")
            previous_journal_end = str(checkpoint_result["journal_end_cursor"])
            checkpoint_refs.extend(
                value
                for key, value in checkpoint_result.items()
                if key not in {"journal_start_cursor", "journal_end_cursor", "replay_activation"}
            )
            checkpoint_at = _parse_utc(event["captured_at"], f"supervisor checkpoint[{index}].captured_at")
            checkpoint_times[checkpoint_id] = checkpoint_at
            checkpoint_mono = event["monotonic"]
            if not isinstance(checkpoint_mono, (int, float)) or checkpoint_mono <= previous_event_mono:
                raise EvidenceError("supervisor checkpoint monotonic value is invalid")
            previous_event_mono = float(checkpoint_mono)
            checkpoint_monos[checkpoint_id] = float(checkpoint_mono)
            checkpoint_event_indexes[checkpoint_id] = index
            started = min(started or checkpoint_at, checkpoint_at)
            finished = max(finished or checkpoint_at, checkpoint_at)
            observed_checkpoints.add(checkpoint_id)
        else:
            raise EvidenceError("supervisor ledger contains an unowned event type")
    if tuple(observed_producer_sequence) != EXPECTED_PRODUCER_SEQUENCE:
        raise EvidenceError("supervisor capture owner chronology differs")
    if (
        len(run_ids) != 1
        or "" in run_ids
        or len(invocation_ids) != 1
        or len(supervisor_pids) != 1
        or len(replay_activations) != 1
        or observed_commands != set(command_by_id)
        or observed_command_order != list(command_by_id)
        or observed_captures != set(capture_by_id)
        or observed_capture_order != list(capture_by_id)
        or observed_checkpoints != planned_checkpoint_ids
        or {**observed_counts, "replay_supervisor_activation": len(replay_activations)}
        != dict(EXPECTED_LEDGER_COUNTS)
        or started is None
        or finished is None
    ):
        raise EvidenceError("supervisor ledger cardinality/checkpoint coverage differs")
    capture_constraints = (
        ("preflight_evidence", "before", "pg_dump", 0),
        ("schema_dump_list", "after", "pg_restore_list", 0),
        ("catalog_before", "after", "schema_dump_list", 0),
        ("catalog_after_first", "after", "migration_apply", 0),
        ("catalog_after_second", "after", "migration_apply", 1),
        ("recovery_preflight", "before", "decompress", 0),
        ("post_dry_selection", "after", "compression_dry_run", 0),
        ("pre_enforce_selection", "before", "compression_enforce", 0),
        ("sizes_pre", "before", "compression_enforce", 0),
        ("sizes_post", "after", "compression_enforce", 0),
        ("catalog_post", "after", "sizes_post", 0),
        ("cleanup", "after", "benchmark_after", 0),
    )
    for capture_kind, relation, owner_kind, ordinal in capture_constraints:
        capture_index = capture_event_indexes[capture_kind]
        if owner_kind in capture_event_indexes:
            owner_index = capture_event_indexes[owner_kind]
        else:
            owner_index = child_indexes_by_kind[owner_kind][ordinal]
        if (relation == "before" and capture_index >= owner_index) or (
            relation == "after" and capture_index <= owner_index
        ):
            raise EvidenceError("supervisor capture owner chronology differs")
    for checkpoint_id, checkpoint in planned_checkpoint_by_id.items():
        command_id = checkpoint["command_id"]
        if command_id is None:
            continue
        child_started, child_finished = child_intervals[str(command_id)]
        child_started_mono, child_finished_mono = child_mono_intervals[str(command_id)]
        checkpoint_at = checkpoint_times[checkpoint_id]
        checkpoint_mono = checkpoint_monos[checkpoint_id]
        checkpoint_index = checkpoint_event_indexes[checkpoint_id]
        child_index = child_event_indexes[str(command_id)]
        if checkpoint["phase"] == "before_mutation" and not (
            checkpoint_at < child_started and checkpoint_mono < child_started_mono and checkpoint_index < child_index
        ):
            raise EvidenceError("supervisor before-mutation checkpoint is not strict")
        if checkpoint["phase"] == "after_mutation" and not (
            child_finished < checkpoint_at and child_finished_mono < checkpoint_mono and child_index < checkpoint_index
        ):
            raise EvidenceError("supervisor after-mutation checkpoint is not strict")
    global_times = {
        str(checkpoint["phase"]): checkpoint_times[checkpoint_id]
        for checkpoint_id, checkpoint in planned_checkpoint_by_id.items()
        if checkpoint["command_id"] is None
    }
    first_child = min(value[0] for value in child_intervals.values())
    last_child = max(value[1] for value in child_intervals.values())
    if not (
        global_times["preflight"] < first_child and last_child < global_times["postflight"] < global_times["cleanup"]
    ):
        raise EvidenceError("supervisor global checkpoint chronology is not strict")
    return {
        "run_plan": run_plan_ref,
        "ledger": ledger_ref,
        "run_id": next(iter(run_ids)),
        "namespace_counts": {**observed_counts, "replay_supervisor_activation": len(replay_activations)},
        "invocation_id": next(iter(invocation_ids)),
        "checkpoint_artifacts": checkpoint_refs,
        "artifact_window_started_at": started,
        "artifact_window_finished_at": finished,
        "attestation": expected_attestation,
        "events_by_kind": events_by_kind,
        "capture_events_by_kind": capture_events_by_kind,
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
        jsonschema.Draft7Validator(receipt_schema, format_checker=jsonschema.FormatChecker()).validate(receipt)
    except jsonschema.ValidationError as error:
        raise EvidenceError(f"{label} fails runner receipt schema: {error.message}") from error
    return ref, receipt


def _validate_d3_catalog(raw: Any, label: str) -> None:
    catalog = _require_mapping(raw, label)
    _require_exact_keys(catalog, {"hypertables", "compression_settings", "policy_jobs"}, label)
    hypertables = _require_mapping(catalog["hypertables"], f"{label}.hypertables")
    if set(hypertables) != set(HYPERTABLE_KEYS) or not all(hypertables[key] is True for key in HYPERTABLE_KEYS):
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
    if set(hypertables) != set(HYPERTABLE_KEYS):
        raise EvidenceError(f"{label} does not prove the exact pre-migration catalog")
    if all(value is True for value in hypertables.values()):
        _validate_d3_catalog(catalog, label)
        return
    if all(value is False for value in hypertables.values()):
        if catalog["compression_settings"] == [] and catalog["policy_jobs"] == []:
            return
    raise EvidenceError(f"{label} is neither pristine nor exact current D3 state")


def _catalog_snapshot(
    raw: Any,
    *,
    label: str,
    mutation_head_sha: str,
    phase: str,
    validator: Any,
) -> dict[str, Any]:
    snapshot = _require_mapping(raw, label)
    _require_exact_keys(
        snapshot,
        {"captured_at", "snapshot_id", "phase", "mutation_head_sha", "catalog"},
        label,
    )
    captured_at = _parse_utc(snapshot["captured_at"], f"{label}.captured_at")
    if (
        snapshot["phase"] != phase
        or snapshot["mutation_head_sha"] != mutation_head_sha
        or not isinstance(snapshot["snapshot_id"], str)
        or not snapshot["snapshot_id"]
    ):
        raise EvidenceError(f"{label} snapshot identity differs")
    validator(snapshot["catalog"], f"{label}.catalog")
    return {
        "captured_at_dt": captured_at,
        "snapshot_id": snapshot["snapshot_id"],
        "catalog": snapshot["catalog"],
    }


def _validate_dump_listing(
    raw: Any,
    *,
    dump_ref: Mapping[str, Any],
    mutation_head_sha: str,
) -> dict[str, Any]:
    listing = _require_mapping(raw, "preflight.schema_dump_list document")
    _require_exact_keys(
        listing,
        {
            "captured_at",
            "snapshot_id",
            "mutation_head_sha",
            "dump_descriptor_sha256",
            "container_image_id",
            "binary_realpath",
            "binary_sha256",
            "version_argv",
            "list_argv",
            "exit_code",
            "tool_version",
            "version_stdout_sha256",
            "version_stdout_bytes",
            "stdout_sha256",
            "stdout_bytes",
            "stderr_sha256",
            "stderr_bytes",
            "entries",
        },
        "preflight.schema_dump_list document",
    )
    captured_at = _parse_utc(listing["captured_at"], "preflight.schema_dump_list.captured_at")
    if (
        listing["mutation_head_sha"] != mutation_head_sha
        or not isinstance(listing["snapshot_id"], str)
        or not listing["snapshot_id"]
    ):
        raise EvidenceError("schema dump listing snapshot identity differs")
    entries = _require_list(listing["entries"], "preflight.schema_dump_list.entries")
    version_argv = _concrete_argv(listing["version_argv"], "pg_restore version argv")
    list_argv = _concrete_argv(listing["list_argv"], "pg_restore list argv")
    if (
        listing["dump_descriptor_sha256"] != dump_ref["sha256"]
        or version_argv != ["/usr/bin/docker", "exec", "nhms-db", "/usr/bin/pg_restore", "--version"]
        or list_argv[:5] != ["/usr/bin/docker", "exec", "nhms-db", "/usr/bin/pg_restore", "--list"]
        or len(list_argv) != 6
        or not list_argv[-1].startswith("/var/lib/postgresql/")
        or not isinstance(listing["container_image_id"], str)
        or not listing["container_image_id"].startswith("sha256:")
        or listing["binary_realpath"] != "/usr/bin/pg_restore"
        or re.fullmatch(r"[0-9a-f]{64}", str(listing["binary_sha256"])) is None
        or re.search(r"\b15(?:\.|\b)", str(listing["tool_version"])) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(listing["version_stdout_sha256"])) is None
        or not isinstance(listing["version_stdout_bytes"], int)
        or not 1 <= listing["version_stdout_bytes"] <= 4096
        or listing["exit_code"] != 0
        or not isinstance(listing["stdout_bytes"], int)
        or not 0 <= listing["stdout_bytes"] <= MAX_SUBPROCESS_OUTPUT_BYTES
        or not isinstance(listing["stderr_bytes"], int)
        or not 0 <= listing["stderr_bytes"] <= MAX_SUBPROCESS_OUTPUT_BYTES
        or re.fullmatch(r"[0-9a-f]{64}", str(listing["stdout_sha256"])) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(listing["stderr_sha256"])) is None
        or not entries
        or any(not isinstance(item, str) or not item for item in entries)
        or not all(any(table.split(".")[1] in item for item in entries) for table in HYPERTABLE_KEYS)
    ):
        raise EvidenceError("schema forensic dump/list identity is not verifiable")
    return {**dict(listing), "captured_at_dt": captured_at}


def _require_custom_dump_magic(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            if os.read(fd, 5) != b"PGDMP":
                raise EvidenceError("schema dump is not PostgreSQL custom format")
        finally:
            os.close(fd)
    except OSError as error:
        raise EvidenceError("schema dump magic is unavailable") from error


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
        or preflight["repo_path"] != EXPECTED_REPO_PATH
        or preflight["repo_remote_identity"] != EXPECTED_REMOTE_IDENTITY
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
        journal_ref, _ = _text_artifact(unit["journal"], f"preflight.units.{unit_name}.journal")
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
    prior_autopipe = _require_mapping(preflight["prior_autopipe_state"], "preflight.prior_autopipe_state")
    _require_exact_keys(
        prior_autopipe,
        {"timer", "service"},
        "preflight.prior_autopipe_state",
    )
    normalized_prior: dict[str, Any] = {}
    for kind in ("timer", "service"):
        state = _require_mapping(prior_autopipe[kind], f"preflight.prior_autopipe_state.{kind}")
        _require_exact_keys(
            state,
            {"enabled", "active", "sub", "result"},
            f"preflight.prior_autopipe_state.{kind}",
        )
        if not all(isinstance(value, str) and value for value in state.values()):
            raise EvidenceError("preflight prior autopipe state is incomplete")
        normalized_prior[kind] = dict(state)
    database = _require_mapping(preflight["database_identity"], "database_identity")
    probe = _require_mapping(preflight["database_identity_probe"], "preflight.database_identity_probe")
    _require_exact_keys(
        probe,
        {"captured_at", "query", "row"},
        "preflight.database_identity_probe",
    )
    expected_probe_query = (
        "SELECT current_database() AS dbname, "
        "current_setting('server_version') AS postgres_version, "
        "extversion AS timescaledb_version FROM pg_extension "
        "WHERE extname = 'timescaledb'"
    )
    probe_captured = _parse_utc(probe["captured_at"], "preflight.database_identity_probe.captured_at")
    if (
        database.get("dbname") != "nhms"
        or database.get("instance") != "node27-primary-pg15"
        or re.match(r"^15(?:\.|$)", str(database.get("postgres_version", ""))) is None
        or re.match(r"^2\.10(?:\.|$)", str(database.get("timescaledb_version", ""))) is None
        or probe["query"] != expected_probe_query
        or probe["row"] != database
        or probe_captured > captured_at
    ):
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
        "prior_autopipe_state": normalized_prior,
    }


def _validate_recovery(
    preflight_raw: Any,
    receipt_raw: Any,
    *,
    mutation_head_sha: str,
    database_identity: Mapping[str, Any],
    compression_preflight_captured_at: datetime,
) -> dict[str, Any]:
    recovery_preflight = _require_mapping(preflight_raw, "recovery.preflight document")
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
    recovery_safety = {key: recovery_preflight[key] for key in PREFLIGHT_KEYS}
    recovery_safety_summary = _validate_preflight(recovery_safety, mutation_head_sha)
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
    preflight_target = _require_mapping(recovery_preflight["target"], "recovery.preflight.target")
    receipt_target = _require_mapping(recovery_receipt["target"], "recovery.receipt.target")
    target_keys = set(RECOVERY_TARGET)
    _require_exact_keys(preflight_target, target_keys, "recovery.preflight.target")
    _require_exact_keys(receipt_target, target_keys, "recovery.receipt.target")
    if dict(preflight_target) != dict(RECOVERY_TARGET) or dict(receipt_target) != dict(RECOVERY_TARGET):
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
    if not isinstance(free_bytes, int) or isinstance(free_bytes, bool) or free_bytes < MIN_FREE_BYTES:
        raise EvidenceError("recovery free-space headroom is below 300 GiB")
    before_rows = recovery_preflight["before_row_count"]
    after_rows = recovery_receipt["after_row_count"]
    if recovery_preflight["before_compressed"] is not True or recovery_receipt["after_compressed"] is not False:
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
    if recovery_receipt["exit_code"] != 0 or recovery_receipt["decompress_return_relation"] != RECOVERY_RETURN_RELATION:
        raise EvidenceError("recovery decompression result is not the exact target relation")
    preflight_at = _parse_utc(recovery_safety_summary["captured_at"], "recovery.preflight.captured_at")
    started_at = _parse_utc(recovery_receipt["started_at"], "recovery.receipt.started_at")
    finished_at = _parse_utc(recovery_receipt["finished_at"], "recovery.receipt.finished_at")
    if not (preflight_at < started_at < finished_at < compression_preflight_captured_at):
        raise EvidenceError("recovery chronology must precede the compression preflight")
    return {
        "node": "node-27",
        "mutation_head_sha": mutation_head_sha,
        "target": dict(RECOVERY_TARGET),
        "preflight_captured_at": preflight_at.isoformat().replace("+00:00", "Z"),
        "decompress_started_at": started_at.isoformat().replace("+00:00", "Z"),
        "decompress_finished_at": finished_at.isoformat().replace("+00:00", "Z"),
        "compression_preflight_captured_at": compression_preflight_captured_at.isoformat().replace("+00:00", "Z"),
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
        {
            "captured_at",
            "snapshot_id",
            "phase",
            "mutation_head_sha",
            "selected_origin_uncompressed_index",
            "tables",
        },
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
    expected_uncompressed = -1 if expected_phase == "pre-enforce" else None
    if snapshot["selected_origin_uncompressed_index"] != expected_uncompressed:
        raise EvidenceError(f"{label} selected origin uncompressed-state differs")
    tables = _require_mapping(snapshot["tables"], f"{label}.tables")
    if set(tables) != set(HYPERTABLE_KEYS):
        raise EvidenceError(f"{label} must contain exactly both hypertables")
    all_origins: set[tuple[str, str]] = set()
    all_siblings: set[tuple[str, str]] = set()
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
        relations = _require_list(row["compressed_relations"], f"{label}.{key}.compressed_relations")
        if len(relations) != row["compressed_chunks"]:
            raise EvidenceError(f"{label}.{key} compressed count/relation list differs")
        table_origins: set[tuple[str, str]] = set()
        table_siblings: set[tuple[str, str]] = set()
        for index, relation_value in enumerate(relations):
            relation = _require_mapping(relation_value, f"{label}.{key}.compressed_relations[{index}]")
            _require_exact_keys(
                relation,
                {"origin_chunk_schema", "origin_chunk_name", "schema", "name", "bytes"},
                f"{label}.{key}.compressed_relations[{index}]",
            )
            if not isinstance(relation["bytes"], int) or relation["bytes"] < 0:
                raise EvidenceError(f"{label}.{key}.compressed_relations[{index}].bytes invalid")
            origin = (
                str(relation["origin_chunk_schema"]),
                str(relation["origin_chunk_name"]),
            )
            sibling = (str(relation["schema"]), str(relation["name"]))
            if (
                not all(origin)
                or not all(sibling)
                or origin == sibling
                or origin in table_origins
                or sibling in table_siblings
                or origin in all_origins
                or sibling in all_siblings
            ):
                raise EvidenceError(f"{label}.{key} compressed relation bijection differs")
            table_origins.add(origin)
            table_siblings.add(sibling)
        all_origins.update(table_origins)
        all_siblings.update(table_siblings)
    if all_origins & all_siblings:
        raise EvidenceError(f"{label} origin/compressed namespaces overlap")
    return {
        "captured_at_dt": captured_at,
        "snapshot_id": snapshot["snapshot_id"],
        "tables": tables,
        "selected_origin_uncompressed_index": snapshot["selected_origin_uncompressed_index"],
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
    cutoff_margin = int((cutoff - _parse_utc(selected_row["range_end"], f"{label} selected range_end")).total_seconds())
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


def _plan_binds_selected_decompress(plan: Any, *, selected_relation_names: set[str]) -> bool:
    """Require exact provider/relation fields on the qualifying Custom Scan."""

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
        provider = re.sub(r"[^a-z0-9]+", "", str(value.get("Custom Plan Provider", "")).lower())
        relation = str(value.get("Relation Name", "")).lower()
        schema = str(value.get("Schema", "")).lower()
        alias = str(value.get("Alias", "")).lower()
        if (
            node_type == "custom scan"
            and provider == "decompresschunk"
            and relation in lowered_names
            and schema == "_timescaledb_internal"
            and alias == "rt_1"
        ):
            return True
        stack.extend(value.values())
    return False


def _stats(samples_value: Any, label: str) -> tuple[list[float], float, float]:
    samples = _require_list(samples_value, label)
    if len(samples) != 7 or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or value < 0
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
    raw_plan = measurement["plan"]
    root = raw_plan[0] if isinstance(raw_plan, list) else raw_plan
    root = _require_mapping(root, f"{label}.plan root")
    derived_planning = root.get("Planning Time")
    derived_execution = root.get("Execution Time")
    plan_tree = root.get("Plan", root)
    if (
        not isinstance(derived_planning, (int, float))
        or not isinstance(derived_execution, (int, float))
        or float(measurement["planning_ms"]) != float(derived_planning)
        or float(measurement["execution_ms"]) != float(derived_execution)
    ):
        raise EvidenceError(f"{label} authored timing differs from the raw plan")
    from scripts.node27_timeseries_compression_benchmark import _walk_metric

    try:
        derived_hits = _walk_metric(plan_tree, "Shared Hit Blocks")
        derived_reads = _walk_metric(plan_tree, "Shared Read Blocks")
    except Exception as error:
        raise EvidenceError(f"{label} raw buffer metrics are invalid") from error
    if measurement["shared_hit_blocks"] != derived_hits or measurement["shared_read_blocks"] != derived_reads:
        raise EvidenceError(f"{label} authored buffers differ from the raw plan")
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
    raw_measurements = _require_list(phase["measurements"], "benchmark measurements")
    for index, raw_measurement_value in enumerate(raw_measurements):
        raw_measurement = _require_mapping(
            raw_measurement_value,
            f"benchmark {query_name} {phase_name}.measurements[{index}]",
        )
        binds = _plan_binds_selected_decompress(
            raw_measurement.get("plan"), selected_relation_names=selected_relation_names
        )
        if phase_name == "before" and binds:
            raise EvidenceError(
                f"benchmark {query_name} before measurement {index} already uses selected DecompressChunk"
            )
        if phase_name == "after" and not binds:
            raise EvidenceError(f"benchmark {query_name} after measurement {index} lacks selected DecompressChunk")
    cold = _validate_measurement(phase["cold"], f"benchmark {query_name} {phase_name}.cold")
    warmups = [
        _validate_measurement(item, f"benchmark {query_name} {phase_name}.warmups[{index}]")
        for index, item in enumerate(_require_list(phase["warmups"], "benchmark warmups"))
    ]
    if not 2 <= len(warmups) <= 5:
        raise EvidenceError(f"benchmark {query_name} {phase_name} needs 2-5 warmups")
    measurements = [
        _validate_measurement(item, f"benchmark {query_name} {phase_name}.measurements[{index}]")
        for index, item in enumerate(raw_measurements)
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
    activity_times: list[datetime] = []
    for index, value_ in enumerate(activities):
        activity = _require_mapping(value_, f"benchmark activity[{index}]")
        _require_exact_keys(
            activity,
            {"captured_at", "stage", "sessions", "material_load_stable"},
            f"benchmark activity[{index}]",
        )
        activity_times.append(_parse_utc(activity["captured_at"], f"benchmark activity[{index}].captured_at"))
        if activity["stage"] != expected_stages[index] or activity["material_load_stable"] is not True:
            raise EvidenceError(f"benchmark {query_name} {phase_name} has load drift")
        sessions = _require_list(activity["sessions"], f"benchmark activity[{index}].sessions")
        for session_index, session_value in enumerate(sessions):
            session = _require_mapping(session_value, f"benchmark activity[{index}].sessions[{session_index}]")
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
    if any(item["sessions"] != normalized_activities[0]["sessions"] for item in normalized_activities[1:]):
        raise EvidenceError(f"benchmark {query_name} {phase_name} has session-identity drift")
    bounds = _require_mapping(phase["execution_bounds"], f"benchmark {query_name} {phase_name}.execution_bounds")
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
        or not started_at < finished_at
        or (finished_at - started_at).total_seconds() > EXPECTED_TIMEOUT_SECONDS
    ):
        raise EvidenceError(f"benchmark {query_name} {phase_name} execution bounds differ")
    if activity_times != sorted(activity_times) or any(
        captured < started_at or captured > finished_at for captured in activity_times
    ):
        raise EvidenceError(f"benchmark {query_name} {phase_name} activity chronology differs")
    if query_name == "curve" and phase["rows"] < 1:
        raise EvidenceError("benchmark curve must return at least one row")
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
    _require_exact_keys(benchmark, {"execution_bounds", "queries"}, "benchmarks.evidence document")
    capture_bounds = _require_mapping(benchmark["execution_bounds"], "benchmarks.execution_bounds")
    _require_exact_keys(capture_bounds, {"before", "after"}, "benchmarks.execution_bounds")
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
            parameter_names = _require_list(binding["parameter_names"], "benchmark curve parameter_names")
            bound_parameters = _require_list(binding["bound_parameters"], "benchmark curve bound_parameters")
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
                request_start < request_end
                and request_start < selected_end
                and request_end > selected_start
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
    for phase_name in ("before", "after"):
        bounds = _require_mapping(capture_bounds[phase_name], f"benchmarks.execution_bounds.{phase_name}")
        _require_exact_keys(
            bounds,
            {"started_at", "finished_at", "wall_seconds"},
            f"benchmarks.execution_bounds.{phase_name}",
        )
        capture_started = _parse_utc(bounds["started_at"], f"benchmark {phase_name} capture-wide start")
        capture_finished = _parse_utc(bounds["finished_at"], f"benchmark {phase_name} capture-wide finish")
        phase_intervals = [_phase_interval(query, phase_name) for query in output]
        if (
            bounds["wall_seconds"] != EXPECTED_TIMEOUT_SECONDS
            or not capture_started < phase_intervals[0][0]
            or not phase_intervals[-1][1] < capture_finished
            or not capture_started < capture_finished
            or (capture_finished - capture_started).total_seconds() > EXPECTED_TIMEOUT_SECONDS
        ):
            raise EvidenceError(f"benchmark {phase_name} capture-wide deadline differs")
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
            "installed_unit_paths",
            "resolved_exec_start",
            "final_units",
            "compression_service_activations",
        },
        "cleanup.evidence document",
    )
    started = _parse_utc(cleanup["window_started_at"], "cleanup.window_started_at")
    finished = _parse_utc(cleanup["window_finished_at"], "cleanup.window_finished_at")
    captured = _parse_utc(cleanup["captured_at"], "cleanup.captured_at")
    if not window_started_at < started < finished < captured:
        raise EvidenceError("cleanup activation-window chronology differs")
    expected_repo = {
        "service": "infra/systemd/nhms-node27-timeseries-compression.service",
        "timer": "infra/systemd/nhms-node27-timeseries-compression.timer",
    }
    repo_units = _require_mapping(cleanup["repo_units"], "cleanup.repo_units")
    installed_units = _require_mapping(cleanup["installed_units"], "cleanup.installed_units")
    installed_unit_paths = _require_mapping(cleanup["installed_unit_paths"], "cleanup.installed_unit_paths")
    if set(repo_units) != set(expected_repo) or set(installed_units) != set(expected_repo):
        raise EvidenceError("cleanup unit evidence set differs")
    if installed_unit_paths != {
        "service": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.service",
        "timer": "/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.timer",
    }:
        raise EvidenceError("cleanup installed unit paths are not canonical user units")
    repo_refs: dict[str, Any] = {}
    installed_refs: dict[str, Any] = {}
    for key, relative_path in expected_repo.items():
        repo_refs[key] = _validate_reviewed_file_ref(
            repo_units[key],
            label=f"cleanup.repo_units.{key}",
            mutation_head_sha=mutation_head_sha,
            relative_path=relative_path,
        )
        installed_refs[key] = _artifact_ref(installed_units[key], f"cleanup.installed_units.{key}", max_bytes=1024**2)
        if (
            repo_refs[key]["sha256"] != installed_refs[key]["sha256"]
            or repo_refs[key]["bytes"] != installed_refs[key]["bytes"]
        ):
            raise EvidenceError(f"cleanup installed {key} differs from reviewed repo bytes")
    exec_start = _require_list(cleanup["resolved_exec_start"], "cleanup.resolved_exec_start")
    expected_exec_start = [
        "/home/nwm/NWM/.venv/bin/python",
        "/home/nwm/NWM/scripts/node27_timeseries_compression_supervisor.py",
        "--enforce",
    ]
    if exec_start != expected_exec_start:
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
        journal, _ = _text_artifact(state["journal"], f"cleanup.final_units.{unit_name}.journal", max_bytes=4 * 1024**2)
        normalized_units[unit_name] = {**dict(state), "journal": journal}
    for kind in ("timer", "service"):
        autopipe = normalized_units[f"nhms-node27-autopipe.{kind}"]
        prior = _require_mapping(prior_autopipe_state[kind], f"prior autopipe {kind}")
        if any(autopipe[key] != prior[key] for key in prior):
            raise EvidenceError("cleanup did not restore the exact prior autopipe timer/service state")
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
        "window_started_at": started.isoformat().replace("+00:00", "Z"),
        "window_finished_at": finished.isoformat().replace("+00:00", "Z"),
        "repo_units": repo_refs,
        "installed_units": installed_refs,
        "installed_unit_paths": dict(installed_unit_paths),
        "resolved_exec_start": exec_start,
        "final_units": normalized_units,
        "compression_service_activation_count": 0,
    }


def _phase_interval(query: Mapping[str, Any], phase: str) -> tuple[datetime, datetime]:
    capture = _require_mapping(query[f"{phase}_capture"], f"benchmark {phase} capture")
    bounds = _require_mapping(capture["execution_bounds"], f"benchmark {phase} bounds")
    return (
        _parse_utc(bounds["started_at"], f"benchmark {phase} started_at"),
        _parse_utc(bounds["finished_at"], f"benchmark {phase} finished_at"),
    )


def _assert_global_chronology(events: list[tuple[str, datetime]]) -> None:
    for (left_label, left), (right_label, right) in zip(events, events[1:]):
        if left >= right:
            raise EvidenceError(f"global chronology is not strict: {left_label} is not before {right_label}")


def _artifact_refs_in(value: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            if set(current) == {"path", "sha256", "bytes"}:
                path = Path(str(current["path"]))
                if path.is_absolute():
                    refs.append(dict(current))
            else:
                stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    unique = {(str(ref["path"]), str(ref["sha256"]), int(ref["bytes"])): ref for ref in refs}
    return [unique[key] for key in sorted(unique)]


def _reverify_retained_identities() -> None:
    for index, retained in enumerate(_RETAINED_IDENTITIES):
        try:
            current = inspect_bounded_file_no_follow(
                retained.path,
                max_bytes=max(1, retained.size),
                label=f"retained artifact[{index}]",
            )
        except BoundedEvidenceError as error:
            raise EvidenceError(str(error)) from error
        if current != retained:
            raise EvidenceError("retained artifact identity changed during publication")


_output_identity = terminal_state.terminal_identity
_terminal_lock_path = terminal_state._terminal_lock_path
_terminal_intent_gate_path = terminal_state._terminal_intent_gate_path
_terminal_intent_state_path = terminal_state._terminal_intent_state_path
_terminal_intent_root_path = terminal_state._terminal_intent_root_path
_terminal_intent_path = terminal_state._terminal_intent_path
_terminal_intent_identity_path = terminal_state._terminal_intent_identity_path
_identity_document = terminal_state._identity_document
_validate_identity_document = terminal_state._validate_identity_document
_validate_intent_context = terminal_state._validate_intent_context
_intent_context_from_terminal = terminal_state._intent_context_from_terminal
_unavailable_intent_context = terminal_state.unavailable_intent_context
_locked_intent_gate = terminal_state._locked_intent_gate
_read_gate_state = terminal_state._read_gate_state
_create_pending_intent_locked = terminal_state._create_pending_intent_locked
_consume_pending_intent_locked = terminal_state._consume_pending_intent_locked
_open_terminal_lock = terminal_state._open_terminal_lock


def _assert_terminal_state_paths_disjoint(
    output_path: Path,
    *,
    bundle_path: Path,
    closure: ArtifactClosure,
) -> None:
    """Keep terminal, lock, and intent outside the complete immutable input graph."""

    derived = {
        "terminal failure intent directory": _terminal_intent_root_path(output_path),
        "terminal failure intent": _terminal_intent_path(output_path),
        "terminal failure identity": _terminal_intent_identity_path(output_path),
        "terminal intent gate": _terminal_intent_gate_path(output_path),
        "terminal intent gate state": _terminal_intent_state_path(output_path),
        "terminal publication lock": _terminal_lock_path(output_path),
    }
    fixed_inputs = [bundle_path, CANONICAL_RECEIPT_SCHEMA, CANONICAL_EVIDENCE_SCHEMA]
    assert_paths_disjoint(output_path, fixed_inputs, label="terminal evidence")
    assert_output_disjoint_from_closure(output_path, closure, label="terminal evidence")
    for label, path in derived.items():
        assert_paths_disjoint(path, [output_path, *fixed_inputs], label=label)
        assert_output_disjoint_from_closure(path, closure, label=label)
    derived_items = list(derived.items())
    for index, (left_label, left_path) in enumerate(derived_items):
        for right_label, right_path in derived_items[index + 1 :]:
            assert_paths_disjoint(left_path, [right_path], label=f"{left_label}/{right_label}")


def _intent_context_from_bundle(bundle: Mapping[str, Any], *, verifier_head_sha: str) -> dict[str, Any]:
    mutation_head_sha = str(bundle.get("mutation_head_sha", ""))
    if str(bundle.get("verifier_head_sha", "")) != verifier_head_sha:
        raise EvidenceError("bundle verifier identity differs from the executing verifier")
    execution = _require_mapping(bundle.get("execution"), "bundle execution for failure identity")
    run_plan_ref = _require_mapping(execution.get("run_plan"), "bundle run plan for failure identity")
    _require_exact_keys(run_plan_ref, {"path", "sha256", "bytes"}, "bundle run plan reference")
    run_plan_path = Path(str(run_plan_ref["path"]))
    if not run_plan_path.is_absolute():
        raise EvidenceError("bundle run plan path for failure identity must be absolute")
    try:
        raw, _, run_plan_raw = read_bounded_json_with_identity_no_follow(
            run_plan_path,
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
            label="bundle run plan for failure identity",
            max_depth=MAX_PLAN_DEPTH,
            max_nodes=MAX_JSON_NODES,
            max_array_items=MAX_JSON_ARRAY_ITEMS,
        )
    except BoundedEvidenceError as error:
        raise EvidenceError(str(error)) from error
    if len(raw) != run_plan_ref["bytes"] or _sha256(raw) != run_plan_ref["sha256"]:
        raise EvidenceError("bundle run plan failure identity reference differs")
    run_plan = _require_mapping(run_plan_raw, "bundle run plan failure identity")
    if str(run_plan.get("mutation_head_sha", "")) != mutation_head_sha:
        raise EvidenceError("bundle/run-plan mutation identity differs")
    ledger_ref = _require_mapping(execution.get("ledger"), "bundle ledger for failure identity")
    _require_exact_keys(ledger_ref, {"path", "sha256", "bytes"}, "bundle ledger reference")
    ledger_path = Path(str(ledger_ref["path"]))
    if not ledger_path.is_absolute():
        raise EvidenceError("bundle ledger path for failure identity must be absolute")
    try:
        ledger_raw, _ = read_bounded_bytes_with_identity_no_follow(
            ledger_path,
            max_bytes=MAX_BINARY_ARTIFACT_BYTES,
            label="bundle ledger for failure identity",
        )
    except BoundedEvidenceError as error:
        raise EvidenceError(str(error)) from error
    if len(ledger_raw) != ledger_ref["bytes"] or _sha256(ledger_raw) != ledger_ref["sha256"]:
        raise EvidenceError("bundle ledger failure identity reference differs")
    run_ids: set[str] = set()
    for raw_line in ledger_raw.splitlines():
        try:
            event = _require_mapping(json.loads(raw_line), "bundle ledger event for failure identity")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceError("bundle ledger failure identity is not JSONL") from error
        validate_json_complexity(
            event,
            label="bundle ledger event for failure identity",
            max_depth=MAX_PLAN_DEPTH,
            max_nodes=MAX_JSON_NODES,
            max_array_items=MAX_JSON_ARRAY_ITEMS,
        )
        run_ids.add(str(event.get("run_id", "")))
    if len(run_ids) != 1:
        raise EvidenceError("bundle ledger run identity differs")
    return terminal_state.bound_intent_context(
        run_id=next(iter(run_ids)),
        verifier_head_sha=verifier_head_sha,
        mutation_head_sha=mutation_head_sha,
    )


def read_authoritative_terminal(
    path: Path, *, max_bytes: int = MAX_JSON_ARTIFACT_BYTES
) -> Mapping[str, Any]:
    return terminal_state.read_authoritative_terminal(
        path,
        max_bytes=max_bytes,
        deadline_monotonic=time.monotonic() + PUBLISH_LOCK_TIMEOUT_SECONDS,
    )


def _publish_terminal_cas(
    path: Path,
    payload: bytes,
    expected: FileIdentity | None,
    *,
    intent_context: Mapping[str, Any] | None = None,
) -> FileIdentity:
    return terminal_state.publish_terminal_cas(
        path,
        payload,
        expected,
        intent_context=intent_context,
        deadline_monotonic=time.monotonic() + PUBLISH_LOCK_TIMEOUT_SECONDS,
    )


def _publish_terminal_failure(
    path: Path,
    *,
    stage: str,
    expected: FileIdentity | None,
    intent_context: Mapping[str, Any],
) -> bool:
    context = _validate_intent_context(intent_context)
    return terminal_state.publish_unavailable_failure(
        path,
        stage=stage,
        expected=expected,
        verifier_head_sha=context["verifier_head_sha"],
        deadline_monotonic=time.monotonic() + PUBLISH_LOCK_TIMEOUT_SECONDS,
    )

def verify_bundle(
    bundle: Mapping[str, Any],
    *,
    receipt_schema: Mapping[str, Any],
    verifier_head_sha: str,
    artifact_manifest: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Recompute all task-4.5 derivable gates and return the terminal envelope."""
    _RETAINED_IDENTITIES.clear()
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
        "migration_invocations": 2,
        "dry_run_invocations": 1,
        "sole_db_user_during_window": True,
        "database_audit_proof": False,
        "acceptance_claim": PASS_CLAIM,
        "repo_path": EXPECTED_REPO_PATH,
        "remote_identity": EXPECTED_REMOTE_IDENTITY,
        "reviewed_mutation_sha": bundle["mutation_head_sha"],
        "reviewed_remote_ref": "refs/remotes/origin/feat/issue-1069-live-compression",
    }
    if authorization != expected_authorization:
        raise EvidenceError("authorization differs from the issue #1069 bound-1 envelope or mutation-head")
    _validate_repository_provenance(
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        reviewed_remote_ref=str(authorization["reviewed_remote_ref"]),
    )
    database_identity_for_execution = _require_mapping(bundle["database_identity"], "database_identity")
    execution_summary = _validate_supervisor_execution(
        _require_mapping(bundle["execution"], "execution"),
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        database=str(database_identity_for_execution.get("dbname", "")),
    )
    observed_requirements: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}

    def ledger_invocation(kind: str, ordinal: int = 0) -> dict[str, Any]:
        event = execution_summary["events_by_kind"][kind][ordinal]
        return {
            "started_at_dt": _parse_utc(event["started_at"], f"{kind} ledger start"),
            "finished_at_dt": _parse_utc(event["finished_at"], f"{kind} ledger finish"),
            "artifact_associations": _require_mapping(event["artifact_associations"], f"{kind} artifacts"),
            "kind": kind,
            "ordinal": ordinal,
        }

    def capture_invocation(kind: str) -> dict[str, Any]:
        event = execution_summary["capture_events_by_kind"][kind]
        return {
            "started_at_dt": _parse_utc(event["started_at"], f"capture {kind} ledger start"),
            "finished_at_dt": _parse_utc(event["finished_at"], f"capture {kind} ledger finish"),
            "artifact_associations": {kind: event["artifact_association"]},
            "kind": f"capture:{kind}",
            "ordinal": 0,
        }

    def require_observed_artifact(invocation: Mapping[str, Any], name: str, ref: Mapping[str, Any]) -> None:
        if name in observed_requirements:
            raise EvidenceError(f"{name} has duplicate semantic associations")
        if EXPECTED_OUTPUT_OWNERS.get(name) != (invocation["kind"], invocation["ordinal"]):
            raise EvidenceError(f"{name} is associated with the wrong producer")
        observed_requirements[name] = (invocation, ref)

    preflight_bundle = _require_mapping(bundle["preflight"], "preflight")
    _require_exact_keys(
        preflight_bundle,
        {"evidence", "schema_dump", "schema_dump_list", "catalog_before"},
        "preflight",
    )
    preflight_ref, preflight = _json_artifact(preflight_bundle.get("evidence"), "preflight.evidence")
    pg_dump_invocation = ledger_invocation("pg_dump")
    require_observed_artifact(capture_invocation("preflight_evidence"), "preflight_evidence", preflight_ref)
    preflight_summary = _validate_preflight(preflight, str(bundle["mutation_head_sha"]))
    recovery_bundle = _require_mapping(bundle["recovery"], "recovery")
    _require_exact_keys(recovery_bundle, {"preflight", "receipt", "invocation"}, "recovery")
    recovery_preflight_ref, recovery_preflight_raw = _json_artifact(recovery_bundle["preflight"], "recovery.preflight")
    recovery_receipt_ref, recovery_receipt_raw = _json_artifact(recovery_bundle["receipt"], "recovery.receipt")
    if (
        recovery_preflight_ref["path"] == recovery_receipt_ref["path"]
        or recovery_preflight_ref["sha256"] == recovery_receipt_ref["sha256"]
    ):
        raise EvidenceError("recovery preflight and receipt must be distinct artifacts")
    recovery_summary = _validate_recovery(
        recovery_preflight_raw,
        recovery_receipt_raw,
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        database_identity=_require_mapping(preflight["database_identity"], "preflight.database_identity"),
        compression_preflight_captured_at=_parse_utc(preflight_summary["captured_at"], "preflight captured_at"),
    )
    dump_ref, _ = _streaming_artifact_ref(
        preflight_bundle.get("schema_dump"),
        "preflight.schema_dump",
        max_bytes=MAX_BINARY_ARTIFACT_BYTES,
    )
    _require_custom_dump_magic(Path(dump_ref["path"]))
    require_observed_artifact(pg_dump_invocation, "schema_dump", dump_ref)
    dump_list_ref, dump_list_raw = _json_artifact(
        preflight_bundle.get("schema_dump_list"), "preflight.schema_dump_list"
    )
    dump_listing = _validate_dump_listing(
        dump_list_raw,
        dump_ref=dump_ref,
        mutation_head_sha=str(bundle["mutation_head_sha"]),
    )
    require_observed_artifact(capture_invocation("schema_dump_list"), "schema_dump_list", dump_list_ref)
    version_event = execution_summary["events_by_kind"]["pg_restore_version"][0]
    list_event = execution_summary["events_by_kind"]["pg_restore_list"][0]
    expected_tool_association = {
        "dump_sha256": dump_ref["sha256"],
        "container_image_id": dump_listing["container_image_id"],
        "binary_realpath": dump_listing["binary_realpath"],
        "binary_sha256": dump_listing["binary_sha256"],
    }
    if (
        version_event["stdout"]["sha256"] != dump_listing["version_stdout_sha256"]
        or version_event["stdout"]["bytes"] != dump_listing["version_stdout_bytes"]
        or list_event["stdout"]["sha256"] != dump_listing["stdout_sha256"]
        or list_event["stdout"]["bytes"] != dump_listing["stdout_bytes"]
        or version_event["artifact_associations"] != expected_tool_association
        or {key: list_event["artifact_associations"].get(key) for key in expected_tool_association}
        != expected_tool_association
    ):
        raise EvidenceError("container pg_restore ledger/listing association differs")
    catalog_before_ref, catalog_before = _json_artifact(
        preflight_bundle.get("catalog_before"), "preflight.catalog_before"
    )
    catalog_before_snapshot = _catalog_snapshot(
        catalog_before,
        label="preflight.catalog_before",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        phase="pre-migration",
        validator=_validate_pre_migration_catalog,
    )
    require_observed_artifact(capture_invocation("catalog_before"), "catalog_before", catalog_before_ref)
    recovery_invocation_ref = execution_summary["ledger"]
    recovery_invocation = ledger_invocation("decompress")
    require_observed_artifact(capture_invocation("recovery_preflight"), "recovery_preflight", recovery_preflight_ref)
    require_observed_artifact(recovery_invocation, "recovery_receipt", recovery_receipt_ref)
    if recovery_invocation["started_at_dt"] != _parse_utc(
        recovery_receipt_raw["started_at"], "recovery receipt started"
    ) or recovery_invocation["finished_at_dt"] != _parse_utc(
        recovery_receipt_raw["finished_at"], "recovery receipt finished"
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
    first_ref, first_catalog = _json_artifact(migration.get("catalog_after_first"), "migration.catalog_after_first")
    second_ref, second_catalog = _json_artifact(migration.get("catalog_after_second"), "migration.catalog_after_second")
    first_invocation_ref = execution_summary["ledger"]
    second_invocation_ref = execution_summary["ledger"]
    first_invocation = ledger_invocation("migration_apply", 0)
    second_invocation = ledger_invocation("migration_apply", 1)
    require_observed_artifact(capture_invocation("catalog_after_first"), "catalog_after_first", first_ref)
    require_observed_artifact(capture_invocation("catalog_after_second"), "catalog_after_second", second_ref)
    if first_invocation["finished_at_dt"] > second_invocation["started_at_dt"]:
        raise EvidenceError("migration applies are not distinct ordered execution artifacts")
    first_catalog_snapshot = _catalog_snapshot(
        first_catalog,
        label="migration first catalog",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        phase="after-first-apply",
        validator=_validate_d3_catalog,
    )
    second_catalog_snapshot = _catalog_snapshot(
        second_catalog,
        label="migration second catalog",
        mutation_head_sha=str(bundle["mutation_head_sha"]),
        phase="after-second-apply",
        validator=_validate_d3_catalog,
    )
    if _canonical_json_bytes(first_catalog_snapshot["catalog"]) != _canonical_json_bytes(
        second_catalog_snapshot["catalog"]
    ):
        raise EvidenceError("first/second migration catalog snapshots differ")

    selection_bundle = _require_mapping(bundle["selection"], "selection")
    _require_exact_keys(selection_bundle, {"post_dry_run", "pre_enforce"}, "selection")
    post_dry_ref, post_dry_raw = _json_artifact(selection_bundle["post_dry_run"], "selection.post_dry_run")
    pre_enforce_ref, pre_enforce_raw = _json_artifact(selection_bundle["pre_enforce"], "selection.pre_enforce")
    if post_dry_ref["path"] == pre_enforce_ref["path"] or post_dry_ref["sha256"] == pre_enforce_ref["sha256"]:
        raise EvidenceError("selection snapshots must be distinct observations")
    post_dry = _validate_selection_snapshot(post_dry_raw, "selection.post_dry_run")
    pre_enforce = _validate_selection_snapshot(pre_enforce_raw, "selection.pre_enforce")
    if post_dry["identities"] != pre_enforce["identities"]:
        raise EvidenceError("post-dry-run and pre-enforce selected tuples differ")
    if _parse_utc(preflight_summary["captured_at"], "preflight captured_at") > post_dry["observed_at_dt"]:
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
    enforce_ref, enforce = _load_receipt(receipts_bundle.get("enforce"), "receipts.enforce", receipt_schema)
    dry_invocation_ref = execution_summary["ledger"]
    enforce_invocation_ref = execution_summary["ledger"]
    dry_invocation = ledger_invocation("compression_dry_run")
    enforce_invocation = ledger_invocation("compression_enforce")
    require_observed_artifact(dry_invocation, "dry_run_receipt", dry_ref)
    require_observed_artifact(capture_invocation("post_dry_selection"), "post_dry_selection", post_dry_ref)
    require_observed_artifact(capture_invocation("pre_enforce_selection"), "pre_enforce_selection", pre_enforce_ref)
    require_observed_artifact(enforce_invocation, "enforce_receipt", enforce_ref)
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
    pre_enforce_delta = (enforce_started_at - pre_enforce["observed_at_dt"]).total_seconds()
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
    enforce_identity = [_selected_identity(_require_mapping(row, "enforce selected")) for row in enforce["selected"]]
    if dry_identity != post_dry["identities"] or enforce_identity != identities:
        raise EvidenceError("selection/dry-run/enforce selected tuples differ")
    dry_candidate_identities = [
        _selected_identity(_require_mapping(row, "dry candidate")) for row in [*dry["selected"], *dry["deferred"]]
    ]
    enforce_candidate_identities = [
        _selected_identity(_require_mapping(row, "enforce candidate"))
        for row in [*enforce["selected"], *enforce["deferred"]]
    ]
    if [_selected_identity(row) for row in post_dry["candidates"]] != dry_candidate_identities or [
        _selected_identity(row) for row in pre_enforce["candidates"]
    ] != enforce_candidate_identities:
        raise EvidenceError("selection candidates do not cover the complete ordered receipt scope")
    expected_totals = {key: {"before_bytes": 0, "after_bytes": None, "chunks_compressed": 0} for key in HYPERTABLE_KEYS}
    enforce_row = _require_mapping(enforce["selected"][0], "enforce selected")
    if (
        post_dry["selected"][0]["before_bytes"] != dry["selected"][0]["before_bytes"]
        or selected_before != enforce_row["before_bytes"]
    ):
        raise EvidenceError("selection and enforce before_bytes differ")
    selected_key = f"{enforce_row['hypertable_schema']}.{enforce_row['hypertable_name']}"
    expected_dry_totals = {
        key: {"before_bytes": 0, "after_bytes": None, "chunks_compressed": 0} for key in HYPERTABLE_KEYS
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
    require_observed_artifact(capture_invocation("sizes_pre"), "sizes_pre", sizes_pre_ref)
    require_observed_artifact(capture_invocation("sizes_post"), "sizes_post", sizes_post_ref)
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
    if any(
        relation.get("origin_chunk_schema") == selected["chunk_schema"]
        and relation.get("origin_chunk_name") == selected["chunk_name"]
        for relation in sizes_pre[selected_key]["compressed_relations"]
        if isinstance(relation, Mapping)
    ):
        raise EvidenceError("selected compressed relation already existed in pre snapshot")
    for table_key in HYPERTABLE_KEYS:
        pre_total = sizes_pre[table_key]["compressed_chunks"] + sizes_pre[table_key]["uncompressed_chunks"]
        post_total = sizes_post[table_key]["compressed_chunks"] + sizes_post[table_key]["uncompressed_chunks"]
        if pre_total != post_total:
            raise EvidenceError("snapshot total chunk count drifted")

    def relation_map(table: Mapping[str, Any]) -> dict[tuple[str, str], tuple[str, str]]:
        return {
            (str(row["origin_chunk_schema"]), str(row["origin_chunk_name"])): (
                str(row["schema"]),
                str(row["name"]),
            )
            for row in table["compressed_relations"]
        }

    pre_maps = {key: relation_map(sizes_pre[key]) for key in HYPERTABLE_KEYS}
    post_maps = {key: relation_map(sizes_post[key]) for key in HYPERTABLE_KEYS}
    if any(
        relation.get("origin_chunk_schema") == selected["chunk_schema"]
        and relation.get("origin_chunk_name") == selected["chunk_name"]
        for relation in sizes_pre[selected_key]["compressed_relations"]
        if isinstance(relation, Mapping)
    ):
        raise EvidenceError("selected compressed relation already existed in pre snapshot")
    pre_combined = sum(sizes_pre[key]["hypertable_size"] for key in HYPERTABLE_KEYS)
    post_combined = sum(sizes_post[key]["hypertable_size"] for key in HYPERTABLE_KEYS)
    compressed_delta = sum(
        sizes_post[key]["compressed_chunks"] - sizes_pre[key]["compressed_chunks"] for key in HYPERTABLE_KEYS
    )
    if post_combined >= pre_combined or compressed_delta != 1:
        raise EvidenceError("size/compressed-count acceptance arithmetic failed")
    if any(
        sizes_post[key]["compressed_chunks"] - sizes_pre[key]["compressed_chunks"] != (1 if key == selected_key else 0)
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
    selected_origin = (str(selected["chunk_schema"]), str(selected["chunk_name"]))
    for table_key in HYPERTABLE_KEYS:
        expected_map = dict(pre_maps[table_key])
        if table_key == selected_key:
            expected_map[selected_origin] = (
                str(selected_relations[0]["schema"]),
                str(selected_relations[0]["name"]),
            )
        if post_maps[table_key] != expected_map:
            raise EvidenceError("snapshot origin/compressed-sibling map drifted")
    post_relation_bytes = selected_relations[0]["bytes"]
    receipt_after_bytes = enforce_row["after_bytes"]
    if (
        post_relation_bytes >= enforce_row["before_bytes"]
        or abs(post_relation_bytes - receipt_after_bytes) > MAX_POST_MEASUREMENT_DRIFT_BYTES
    ):
        raise EvidenceError(
            "post compressed sibling size is not reduced or exceeds the 1 MiB measurement-time drift bound"
        )
    selected_relation_names = {
        str(selected["chunk_name"]),
        str(selected_relations[0]["name"]),
    }

    catalog_bundle = _require_mapping(bundle["catalog"], "catalog")
    _require_exact_keys(catalog_bundle, {"post"}, "catalog")
    catalog_post_ref, catalog_post_raw = _json_artifact(catalog_bundle.get("post"), "catalog.post")
    require_observed_artifact(capture_invocation("catalog_post"), "catalog_post", catalog_post_ref)
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
    benchmark_ref, benchmark_raw = _json_artifact(benchmarks_bundle.get("evidence"), "benchmarks.evidence")
    benchmark_after_invocation = ledger_invocation("benchmark_after")
    require_observed_artifact(benchmark_after_invocation, "benchmarks", benchmark_ref)
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
        window_started_at=_parse_utc(recovery_summary["preflight_captured_at"], "recovery preflight captured_at"),
    )
    require_observed_artifact(capture_invocation("cleanup"), "cleanup", cleanup_ref)
    before_intervals = [_phase_interval(query, "before") for query in benchmark_results]
    after_intervals = [_phase_interval(query, "after") for query in benchmark_results]
    benchmark_before_invocation = ledger_invocation("benchmark_before")
    if any(left[1] >= right[0] for left, right in zip(before_intervals, before_intervals[1:])) or any(
        left[1] >= right[0] for left, right in zip(after_intervals, after_intervals[1:])
    ):
        raise EvidenceError("benchmark query phases overlap or are reversed")
    if not (
        benchmark_before_invocation["started_at_dt"]
        <= before_intervals[0][0]
        < before_intervals[-1][1]
        <= benchmark_before_invocation["finished_at_dt"]
        < benchmark_after_invocation["started_at_dt"]
        <= after_intervals[0][0]
        < after_intervals[-1][1]
        <= benchmark_after_invocation["finished_at_dt"]
    ):
        raise EvidenceError("benchmark artifact intervals escape their supervisor child events")
    chronology_events = [
        ("audit-window-start", execution_summary["artifact_window_started_at"]),
        ("dump-list", dump_listing["captured_at_dt"]),
        ("catalog-before", catalog_before_snapshot["captured_at_dt"]),
        ("migration-1-start", first_invocation["started_at_dt"]),
        ("migration-1-finish", first_invocation["finished_at_dt"]),
        ("catalog-1", first_catalog_snapshot["captured_at_dt"]),
        ("migration-2-start", second_invocation["started_at_dt"]),
        ("migration-2-finish", second_invocation["finished_at_dt"]),
        ("catalog-2", second_catalog_snapshot["captured_at_dt"]),
        (
            "recovery-preflight",
            _parse_utc(
                recovery_summary["preflight_captured_at"],
                "recovery preflight",
            ),
        ),
        ("recovery-start", recovery_invocation["started_at_dt"]),
        ("recovery-finish", recovery_invocation["finished_at_dt"]),
        (
            "compression-preflight",
            _parse_utc(preflight_summary["captured_at"], "compression preflight"),
        ),
        ("dry-start", dry_invocation["started_at_dt"]),
        ("dry-finish", dry_invocation["finished_at_dt"]),
        ("post-dry-selector", post_dry["observed_at_dt"]),
        ("benchmark-before-start", benchmark_before_invocation["started_at_dt"]),
        ("benchmark-before-finish", benchmark_before_invocation["finished_at_dt"]),
        ("pre-enforce-selector", pre_enforce["observed_at_dt"]),
        ("sizes-pre", sizes_pre_snapshot["captured_at_dt"]),
        ("enforce-start", enforce_invocation["started_at_dt"]),
        ("enforce-finish", enforce_invocation["finished_at_dt"]),
        ("sizes-post", sizes_post_snapshot["captured_at_dt"]),
        ("catalog-post", catalog_captured_at),
        ("benchmark-after-start", benchmark_after_invocation["started_at_dt"]),
        ("benchmark-after-finish", benchmark_after_invocation["finished_at_dt"]),
        (
            "cleanup-finish",
            _parse_utc(cleanup_summary["window_finished_at"], "cleanup finish"),
        ),
        (
            "cleanup-captured",
            _parse_utc(cleanup_summary["captured_at"], "cleanup captured"),
        ),
        ("audit-window-finish", execution_summary["artifact_window_finished_at"]),
    ]
    chronology_snapshot_ids = [
        dump_listing["snapshot_id"],
        catalog_before_snapshot["snapshot_id"],
        first_catalog_snapshot["snapshot_id"],
        second_catalog_snapshot["snapshot_id"],
        sizes_pre_snapshot["snapshot_id"],
        sizes_post_snapshot["snapshot_id"],
        catalog_post["snapshot_id"],
    ]
    if len(set(chronology_snapshot_ids)) != len(chronology_snapshot_ids):
        raise EvidenceError("global chronology snapshot identifiers are not unique")
    _assert_global_chronology(chronology_events)
    derived_cleanup = {
        "autopipe_timer_restored": True,
        "compression_timer_enabled": True,
        "compression_timer_active": False,
        "compression_service_active": False,
        "compression_service_activation_count": cleanup_summary["compression_service_activation_count"],
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

    if set(observed_requirements) | {"benchmark_before"} != set(EXPECTED_OUTPUT_OWNERS):
        raise EvidenceError("semantic output ownership coverage differs")
    for name, (invocation, ref) in observed_requirements.items():
        observed = _require_mapping(
            invocation["artifact_associations"].get(name),
            f"{invocation['kind']}.{name}",
        )
        if observed.get("artifact") != ref:
            raise EvidenceError(f"{name} artifact association is not the supervisor-observed child output")

    database_identity = _require_mapping(bundle["database_identity"], "database_identity")
    if database_identity != preflight["database_identity"]:
        raise EvidenceError("bundle/preflight database identity mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "qualifies_task_4_5": True,
        "issue": ISSUE,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "node": bundle["node"],
        "mutation_head_sha": bundle["mutation_head_sha"],
        "verifier_head_sha": verifier_head_sha,
        "database_identity": database_identity,
        "authorization": authorization,
        "execution": {
            "run_plan": execution_summary["run_plan"],
            "ledger": execution_summary["ledger"],
            "run_id": execution_summary["run_id"],
            "namespace_counts": execution_summary["namespace_counts"],
            "checkpoint_artifacts": execution_summary["checkpoint_artifacts"],
            "claim": PASS_CLAIM,
            "sole_db_user_attested": True,
            "database_audit_proof": False,
            "trust_limit": execution_summary["attestation"]["trust_limit"],
        },
        "recovery": {
            "preflight": recovery_preflight_ref,
            "receipt": recovery_receipt_ref,
            "invocation": recovery_invocation_ref,
            "authorized": True,
            **recovery_summary,
        },
        "preflight": {
            "evidence": preflight_ref,
            "schema_dump": dump_ref,
            "schema_dump_list": dump_list_ref,
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
            "first_invocation": first_invocation_ref,
            "first_exit_code": 0,
            "second_exit_code": 0,
            "catalog_after_first": first_ref,
            "second_invocation": second_invocation_ref,
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
            "dry_run_invocation": dry_invocation_ref,
            "enforce": enforce_ref,
            "enforce_invocation": enforce_invocation_ref,
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
        "chronology": {
            "events": [
                {
                    "name": name,
                    "at": at.isoformat().replace("+00:00", "Z"),
                }
                for name, at in chronology_events
            ],
            "snapshot_ids": chronology_snapshot_ids,
        },
        "source_manifest": [dict(ref) for ref in artifact_manifest]
        if artifact_manifest is not None
        else list(resolve_artifact_closure(bundle).manifest),
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
        default=Path(__file__).resolve().parents[1] / "schemas/timeseries_compression_receipt.schema.json",
    )
    parser.add_argument(
        "--evidence-schema-path",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "schemas/timeseries_compression_live_evidence.schema.json",
    )
    return parser


def _current_verifier_head() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        cleanliness = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--"],
            cwd=repo_root,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceError("verifier cleanliness query timed out") from error
    if cleanliness.returncode != 0:
        raise EvidenceError("executing verifier/schema differs from verifier_head_sha")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise EvidenceError("verifier HEAD query timed out") from error
    head = result.stdout.strip()
    if result.returncode != 0 or len(result.stdout) > 128 or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise EvidenceError("cannot bind verifier_head_sha to the executing repository")
    return head


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_safe = False
    expected_output_identity: FileIdentity | None = None
    failure_publication_pending = False
    intent_context: dict[str, Any] | None = None
    failure_stage = "provenance_unavailable"
    _RETAINED_IDENTITIES.clear()
    try:
        if not args.bundle_path.is_absolute() or not args.output_path.is_absolute():
            raise EvidenceError("bundle/output paths must be absolute")
        if args.receipt_schema_path != CANONICAL_RECEIPT_SCHEMA:
            raise EvidenceError("receipt schema path must be the canonical checkout path")
        if args.evidence_schema_path != CANONICAL_EVIDENCE_SCHEMA:
            raise EvidenceError("evidence schema path must be the canonical checkout path")
        _, bundle_identity, bundle_raw = read_bounded_json_with_identity_no_follow(
            args.bundle_path,
            max_bytes=MAX_JSON_ARTIFACT_BYTES,
            label="bundle",
            max_depth=MAX_PLAN_DEPTH,
            max_nodes=MAX_JSON_NODES,
            max_array_items=MAX_JSON_ARRAY_ITEMS,
        )
        bundle = _require_mapping(bundle_raw, "bundle")
        _reject_secrets(bundle, "bundle")
        try:
            closure = resolve_artifact_closure(bundle)
            _assert_terminal_state_paths_disjoint(
                args.output_path,
                bundle_path=args.bundle_path,
                closure=closure,
            )
        except BoundedEvidenceError as error:
            raise EvidenceError(str(error)) from error
        expected_output_identity = _output_identity(args.output_path)
        output_safe = True
        intent_context = _unavailable_intent_context(None)
        verifier_head_sha = _current_verifier_head()
        intent_context = _unavailable_intent_context(verifier_head_sha)
        intent_context = _intent_context_from_bundle(bundle, verifier_head_sha=verifier_head_sha)
        failure_stage = "verify_or_publish"
        _, receipt_schema_identity, receipt_schema_raw = read_bounded_json_with_identity_no_follow(
            CANONICAL_RECEIPT_SCHEMA,
            max_bytes=1024**2,
            label="receipt schema",
        )
        receipt_schema = _require_mapping(receipt_schema_raw, "receipt schema")
        _, evidence_schema_identity, evidence_schema_raw = read_bounded_json_with_identity_no_follow(
            CANONICAL_EVIDENCE_SCHEMA,
            max_bytes=1024**2,
            label="evidence schema",
        )
        evidence_schema = _require_mapping(evidence_schema_raw, "evidence schema")
        terminal = verify_bundle(
            bundle,
            receipt_schema=receipt_schema,
            verifier_head_sha=verifier_head_sha,
            artifact_manifest=closure.manifest,
        )
        _RETAINED_IDENTITIES.extend([bundle_identity, receipt_schema_identity, evidence_schema_identity])
        jsonschema.Draft7Validator(evidence_schema, format_checker=jsonschema.FormatChecker()).validate(terminal)
        expected_output_identity = _publish_terminal_cas(
            args.output_path,
            _canonical_json_bytes(terminal),
            expected_output_identity,
        )
        _reverify_retained_identities()
        reverify_artifact_closure(closure)
    except (
        BoundedEvidenceError,
        EvidenceError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        jsonschema.ValidationError,
    ) as error:
        if output_safe and intent_context is not None:
            failure_publication_pending = not _publish_terminal_failure(
                args.output_path,
                stage=failure_stage,
                expected=expected_output_identity,
                intent_context=intent_context,
            )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": str(error),
                    "failure_publication_pending": failure_publication_pending,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"status": "passed", "verdict": PASS_VERDICT}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
