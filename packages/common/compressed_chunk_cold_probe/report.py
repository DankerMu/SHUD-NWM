"""JSON report helpers and the isolated-cluster PASS predicate."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packages.common.compressed_chunk_cold_probe.types import WAL_LIMITATION, ProbeError
from packages.common.compressed_chunk_cold_residency import (
    ACCEPTED_SEQUENCE_NAME,
    PINNED_IMAGE_ID,
    PINNED_IMAGE_REF,
    REJECTED_SEQUENCE_NAMES,
    SOURCE_TABLESPACE_NAME,
    json_ready,
)


def _compressed_oid(payload: Mapping[str, Any] | None) -> object:
    if not isinstance(payload, Mapping):
        return None
    compressed = payload.get("compressed")
    if isinstance(compressed, Mapping):
        return compressed.get("oid")
    return None


def _error_blob(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return ""
    parts = [str(payload.get("error") or ""), str(payload.get("error_type") or "")]
    nested = payload.get("exec") or payload.get("sleep") or payload.get("block") or payload.get("move")
    if isinstance(nested, Mapping):
        parts.extend([str(nested.get("error") or ""), str(nested.get("error_type") or "")])
    return " ".join(part for part in parts if part.strip())


def _source_restored(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return payload.get("reconciliation") == "complete_source" and payload.get("original_sibling") is True


def _well_formed_parity(payload: Mapping[str, Any] | None, *, nonempty: bool = False) -> bool:
    if not isinstance(payload, Mapping):
        return False
    count = payload.get("count")
    checksum = payload.get("checksum")
    range_start = payload.get("range_start")
    range_end = payload.get("range_end")
    if not isinstance(count, int) or count < 0:
        return False
    if nonempty and count <= 0:
        return False
    if payload.get("value_sum") is None:
        return False
    if not isinstance(checksum, str) or checksum.strip() == "":
        return False
    if not isinstance(range_start, str) or not range_start.strip():
        return False
    if not isinstance(range_end, str) or not range_end.strip():
        return False
    return True


def _row_parities(*rows: Mapping[str, Any] | None) -> list[Mapping[str, Any] | None]:
    found: list[Mapping[str, Any] | None] = []
    for row in rows:
        if not isinstance(row, Mapping):
            found.append(None)
            continue
        found.append(row.get("before_parity") if isinstance(row.get("before_parity"), Mapping) else None)
        found.append(row.get("after_parity") if isinstance(row.get("after_parity"), Mapping) else None)
    return found


def _all_passed(report: Mapping[str, Any]) -> bool:
    if report.get("false_success"):
        return False
    gate = report.get("engine_gate") or {}
    if not all(
        gate.get(key) is True
        for key in (
            "image_pin_ok",
            "pg_matches_pin",
            "ts_matches_pin",
            "live_matches_pin",
            "requested_matches_pin",
            "used_matches_requested",
        )
    ):
        return False
    if (
        report.get("image_pin_ok") is False
        or report.get("pg_matches_pin") is False
        or report.get("ts_matches_pin") is False
    ):
        return False
    sequence = report.get("sequence") or {}
    if sequence.get("accepted") != ACCEPTED_SEQUENCE_NAME:
        return False
    rejected = set(sequence.get("rejected") or [])
    if not REJECTED_SEQUENCE_NAMES.issubset(rejected):
        return False
    candidates = report.get("candidates") or {}
    move = candidates.get("move_chunk") or {}
    if "access node" not in _error_blob(move).lower() and "access node" not in str(move).lower():
        return False
    compressed_alter = candidates.get("direct_compressed_heap_alter")
    if "not supported" not in _error_blob(compressed_alter).lower() and "not supported" not in str(
        compressed_alter
    ).lower():
        return False
    toast_alter = candidates.get("direct_toast_alter")
    if "system catalog" not in _error_blob(toast_alter).lower() and "system catalog" not in str(toast_alter).lower():
        return False
    if (candidates.get("decompress_first") or {}).get("complete") is not False:
        return False
    attach = candidates.get("internal_attach") or {}
    if attach.get("complete") is not False:
        return False
    if attach.get("new_group_residency") != "all_source":
        return False
    if attach.get("business_attached") not in ([], None):
        return False
    if (candidates.get("two_transaction") or {}).get("atomic") is not False:
        return False
    rollback = candidates.get("shell_first_rollback") or {}
    if rollback.get("reconciliation") != "complete_source":
        return False
    if rollback.get("original_sibling") is False:
        return False
    if _compressed_oid(rollback.get("before")) != _compressed_oid(rollback.get("after")):
        return False
    if rollback.get("before_parity") != rollback.get("after_parity"):
        return False
    if not _well_formed_parity(rollback.get("before_parity"), nonempty=True):
        return False
    phases = rollback.get("phases") or {}
    if (phases.get("after_recompress") or {}).get("residency") != "already_target":
        return False
    lifecycle = report.get("lifecycle") or {}
    committed = lifecycle.get("committed_move") or {}
    if committed.get("reconciliation") != "complete_target":
        return False
    if committed.get("before_parity") != committed.get("after_parity"):
        return False
    if not _well_formed_parity(committed.get("before_parity"), nonempty=True):
        return False
    if not lifecycle.get("parity_unchanged_until_replay"):
        return False
    if (lifecycle.get("cold") or {}).get("residency") != "already_target":
        return False
    already = lifecycle.get("already_cold") or {}
    if already.get("outcome") not in {"already_cold", "already_target"} and already.get("reason") != "already_cold":
        return False
    decompressed = lifecycle.get("decompressed") or {}
    if decompressed.get("residency") != "already_target" or decompressed.get("is_compressed") is not False:
        return False
    replay = lifecycle.get("replay_parity") or {}
    before_parity = lifecycle.get("before_parity") or {}
    if not _well_formed_parity(replay, nonempty=True) or not _well_formed_parity(before_parity, nonempty=True):
        return False
    if int(replay.get("count") or 0) != int(before_parity.get("count") or 0) + 1:
        return False
    if replay.get("checksum") == before_parity.get("checksum"):
        return False
    recompressed = lifecycle.get("recompressed") or {}
    if recompressed.get("residency") != "already_target" or recompressed.get("is_compressed") is not True:
        return False
    if _compressed_oid(recompressed) in {None, _compressed_oid(committed.get("before"))}:
        return False
    if _compressed_oid(committed.get("after")) in {None, _compressed_oid(committed.get("before"))}:
        return False
    if (lifecycle.get("move_back") or {}).get("reconciliation") != "complete_target":
        return False
    if lifecycle.get("move_back_residency") != "all_source":
        return False
    if lifecycle.get("drop_remaining"):
        return False
    if lifecycle.get("drop_oids_absent") is not True:
        return False
    if not isinstance(lifecycle.get("drop_before_oids"), list) or not lifecycle.get("drop_before_oids"):
        return False
    boundaries = report.get("boundaries") or {}
    if boundaries.get("exact_cutoff_eligibility") != "eligible":
        return False
    if boundaries.get("same_window_disjoint") is not True:
        return False
    if boundaries.get("attach_tablespace") not in ([], None):
        return False
    if not isinstance(boundaries.get("empty_chunk"), Mapping):
        return False
    if boundaries.get("no_index_origin_index_count") != 0:
        return False
    if boundaries.get("quoted_numeric_leading_index") is not True:
        return False
    if boundaries.get("owned_toast_present") is not True:
        return False
    if boundaries.get("new_chunk_tablespace") != SOURCE_TABLESPACE_NAME:
        return False
    fail_parity = report.get("failure_chunk_parity")
    if not _well_formed_parity(fail_parity, nonempty=True):
        return False
    failures = report.get("failures") or {}
    required_failure_keys = (
        "missing_target",
        "mid_shell",
        "mid_decompress",
        "mid_recompress",
        "statement_timeout",
        "lock_conflict",
        "pre_commit_interrupt",
        "lost_commit_ack",
        "permission",
        "full_target",
        "catalog_path_mismatch",
        "injected_missing_relation_error",
        "selection_disappearance",
    )
    for key in required_failure_keys:
        if key not in failures:
            return False
    restored_keys = (
        "missing_target",
        "mid_shell",
        "mid_decompress",
        "mid_recompress",
        "statement_timeout",
        "lock_conflict",
        "pre_commit_interrupt",
        "permission",
        "injected_missing_relation_error",
        "full_target",
    )
    for key in restored_keys:
        row = failures.get(key) or {}
        if not _source_restored(row):
            return False
        if not _well_formed_parity(row.get("after_parity") or row.get("before_parity"), nonempty=True):
            return False
    timeout = failures.get("statement_timeout") or {}
    if "QueryCanceled" not in _error_blob(timeout) and "QueryCanceled" not in str(timeout):
        return False
    lock_conflict = failures.get("lock_conflict") or {}
    if "LockNotAvailable" not in _error_blob(lock_conflict) and "LockNotAvailable" not in str(lock_conflict):
        return False
    for key in (
        "missing_target",
        "mid_shell",
        "mid_decompress",
        "mid_recompress",
        "pre_commit_interrupt",
        "permission",
        "injected_missing_relation_error",
    ):
        if not _error_blob(failures.get(key)):
            return False
    lost = failures.get("lost_commit_ack") or {}
    if lost.get("reconciliation") != "complete_target" or lost.get("replayed") is not False:
        return False
    if lost.get("committed") is False or lost.get("outcome") != "committed_ack_lost":
        return False
    if _compressed_oid(lost.get("after")) in {None, _compressed_oid(lost.get("before"))}:
        return False
    if not _well_formed_parity(lost.get("after_parity"), nonempty=True):
        return False
    if lost.get("before_parity") != lost.get("after_parity"):
        return False
    permission = failures.get("permission") or {}
    if permission.get("ok") is True:
        return False
    full = failures.get("full_target") or {}
    if full.get("genuine_enospc") is not True:
        return False
    if not _source_restored(full):
        return False
    blob = _error_blob(full)
    if (
        "DiskFull" not in blob
        and "No space left on device" not in blob
        and "no space left on device" not in blob.lower()
    ):
        return False
    mismatch = failures.get("catalog_path_mismatch") or {}
    if mismatch.get("refused") is not True:
        return False
    if mismatch.get("relation_oids_unchanged") is not True:
        return False
    if mismatch.get("residency_unchanged") is not True:
        return False
    if mismatch.get("parity_unchanged") is not True:
        return False
    injected = failures.get("injected_missing_relation_error") or {}
    if injected.get("reconciliation") != "complete_source":
        return False
    if injected.get("selected_relation_disappeared") is not False:
        return False
    disappearance = failures.get("selection_disappearance") or {}
    if disappearance.get("stale_blocked") is not True:
        return False
    if disappearance.get("sacrificed_group_gone") is not True:
        return False
    if disappearance.get("unrelated_unchanged") is not True:
        return False
    if disappearance.get("after_oids_absent") is not True:
        return False
    capacity = failures.get("capacity_preflight") or {}
    if not isinstance(capacity, Mapping):
        return False
    if not isinstance(capacity.get("before_compression_total_bytes"), int):
        return False
    if not isinstance(capacity.get("retained_source_bytes"), int):
        return False
    positive = capacity.get("positive") or {}
    equality = capacity.get("equality") or {}
    if positive.get("approved") is not True:
        return False
    if equality.get("approved") is not True:
        return False
    if equality.get("cold_headroom_bytes") != 0 or equality.get("hot_headroom_bytes") != 0:
        return False
    for key in ("cold_short", "hot_short"):
        row = capacity.get(key) or {}
        if row.get("approved") is not False:
            return False
        if row.get("shell_sql_executed") is not False:
            return False
        if row.get("oids_unchanged") is not True:
            return False
        if row.get("residency_unchanged") is not True:
            return False
        if row.get("original_sibling") is not True:
            return False
        if row.get("parity_unchanged") is not True:
            return False
    if failures.get("false_success"):
        return False
    sentinel = report.get("parity_sentinel") or {}
    if sentinel.get("target_mutation_changes_checksum") is not True:
        return False
    if sentinel.get("sibling_compensation_does_not_hide") is not True:
        return False
    wal = report.get("wal") or {}
    if WAL_LIMITATION not in str(wal.get("limitation") or ""):
        return False
    live = report.get("image_live_readonly") or {}
    alias = report.get("live_ref_alias")
    if alias not in {None, "digest_image_id"}:
        return False
    if alias == "digest_image_id":
        tags = live.get("repo_tags") if isinstance(live.get("repo_tags"), list) else []
        digests = live.get("repo_digests") if isinstance(live.get("repo_digests"), list) else []
        digest = str(report.get("live_repo_digest") or "")
        if PINNED_IMAGE_REF not in tags:
            return False
        if not digest.startswith("timescale/timescaledb-ha@") or digest not in digests:
            return False
        if live.get("image_id") != PINNED_IMAGE_ID:
            return False
        if live.get("config_image") != PINNED_IMAGE_ID:
            return False
    elif live.get("image_ref") not in {PINNED_IMAGE_REF, PINNED_IMAGE_ID}:
        return False
    cleanup = report.get("cleanup") or {}
    if cleanup.get("container_absent") is not True or cleanup.get("work_root_absent") is not True:
        return False
    if cleanup.get("identity_bound") is not True:
        return False
    return True


def write_report(path: Path | None, report: Mapping[str, Any]) -> None:
    payload = json.dumps(json_ready(report), indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(payload)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def parse_probe_report(payload: str | Mapping[str, Any]) -> dict[str, Any]:
    document = json.loads(payload) if isinstance(payload, str) else dict(payload)
    status = document.get("status")
    if status not in {"passed", "failed", "refused", "blocked", "refused_not_triggered"}:
        raise ProbeError(f"probe report status is not reconcilable: {status!r}")
    if status == "passed" and document.get("sequence", {}).get("accepted") != ACCEPTED_SEQUENCE_NAME:
        raise ProbeError("passed report is missing the accepted sequence")
    if status in {"failed", "blocked"} and document.get("false_success") is True:
        raise ProbeError("failed report claimed success")
    if status == "passed":
        cleanup = document.get("cleanup") or {}
        if not cleanup or cleanup.get("container_absent") is not True or cleanup.get("work_root_absent") is not True:
            raise ProbeError("passed report is missing cleanup proof")
        if not _all_passed(document):
            raise ProbeError("passed report is missing a required row")
    return document
