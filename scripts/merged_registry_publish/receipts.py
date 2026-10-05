"""The publish receipt, and the dry-run receipt an apply is held to.

Part of ``scripts/node22_publish_merged_scheduler_registry.py`` (the entry point).
"""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common import succession_receipt as succession
from scripts.merged_registry_publish.model import (
    _NOTHING_WRITTEN,
    DRY_RUN_RECEIPT_NAME,
    RECEIPT_SCHEMA_VERSION,
    RECEIPT_STEP,
    MergedRegistryPublishError,
    _Destination,
    _load_json_object,
    _manifest_byte_cap,
    _manifest_node_cap,
    _Plan,
    _Settings,
    _sha256,
)

# --- receipts ----------------------------------------------------------------


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _receipt(
    settings: _Settings,
    plan: _Plan,
    *,
    dry_run: bool,
    outcome: str,
    sha256_after: Mapping[str, str | None] | None = None,
    backups: Mapping[str, Path] | None = None,
    manifest_generated_at: str | None = None,
    manifest_bytes: int | None = None,
    dry_run_receipt: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    def destination(item: _Destination) -> dict[str, Any]:
        # ``backups`` holds only the backups that were completely written.
        return {
            "path": str(item.path),
            "sha256_before": item.sha256,
            "sha256_after": (sha256_after or {}).get(item.name),
            "backup_path": str(backups[item.name]) if backups and item.name in backups else None,
        }

    size = plan.manifest_bytes if manifest_bytes is None else manifest_bytes
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "succession_id": settings.succession_id,
        "provision_succession_id": settings.provision_succession_id,
        "step": RECEIPT_STEP,
        "dry_run": dry_run,
        "outcome": outcome,
        "generated_at": _utc_text(settings.clock()),
        "operator_id": settings.operator_id,
        "host": socket.gethostname(),
        "git_commit": succession.git_commit(),
        "operations": settings.operations.record(),
        "row_count_before": plan.row_count_before,
        "row_count_after": len(plan.merged_rows),
        "introduced_model_ids": plan.introduced_model_ids,
        "removed_model_ids": plan.removed_model_ids,
        "already_published_model_ids": plan.already_published_model_ids,
        "replaced": plan.replaced,
        "merged_model_ids": plan.merged_model_ids,
        "canonical": destination(plan.canonical),
        "mirror": destination(plan.mirror),
        "canonical_models_sha256_before": plan.canonical_models_sha256,
        "manifest_generated_at": manifest_generated_at,
        "manifest_bytes": size,
        "manifest_bytes_limit": _manifest_byte_cap(),
        "manifest_bytes_remaining": _manifest_byte_cap() - size,
        "manifest_json_nodes": plan.manifest_json_nodes,
        "manifest_json_nodes_limit": _manifest_node_cap(),
        "manifest_json_nodes_remaining": _manifest_node_cap() - plan.manifest_json_nodes,
    }
    if plan.provision_apply_receipt is not None:
        receipt["provision_apply_receipt"] = plan.provision_apply_receipt
    if dry_run_receipt is not None:
        receipt["dry_run_receipt"] = dict(dry_run_receipt)
    if reason is not None:
        receipt["reason"] = reason
    return receipt


def _load_dry_run_receipt(settings: _Settings) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(dry-run receipt, its path record)`` or refuse naming the expected path."""

    succession_id = str(settings.succession_id)
    path = settings.receipt_root / succession_id / DRY_RUN_RECEIPT_NAME
    try:
        content = path.read_bytes()
    except OSError as error:
        raise MergedRegistryPublishError(
            f"--apply requires the dry-run receipt of succession {succession_id!r} at {path} ({error}). "
            f"Run the same command without --apply and with the same --succession-id first. {_NOTHING_WRITTEN}"
        ) from error
    payload = _load_json_object(content, what=f"dry-run receipt {path}")
    expected = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "step": RECEIPT_STEP,
        "dry_run": True,
        "outcome": "planned",
        "succession_id": succession_id,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise MergedRegistryPublishError(
            f"{path} is not a publish dry-run receipt of succession {succession_id!r}. {_NOTHING_WRITTEN}"
        )
    return payload, {"path": str(path), "sha256": _sha256(content)}


def _require_same_as_dry_run(dry_run: Mapping[str, Any], dry_run_path: str, field: str, current: Any) -> None:
    predicted = dry_run.get(field)
    if predicted != current:
        raise MergedRegistryPublishError(
            f"--apply refused: {field} differs from the dry-run receipt {dry_run_path}. "
            f"dry-run: {json.dumps(predicted, sort_keys=True)}; this run: {json.dumps(current, sort_keys=True)}. "
            f"A changed plan, or a canonical manifest whose models changed since the dry-run, needs a new "
            f"--succession-id and its own dry-run (with --provision-succession-id naming the unchanged "
            f"provision). {_NOTHING_WRITTEN}"
        )
