"""Schema-valid cold-residency receipt and intent-sidecar publication."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from packages.common.compressed_chunk_cold_runtime_catalog import (
    residency_group_from_snapshot,
    snapshot_group,
    window_parity_from_dict,
)
from packages.common.compressed_chunk_cold_runtime_timing import MoveObservation
from packages.common.evidence_io import (
    BoundedEvidenceError,
    assert_paths_disjoint,
    inspect_bounded_file_no_follow,
    read_bounded_bytes_no_follow,
    reject_secret_material,
)
from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    prove_named_entry_absent_durable,
    read_bytes_durable_no_follow,
    stat_no_follow,
    unlink_no_follow_durable,
)

SCHEMA_VERSION = "1.0"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/timeseries_cold_residency_receipt.schema.json"
MAX_RECEIPT_BYTES = 16 * 1024**2
_FORMAT_CHECKER = jsonschema.FormatChecker()


class ColdReceiptError(RuntimeError):
    def __init__(self, message: str, *, error_class: str = "publication", stage: str = "publication") -> None:
        super().__init__(message)
        self.error_class = error_class
        self.stage = stage


def load_receipt_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def iso_now(value: datetime | None = None) -> str:
    stamp = datetime.now(UTC) if value is None else value.astimezone(UTC)
    return stamp.isoformat().replace("+00:00", "Z")


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    redacted = redact_payload(dict(payload))
    reject_secret_material(redacted, label="cold residency receipt")
    return (json.dumps(redacted, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def validate_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    document = json.loads(canonical_bytes(payload).decode("utf-8"))
    jsonschema.validate(document, load_receipt_schema(), format_checker=_FORMAT_CHECKER)
    recovery = document.get("recovery") if isinstance(document.get("recovery"), Mapping) else None
    if document.get("outcome") == "in_progress" or (
        recovery and recovery.get("authority") in {"sidecar", "pending_cleanup"}
    ):
        _require_intent_evidence(document)
    return document


def sidecar_status(path: Path) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError as error:
        raise ColdReceiptError(
            "intent sidecar is corrupt or unreadable",
            error_class="corrupt_intent",
            stage="startup",
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise ColdReceiptError(
            "intent sidecar must not be a symlink",
            error_class="corrupt_intent",
            stage="startup",
        )
    if not stat.S_ISREG(info.st_mode):
        raise ColdReceiptError(
            "intent sidecar is corrupt or unreadable",
            error_class="corrupt_intent",
            stage="startup",
        )
    return "present"


def sidecar_present(path: Path) -> bool:
    return sidecar_status(path) == "present"


def _proof_error(*, message: str, stage: str, error: SafeFilesystemError) -> ColdReceiptError:
    return ColdReceiptError(
        message,
        error_class="publication_indeterminate" if error.kind == "indeterminate" else "publication",
        stage=stage,
    )


def prove_sidecar_absent(path: Path) -> None:
    """Durably prove intent-sidecar absence through the shared pinned-fd primitive."""

    try:
        prove_named_entry_absent_durable(path)
    except SafeFilesystemError as error:
        raise _proof_error(
            message="intent sidecar absence is unproven",
            stage="prove_sidecar_absent",
            error=error,
        ) from error


def authority_from_document(document: Mapping[str, Any] | None) -> str | None:
    if not isinstance(document, Mapping):
        return None
    recovery = document.get("recovery")
    if not isinstance(recovery, Mapping):
        return None
    authority = recovery.get("authority")
    return str(authority) if isinstance(authority, str) else None


def public_authority_blocks_selection(document: Mapping[str, Any] | None) -> bool:
    recovery = document.get("recovery") if isinstance(document, Mapping) else None
    return bool(isinstance(recovery, Mapping) and recovery.get("blocked_new_selection") is True)


def public_closed_authority(document: Mapping[str, Any] | None) -> bool:
    recovery = document.get("recovery") if isinstance(document, Mapping) else None
    return bool(
        isinstance(recovery, Mapping)
        and recovery.get("authority") == "closed"
        and recovery.get("sidecar_present") is False
        and recovery.get("cleanup_pending") is False
        and recovery.get("blocked_new_selection") is False
    )


def read_public_receipt(path: Path) -> dict[str, Any] | None:
    try:
        stat_no_follow(path)
    except FileNotFoundError:
        return None
    except SafeFilesystemError as error:
        raise ColdReceiptError(
            "public receipt is corrupt or unreadable",
            error_class="publication",
            stage="startup",
        ) from error
    return _decode_public_receipt(_read_public_receipt_bytes(path))


def read_public_receipt_durable(path: Path) -> dict[str, Any]:
    """Return the schema-valid public authority from the exact durably proven bytes."""

    try:
        raw = read_bytes_durable_no_follow(path, max_bytes=MAX_RECEIPT_BYTES)
    except SafeFilesystemError as error:
        raise _proof_error(
            message="public receipt durability is unproven",
            stage="read_public_receipt_durable",
            error=error,
        ) from error
    return _decode_public_receipt(raw)


def _read_public_receipt_bytes(path: Path) -> bytes:
    try:
        inspect_bounded_file_no_follow(path, max_bytes=MAX_RECEIPT_BYTES, label="cold residency receipt")
        return read_bounded_bytes_no_follow(path, max_bytes=MAX_RECEIPT_BYTES, label="cold residency receipt")
    except (BoundedEvidenceError, OSError) as error:
        raise ColdReceiptError(
            "public receipt is corrupt or unreadable",
            error_class="publication",
            stage="startup",
        ) from error


def _decode_public_receipt(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ColdReceiptError(
            "public receipt is corrupt or unreadable",
            error_class="publication",
            stage="startup",
        ) from error
    if not isinstance(payload, dict):
        raise ColdReceiptError(
            "public receipt is corrupt or unreadable",
            error_class="publication",
            stage="startup",
        )
    try:
        return validate_receipt(payload)
    except (jsonschema.ValidationError, BoundedEvidenceError, ColdReceiptError) as error:
        raise ColdReceiptError(
            "public receipt is corrupt or unreadable",
            error_class="publication",
            stage="startup",
        ) from error


def _require_intent_evidence(document: Mapping[str, Any]) -> None:
    recovery = document.get("recovery") if isinstance(document.get("recovery"), Mapping) else None
    authority_relevant = bool(
        recovery and recovery.get("authority") in {"sidecar", "pending_cleanup"}
    )
    inventory = document.get("inventory") if isinstance(document.get("inventory"), Mapping) else {}
    hypertables = inventory.get("hypertables") if isinstance(inventory, Mapping) else None
    for item in document.get("selected") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("plan_kind") != "migrate":
            continue
        if not authority_relevant and item.get("outcome") != "planned":
            continue
        before = item.get("before")
        parity = item.get("before_parity")
        capacity = item.get("capacity")
        if not isinstance(before, Mapping) or not isinstance(parity, Mapping) or not isinstance(capacity, Mapping):
            raise ColdReceiptError(
                "planned intent is missing reconstructible before/parity/capacity",
                error_class="corrupt_intent",
                stage="startup",
            )
        try:
            group = residency_group_from_snapshot(before)
            window = window_parity_from_dict(parity)
        except Exception as error:
            raise ColdReceiptError(
                "planned intent before evidence is corrupt",
                error_class="corrupt_intent",
                stage="startup",
            ) from error
        durable = item.get("durable") if isinstance(item.get("durable"), Mapping) else {}
        try:
            start = datetime.fromisoformat(str(durable.get("range_start")).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(durable.get("range_end")).replace("Z", "+00:00"))
            same_start = start == window.range_start
            same_end = end == window.range_end
        except ValueError:
            same_start = same_end = False
        if not (same_start and same_end):
            raise ColdReceiptError(
                "before parity window does not match durable identity",
                error_class="corrupt_intent",
                stage="startup",
            )
        key = f"{group.hypertable_schema}.{group.hypertable_name}"
        table = hypertables.get(key) if isinstance(hypertables, Mapping) else None
        digest = table.get("digest") if isinstance(table, Mapping) else None
        if digest and digest != window.inventory_digest:
            raise ColdReceiptError(
                "before parity inventory digest does not match bound inventory",
                error_class="corrupt_intent",
                stage="startup",
            )


def intent_path_for(receipt_path: Path) -> Path:
    return receipt_path.with_name(f".{receipt_path.name}.intent")


def assert_publication_paths_disjoint(
    *,
    receipt_path: Path,
    intent_path: Path,
    lock_path: Path,
    lifecycle_lock_path: Path,
) -> None:
    try:
        assert_paths_disjoint(
            receipt_path, [intent_path, lock_path, lifecycle_lock_path], label="cold residency receipt"
        )
        assert_paths_disjoint(
            intent_path, [receipt_path, lock_path, lifecycle_lock_path], label="cold residency intent"
        )
        assert_paths_disjoint(lock_path, [receipt_path, intent_path, lifecycle_lock_path], label="cold residency lock")
        assert_paths_disjoint(
            lifecycle_lock_path,
            [receipt_path, intent_path, lock_path],
            label="timeseries lifecycle lock",
        )
    except BoundedEvidenceError as error:
        raise ColdReceiptError(str(error), error_class="path_alias", stage="config") from error


def publish_receipt(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    document = validate_receipt(payload)
    try:
        atomic_write_bytes_no_follow(path, canonical_bytes(document), mode=0o600, require_durable_replace=True)
    except SafeFilesystemError as error:
        kind = getattr(error, "kind", "io")
        raise ColdReceiptError(
            "receipt publication failed",
            error_class="publication_indeterminate" if kind == "indeterminate" else "publication",
            stage="publish_receipt",
        ) from error
    return document


def publish_intent(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    document = validate_receipt(payload)
    try:
        atomic_write_bytes_no_follow(path, canonical_bytes(document), mode=0o600, require_durable_replace=True)
    except SafeFilesystemError as error:
        kind = getattr(error, "kind", "io")
        raise ColdReceiptError(
            "intent publication failed",
            error_class="publication_indeterminate" if kind == "indeterminate" else "publication",
            stage="publish_intent",
        ) from error
    return document


def remove_intent(path: Path) -> None:
    try:
        unlink_no_follow_durable(path, missing_ok=False)
    except FileNotFoundError as error:
        raise ColdReceiptError(
            "intent sidecar missing during terminal removal",
            error_class="publication",
            stage="remove_intent",
        ) from error
    except SafeFilesystemError as error:
        kind = getattr(error, "kind", "io")
        raise ColdReceiptError(
            "intent sidecar removal failed",
            error_class="publication_indeterminate" if kind == "indeterminate" else "publication",
            stage="remove_intent",
        ) from error


def read_intent(path: Path) -> dict[str, Any]:
    try:
        inspect_bounded_file_no_follow(path, max_bytes=MAX_RECEIPT_BYTES, label="cold residency intent")
        raw = read_bounded_bytes_no_follow(path, max_bytes=MAX_RECEIPT_BYTES, label="cold residency intent")
        payload = json.loads(raw.decode("utf-8"))
    except (BoundedEvidenceError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ColdReceiptError(
            "intent sidecar is corrupt or unreadable",
            error_class="corrupt_intent",
            stage="startup",
        ) from error
    if not isinstance(payload, dict):
        raise ColdReceiptError(
            "intent sidecar is corrupt or unreadable",
            error_class="corrupt_intent",
            stage="startup",
        )
    try:
        return validate_receipt(payload)
    except (jsonschema.ValidationError, BoundedEvidenceError, ColdReceiptError) as error:
        raise ColdReceiptError(
            "intent sidecar is corrupt or unreadable",
            error_class="corrupt_intent",
            stage="startup",
        ) from error


def observation_payload(observation: MoveObservation) -> dict[str, Any]:
    before = snapshot_group(observation.before)
    after = None if observation.after is None else snapshot_group(observation.after)
    payload: dict[str, Any] = {
        "outcome": observation.outcome,
        "reconciliation": observation.reconciliation,
        "plan_kind": observation.plan_kind,
        "shell_sql_executed": observation.shell_sql_executed,
        "commit_ack_lost": observation.commit_ack_lost,
        "replayed": observation.replayed,
        "before": before,
        "after": after,
        "intermediate": dict(observation.intermediate),
        "before_parity": None if observation.before_parity is None else observation.before_parity.as_dict(),
        "after_parity": None if observation.after_parity is None else observation.after_parity.as_dict(),
    }
    if observation.timing is not None:
        payload["timing"] = dict(observation.timing)
    if observation.capacity is not None:
        payload["capacity"] = observation.capacity
    if observation.error_class is not None:
        payload["error"] = {
            "class": observation.error_class,
            "stage": observation.stage or "runtime",
            "reason": redact_text(observation.reason or observation.error_class),
        }
    return payload



def named_groups_from_intent(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    selected = intent.get("selected")
    if not isinstance(selected, list):
        raise ColdReceiptError(
            "intent sidecar is corrupt or unreadable",
            error_class="corrupt_intent",
            stage="startup",
        )
    groups: list[dict[str, Any]] = []
    for item in selected:
        if not isinstance(item, Mapping):
            raise ColdReceiptError(
                "intent sidecar is corrupt or unreadable",
                error_class="corrupt_intent",
                stage="startup",
            )
        durable = item.get("durable") or (item.get("before") or {}).get("durable")
        if not isinstance(durable, Mapping):
            raise ColdReceiptError(
                "intent sidecar is corrupt or unreadable",
                error_class="corrupt_intent",
                stage="startup",
            )
        groups.append(dict(durable))
    return groups


def empty_table_totals() -> dict[str, dict[str, int]]:
    return {
        "hydro.river_timeseries": {"selected": 0, "migrated": 0, "already_cold": 0, "deferred": 0},
        "met.forcing_station_timeseries": {"selected": 0, "migrated": 0, "already_cold": 0, "deferred": 0},
    }


def accumulate_totals(
    totals: dict[str, dict[str, int]],
    *,
    schema: str,
    name: str,
    field: str,
) -> None:
    key = f"{schema}.{name}"
    if key not in totals:
        totals[key] = {"selected": 0, "migrated": 0, "already_cold": 0, "deferred": 0}
    totals[key][field] = totals[key].get(field, 0) + 1


def stable_error(*, error_class: str, stage: str, reason: str) -> dict[str, str]:
    return {"class": error_class, "stage": stage, "reason": redact_text(reason)}


def recovery_payload(
    *,
    classification: str,
    sidecar_present: bool,
    blocked_new_selection: bool,
    authority: str,
    cleanup_pending: bool,
) -> dict[str, Any]:
    return {
        "classification": classification,
        "sidecar_present": sidecar_present,
        "replayed": False,
        "blocked_new_selection": blocked_new_selection,
        "authority": authority,
        "cleanup_pending": cleanup_pending,
    }


def build_receipt(
    *,
    mode: str,
    outcome: str,
    state: str,
    head_sha: str | None,
    generated_at: datetime,
    watermark: str | None,
    lag_seconds: int | None,
    cutoff: str | None,
    per_tick_bound: int | None,
    max_members: int | None,
    budget: Mapping[str, int] | None,
    cluster: Mapping[str, Any],
    target: Mapping[str, Any],
    inventory: Mapping[str, Any],
    capacity: Mapping[str, Any] | None,
    selected: Sequence[Mapping[str, Any]],
    deferred: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, Any]],
    error: Mapping[str, str] | None = None,
    recovery: Mapping[str, Any] | None = None,
    head_observed: bool | None = None,
    worktree_dirty: bool | None = None,
    config_observed: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(generated_at),
        "mode": mode,
        "outcome": outcome,
        "state": state,
        "head_sha": head_sha,
        "lag_seconds": lag_seconds,
        "per_tick_bound": per_tick_bound,
        "max_members": max_members,
        "budget": None if budget is None else dict(budget),
        "config_observed": config_observed,
        "cluster": _with_observed(cluster, default=True),
        "target": _with_observed(target, default=True),
        "inventory": _with_observed(inventory, default=True),
        "selected": list(selected),
        "deferred": list(deferred),
        "skipped": list(skipped),
        "per_table_totals": empty_table_totals(),
    }
    if head_observed is not None:
        payload["head_observed"] = head_observed
    if worktree_dirty is not None:
        payload["worktree_dirty"] = worktree_dirty
    if watermark is not None:
        payload["watermark"] = watermark
    if cutoff is not None:
        payload["cutoff"] = cutoff
    if capacity is not None:
        payload["capacity"] = dict(capacity)
    if error is not None:
        payload["error"] = dict(error)
    if recovery is not None:
        payload["recovery"] = dict(recovery)
    for item in selected:
        durable = item.get("durable") or (item.get("before") or {}).get("durable") or {}
        schema = str(durable.get("hypertable_schema") or "")
        name = str(durable.get("hypertable_name") or "")
        if schema and name:
            accumulate_totals(payload["per_table_totals"], schema=schema, name=name, field="selected")
            result = str(item.get("outcome") or "")
            if result == "migrated":
                accumulate_totals(payload["per_table_totals"], schema=schema, name=name, field="migrated")
            if result == "already_cold":
                accumulate_totals(payload["per_table_totals"], schema=schema, name=name, field="already_cold")
    for item in deferred:
        durable = item.get("durable") or {}
        schema = str(durable.get("hypertable_schema") or "")
        name = str(durable.get("hypertable_name") or "")
        if schema and name:
            accumulate_totals(payload["per_table_totals"], schema=schema, name=name, field="deferred")
    return payload


def _with_observed(payload: Mapping[str, Any], *, default: bool) -> dict[str, Any]:
    document = dict(payload)
    document.setdefault("observed", default)
    return document


def unavailable_cluster(*, application_name: str) -> dict[str, Any]:
    return {
        "server_version": None,
        "timescaledb_version": None,
        "application_name": application_name,
        "observed": False,
    }


def unavailable_target() -> dict[str, Any]:
    return {
        "catalog_name": "nhms_cold",
        "catalog_location": None,
        "container_bind": None,
        "host_path": None,
        "device_identity": None,
        "observed": False,
    }


def unavailable_inventory() -> dict[str, Any]:
    return {"digest": None, "hypertables": None, "observed": False}
