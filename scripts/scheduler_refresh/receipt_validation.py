"""Receipt structural validation, calibration and cutover-gate fields.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.scheduler.registry_audit import CUTOVER_GATE_MODES
from scripts.scheduler_refresh.classification import _validate_registry_classification_field
from scripts.scheduler_refresh.constants import (
    CALIBRATION_OVERRIDE_NOT_APPLIED_REASONS,
    CALIBRATION_OVERRIDE_REFUSAL_REASON,
    MAX_COLLECTION_ITEMS,
    MAX_ORPHAN_EVIDENCE,
    MAX_ORPHANS,
    MAX_RECEIPT_BYTES,
    MAX_RESIDUES,
    MAX_STRING_LENGTH,
    OUTCOMES,
    REASONS,
    RECEIPT_KEYS,
    RECEIPT_OPTIONAL_KEYS,
    REGISTRY_CUTOVER_REFUSAL_REASONS,
    SCHEMA_VERSION,
)
from workers.model_registry.basins_calibration_overrides import CalibrationOverrideError


def _validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise ValueError("receipt_shape_invalid")
    keys = set(receipt)
    if not RECEIPT_KEYS <= keys or (keys - RECEIPT_KEYS) - RECEIPT_OPTIONAL_KEYS:
        raise ValueError("receipt_shape_invalid")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("receipt_schema_invalid")
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or len(run_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("receipt_run_id_invalid")
    started = _parse_receipt_datetime(receipt.get("started_at"))
    finished = _parse_receipt_datetime(receipt.get("finished_at"))
    if finished < started:
        raise ValueError("receipt_time_invalid")
    if receipt.get("outcome") not in OUTCOMES or receipt.get("reason") not in REASONS:
        raise ValueError("receipt_enum_invalid")
    if receipt.get("operation_outcome") not in OUTCOMES or receipt.get("operation_reason") not in REASONS:
        raise ValueError("receipt_enum_invalid")
    if (
        receipt.get("operation_outcome") != receipt.get("outcome")
        or receipt.get("operation_reason") != receipt.get("reason")
        or len(str(receipt.get("reason"))) > 64
        or receipt.get("database_free") is not True
    ):
        raise ValueError("receipt_contract_invalid")
    phase = receipt.get("phase")
    if not isinstance(phase, str) or not phase or len(phase) > 64:
        raise ValueError("receipt_phase_invalid")
    providers = receipt.get("providers")
    residues = receipt.get("residues")
    orphans = receipt.get("orphans")
    if not isinstance(providers, list) or len(providers) > MAX_COLLECTION_ITEMS:
        raise ValueError("receipt_collection_limit")
    provider_keys = {
        "name",
        "before_sha256",
        "before_inode",
        "before_schema_version",
        "before_generated_at",
        "before_payload_checksum",
        "after_sha256",
        "after_schema_version",
        "after_generated_at",
        "after_payload_checksum",
        "entry_count",
    }
    names: list[str] = []
    for provider in providers:
        if not isinstance(provider, Mapping) or set(provider) != provider_keys:
            raise ValueError("receipt_provider_invalid")
        name = str(provider.get("name") or "")
        names.append(name)
        if name not in {"registry", "registry_worker_mirror", "readiness", "state"}:
            raise ValueError("receipt_provider_invalid")
        for field in ("before_sha256", "after_sha256"):
            value = provider.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("receipt_provider_invalid")
                digest = value
                if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                    raise ValueError("receipt_provider_invalid")
        before_inode = provider.get("before_inode")
        if before_inode is not None and (
            not isinstance(before_inode, int) or isinstance(before_inode, bool) or before_inode < 0
        ):
            raise ValueError("receipt_provider_invalid")
        for field in ("before_schema_version", "after_schema_version"):
            value = provider.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 128):
                raise ValueError("receipt_provider_invalid")
        for field in ("before_generated_at", "after_generated_at"):
            value = provider.get(field)
            if value is not None:
                _parse_receipt_datetime(value)
        for field in ("before_payload_checksum", "after_payload_checksum"):
            value = provider.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 128):
                raise ValueError("receipt_provider_invalid")
        if (
            not isinstance(provider.get("entry_count"), int)
            or isinstance(provider.get("entry_count"), bool)
            or int(provider["entry_count"]) < 0
        ):
            raise ValueError("receipt_provider_invalid")
    if len(names) != len(set(names)):
        raise ValueError("receipt_provider_invalid")
    if receipt.get("outcome") in {"dry_run", "published"} and names not in (
        ["registry", "readiness", "state"],
        ["registry", "registry_worker_mirror", "readiness", "state"],
    ):
        raise ValueError("receipt_provider_invalid")
    if names == ["registry", "registry_worker_mirror", "readiness", "state"]:
        registry_provider = providers[0]
        mirror_provider = providers[1]
        if receipt.get("outcome") in {"dry_run", "published"} and (
            registry_provider.get("after_sha256") != mirror_provider.get("after_sha256")
            or registry_provider.get("entry_count") != mirror_provider.get("entry_count")
        ):
            raise ValueError("receipt_provider_invalid")
    if not isinstance(residues, list) or len(residues) > MAX_RESIDUES:
        raise ValueError("receipt_residue_limit")
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > MAX_STRING_LENGTH
        or Path(item).is_absolute()
        or ".." in Path(item).parts
        for item in residues
    ):
        raise ValueError("receipt_residue_unsafe")
    if not isinstance(orphans, Mapping) or set(orphans) != {
        "items",
        "total",
        "discovered_total",
        "attempted_total",
        "created_total",
        "truncated",
    }:
        raise ValueError("receipt_orphan_invalid")
    orphan_items = orphans.get("items")
    orphan_total = orphans.get("total")
    discovered_total = orphans.get("discovered_total")
    attempted_total = orphans.get("attempted_total")
    created_total = orphans.get("created_total")
    if (
        not isinstance(orphan_items, list)
        or len(orphan_items) > MAX_ORPHAN_EVIDENCE
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"package:[0-9a-f]{32}", item) is None
            for item in orphan_items
        )
        or not isinstance(orphan_total, int)
        or isinstance(orphan_total, bool)
        or orphan_total > MAX_ORPHANS
        or orphan_total < 0
        or orphan_total < len(orphan_items)
        or not isinstance(discovered_total, int)
        or isinstance(discovered_total, bool)
        or discovered_total < 0
        or not isinstance(attempted_total, int)
        or isinstance(attempted_total, bool)
        or attempted_total < 0
        or not isinstance(created_total, int)
        or isinstance(created_total, bool)
        or created_total != orphan_total
        or discovered_total < attempted_total
        or attempted_total < created_total
        or attempted_total > MAX_ORPHANS
        or not isinstance(orphans.get("truncated"), bool)
        or orphans.get("truncated") is not (orphan_total > len(orphan_items))
    ):
        raise ValueError("receipt_orphan_limit")
    _validate_registry_classification_field(receipt)
    _validate_cutover_gate_field(receipt)
    _validate_calibration_overrides_field(receipt)
    _validate_value_bounds(receipt)
    return json.loads(json.dumps(receipt, ensure_ascii=True))

_CALIBRATION_OVERRIDE_BLOCK_KEYS = frozenset({"declaration_path", "not_applied", "error"})

_CALIBRATION_OVERRIDE_BLOCK_REQUIRED_KEYS = frozenset({"declaration_path", "not_applied"})

_CALIBRATION_OVERRIDE_ENTRY_KEYS = frozenset({"basin_slug", "parameter"})

_CALIBRATION_OVERRIDE_NOT_APPLIED_KEYS = frozenset({"basin_slug", "parameter", "reason_not_applied"})

_CALIBRATION_OVERRIDE_ERROR_KEYS = frozenset({"error_code", "message", "entries"})

def _bounded_receipt_text(value: Any) -> str:
    """Every string that enters the receipt is bounded at the source.

    `_validate_value_bounds` rejects a receipt carrying any string longer than
    `MAX_STRING_LENGTH`, and a rejected receipt is not published at all -- so an
    over-long declared `reason` would destroy exactly the diagnosability this
    block exists to add.  Truncate here instead.
    """
    text = "" if value is None else str(value)
    return text[:MAX_STRING_LENGTH]

def _calibration_entry_digest(entry: Mapping[str, Any]) -> dict[str, str]:
    """The two fields that identify a declared entry.  Value/reason/approver stay
    in the declaration file; the receipt only has to name WHICH entry."""
    return {
        "basin_slug": _bounded_receipt_text(entry.get("basin_slug")),
        "parameter": _bounded_receipt_text(entry.get("parameter")),
    }

def _calibration_entry_list(value: Any) -> list[dict[str, str]]:
    """Well-formed entries only.

    A half-named entry would be rejected by `_validate_calibration_overrides_field`,
    and a rejected receipt is not published at all -- dropping it keeps a
    malformed detail payload from costing the operator the whole receipt.
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    digests = [
        _calibration_entry_digest(item)
        for item in list(value)[:MAX_COLLECTION_ITEMS]
        if isinstance(item, Mapping)
    ]
    return [item for item in digests if item["basin_slug"] and item["parameter"]]

def _calibration_overrides_audit_from_summary(
    summary: Mapping[str, Any],
    *,
    declaration_path: Path | None,
) -> dict[str, Any] | None:
    """#1832 round-2 C2: persist the declared-but-not-applied entries.

    This lane never writes the publisher summary anywhere (it passes no
    `output_path`), so without this the fact that a declared override did not
    bite this tick had zero persisted trace.  Returns ``None`` for a summary
    that never ran the declaration at all (the previous-snapshot republish
    short-circuit), because claiming a declaration path there would be a
    receipt asserting something the run did not do.
    """
    if "calibration_overrides_not_applied" not in summary:
        return None
    return {
        "declaration_path": _bounded_receipt_text(
            summary.get("calibration_overrides_declaration") or declaration_path
        )
        or None,
        "not_applied": [
            {
                **_calibration_entry_digest(item),
                "reason_not_applied": _bounded_receipt_text(item.get("reason_not_applied")),
            }
            for item in list(summary.get("calibration_overrides_not_applied") or [])[
                :MAX_COLLECTION_ITEMS
            ]
            if isinstance(item, Mapping)
        ],
    }

def _calibration_overrides_audit_from_error(
    error: CalibrationOverrideError,
    *,
    declaration_path: Path | None,
) -> dict[str, Any]:
    """#1832 round-2 C1: carry the error code and the offending entry.

    The publisher raises with either a plural ``entries`` list (declaration- and
    staging-level refusals) or a single flat ``basin_slug``/``parameter`` pair
    (the apply-time refusals); both are normalised to one list here.  A
    declaration that fails to load names no basin at all, and then the message
    is the whole evidence.
    """
    details = error.details if isinstance(error.details, Mapping) else {}
    entries = _calibration_entry_list(details.get("entries"))
    if not entries:
        entries = _calibration_entry_list([details])
    return {
        "declaration_path": _bounded_receipt_text(declaration_path) or None,
        "not_applied": [],
        "error": {
            "error_code": _bounded_receipt_text(error.error_code),
            "message": _bounded_receipt_text(str(error)),
            "entries": entries,
        },
    }

def _validate_calibration_overrides_field(receipt: Mapping[str, Any]) -> None:
    """Admit exactly the two builders' shape (#1832).

    Optional at the key-set level, because a run that fails before the
    declaration is reached (lock contention, provider preimage) legitimately
    knows nothing about it.  But on the refusal reason the block -- and its
    ``error`` -- is the entire point, so absence there is itself forged, the
    same way `cutover_gate` is required on the cutover refusal reasons.
    """
    refusal = receipt.get("reason") == CALIBRATION_OVERRIDE_REFUSAL_REASON
    if "calibration_overrides" not in receipt:
        if refusal:
            raise ValueError("receipt_calibration_overrides_missing")
        return
    block = receipt["calibration_overrides"]
    if not isinstance(block, Mapping):
        raise ValueError("receipt_calibration_overrides_invalid")
    keys = set(block)
    if not _CALIBRATION_OVERRIDE_BLOCK_REQUIRED_KEYS <= keys or keys - _CALIBRATION_OVERRIDE_BLOCK_KEYS:
        raise ValueError("receipt_calibration_overrides_invalid")
    declaration_path = block["declaration_path"]
    if declaration_path is not None and (
        not isinstance(declaration_path, str)
        or not declaration_path
        or len(declaration_path) > MAX_STRING_LENGTH
    ):
        raise ValueError("receipt_calibration_overrides_invalid")
    not_applied = block["not_applied"]
    if not isinstance(not_applied, list) or len(not_applied) > MAX_COLLECTION_ITEMS:
        raise ValueError("receipt_calibration_overrides_invalid")
    for item in not_applied:
        if not isinstance(item, Mapping) or set(item) != _CALIBRATION_OVERRIDE_NOT_APPLIED_KEYS:
            raise ValueError("receipt_calibration_overrides_invalid")
        _require_calibration_entry_strings(item)
        if item["reason_not_applied"] not in CALIBRATION_OVERRIDE_NOT_APPLIED_REASONS:
            raise ValueError("receipt_calibration_overrides_invalid")
    error = block.get("error")
    if refusal and error is None:
        raise ValueError("receipt_calibration_overrides_missing")
    if error is None:
        return
    if not isinstance(error, Mapping) or set(error) != _CALIBRATION_OVERRIDE_ERROR_KEYS:
        raise ValueError("receipt_calibration_overrides_invalid")
    for field in ("error_code", "message"):
        value = error[field]
        if not isinstance(value, str) or not value or len(value) > MAX_STRING_LENGTH:
            raise ValueError("receipt_calibration_overrides_invalid")
    entries = error["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_COLLECTION_ITEMS:
        raise ValueError("receipt_calibration_overrides_invalid")
    for item in entries:
        if not isinstance(item, Mapping) or set(item) != _CALIBRATION_OVERRIDE_ENTRY_KEYS:
            raise ValueError("receipt_calibration_overrides_invalid")
        _require_calibration_entry_strings(item)

def _require_calibration_entry_strings(entry: Mapping[str, Any]) -> None:
    for field in ("basin_slug", "parameter"):
        value = entry.get(field)
        if not isinstance(value, str) or not value or len(value) > MAX_STRING_LENGTH:
            raise ValueError("receipt_calibration_overrides_invalid")

_CUTOVER_GATE_KEYS = frozenset({"mode", "declaration_env", "declaration_present"})

def _validate_cutover_gate_field(receipt: Mapping[str, Any]) -> None:
    """Admit exactly the normalizer's three-field shape (#1132).

    The key stays optional at the key-set level (``RECEIPT_OPTIONAL_KEYS``)
    because runs that fail before the audit block is constructed omit it, but
    a present key must hold a well-typed block: the schema and this validator
    have to reject the same corpus, so an explicit ``null`` (which the
    schema's ``"type": "object"`` refuses) is rejected here too rather than
    treated as absence.

    #1144: on the outcomes where the runner ALWAYS built the block before
    reaching the receipt (``published``/``dry_run``, or any registry-cutover
    refusal reason) absence is itself forged — the same corpus the schema's
    two ``allOf`` branches now require ``cutover_gate`` on.  The condition
    mirrors ``_validate_registry_classification_field``'s
    ``requires_classification``; the reason is distinct from
    ``receipt_cutover_gate_invalid`` so operators can tell "block missing"
    from "block malformed".
    """
    if "cutover_gate" not in receipt:
        requires_cutover_gate = (
            receipt.get("outcome") in {"dry_run", "published"}
            or receipt.get("reason") in REGISTRY_CUTOVER_REFUSAL_REASONS
        )
        if requires_cutover_gate:
            raise ValueError("receipt_cutover_gate_required")
        return
    cutover_gate = receipt.get("cutover_gate")
    if not isinstance(cutover_gate, Mapping) or set(cutover_gate) != _CUTOVER_GATE_KEYS:
        raise ValueError("receipt_cutover_gate_invalid")
    if cutover_gate.get("mode") not in CUTOVER_GATE_MODES:
        raise ValueError("receipt_cutover_gate_invalid")
    declaration_env = cutover_gate.get("declaration_env")
    if declaration_env is not None and (
        not isinstance(declaration_env, str) or len(declaration_env) > MAX_STRING_LENGTH
    ):
        raise ValueError("receipt_cutover_gate_invalid")
    if not isinstance(cutover_gate.get("declaration_present"), bool):
        raise ValueError("receipt_cutover_gate_invalid")

def _parse_receipt_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("receipt_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("receipt_time_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("receipt_time_invalid")
    return parsed.astimezone(UTC)

def _validate_value_bounds(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str) and len(item) > MAX_STRING_LENGTH:
            raise ValueError("receipt_string_limit")
        if isinstance(item, Mapping):
            if len(item) > MAX_COLLECTION_ITEMS:
                raise ValueError("receipt_collection_limit")
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            if len(item) > MAX_COLLECTION_ITEMS:
                raise ValueError("receipt_collection_limit")
            stack.extend(item)

def _receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    content = json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=True).encode() + b"\n"
    if len(content) > MAX_RECEIPT_BYTES:
        raise ValueError("receipt_size_limit")
    return content
