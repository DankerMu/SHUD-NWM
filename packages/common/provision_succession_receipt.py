"""Succession receipt of the direct-grid provision step.

``scripts/provision_direct_grid_scheduler_registry.py`` is the first step of a
model succession and the only one that writes the database.  A run given a
succession id leaves exactly one receipt per mode under
``<receipt-root>/<succession-id>/``; an apply refuses unless the dry-run receipt
of the same succession predicted exactly what it is about to register.

Paths are recorded as seen from the host that ran the step; ``object_store_key``
is what the other node resolves against its own mount of the same store.
"""

from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.source_identity import normalize_source_id

# The step-independent helpers live in ``packages.common.succession_receipt``
# (the publish step shares them); they stay importable from this module.
from packages.common.succession_receipt import (
    DEFAULT_RECEIPT_ROOT_KEY,
    SUCCESSION_ID_PATTERN,
    SuccessionReceiptError,
    default_receipt_root,
    file_sha256,
    git_commit,
    object_store_key,
    path_record,
    prepare_receipt_target,
    validate_succession_id,
    write_receipt,
)

__all__ = (
    "APPLY_RECEIPT_NAME",
    "DEFAULT_RECEIPT_ROOT_KEY",
    "DRY_RUN_NOTICE",
    "DRY_RUN_RECEIPT_NAME",
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_STEP",
    "SOURCE_GRID_FIELDS",
    "SUCCESSION_ID_PATTERN",
    "SuccessionReceiptError",
    "default_receipt_root",
    "file_sha256",
    "git_commit",
    "load_dry_run_receipt",
    "object_store_key",
    "path_record",
    "prepare_receipt_target",
    "receipt_header",
    "receipt_path",
    "require_predicted_variant",
    "require_same_inputs",
    "require_same_source_grids",
    "require_same_variant_set",
    "source_grid_record",
    "validate_succession_id",
    "write_receipt",
    "write_run_receipt",
)

RECEIPT_SCHEMA_VERSION = "nhms.model_succession.provision_receipt.v1"
RECEIPT_STEP = "provision"
DRY_RUN_RECEIPT_NAME = "provision-dry-run.json"
APPLY_RECEIPT_NAME = "provision-apply.json"
SOURCE_GRID_FIELDS = ("source_id", "grid_id", "grid_snapshot_id", "grid_signature", "canonical_grid_key")

_NOTHING_WRITTEN = "Nothing was written."
_ROLLED_BACK = "The database transaction was rolled back, no registry was published and no apply receipt was written."
_ROLLED_BACK_AFTER_BUILD = (
    f"{_ROLLED_BACK} Any variant package this run built on the object store stays in place, with its "
    "direct_grid_build_receipt.json, and so do the readable file modes this run set on packages."
)


DRY_RUN_NOTICE = (
    "DRY-RUN (no --apply): nothing is written to the database, the object store or --output-registry"
    " (only this run's receipt, when --succession-id is given). An --apply always writes: it updates each"
    " variant row's model_package_uri/resource_profile and upserts one met.met_station row per station,"
    " also where the plan says inserted=false."
)


def receipt_path(receipt_root: str | Path, succession_id: str, *, apply: bool) -> Path:
    return Path(receipt_root) / succession_id / (APPLY_RECEIPT_NAME if apply else DRY_RUN_RECEIPT_NAME)


def receipt_header(
    *,
    succession_id: str | None,
    apply: bool,
    operator_id: str,
    object_store_root: str | Path,
    object_store_prefix: str,
    selected_model_ids: Sequence[str],
    baseline_registry: str | Path,
    baseline_registry_sha256: str,
    output_registry: str | Path,
) -> dict[str, Any]:
    """Return the receipt fields known before any build or database statement."""

    roots = {"object_store_root": object_store_root, "object_store_prefix": object_store_prefix}
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "succession_id": succession_id,
        "step": RECEIPT_STEP,
        "dry_run": not apply,
        "outcome": "applied" if apply else "planned",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "operator_id": operator_id,
        "host": socket.gethostname(),
        "git_commit": git_commit(),
        "object_store_root": str(object_store_root),
        "object_store_prefix": object_store_prefix,
        "selected_model_ids": sorted(selected_model_ids),
        "baseline_registry": path_record(baseline_registry, sha256=baseline_registry_sha256, **roots),
        "output_registry": path_record(output_registry, sha256=None, **roots),
    }


def source_grid_record(snapshot: Any) -> dict[str, Any]:
    """Return one ``source_grids[]`` row from a canonical grid snapshot."""

    return {
        "source_id": normalize_source_id(snapshot.source_id),
        "grid_id": snapshot.grid_id,
        "grid_snapshot_id": str(snapshot.grid_snapshot_id),
        "grid_signature": snapshot.grid_signature,
        "canonical_grid_key": snapshot.canonical_grid_key,
    }


def write_run_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Write the receipt of a finished run; an apply that cannot is reported as done but unreceipted."""

    try:
        write_receipt(path, receipt)
    except OSError as error:
        if receipt["dry_run"]:
            raise SuccessionReceiptError(f"Cannot write the dry-run receipt {path}: {error}") from error
        raise SuccessionReceiptError(
            "The database transaction was committed and the registry was published to "
            f"{receipt['output_registry']['path']}, but the apply receipt {path} could NOT be written: {error}. "
            "The provisioning itself is done; a retry needs a new --succession-id and its own dry-run, and "
            "its receipt will say inserted=false."
        ) from error


# --- apply requires its dry-run ---------------------------------------------


def load_dry_run_receipt(
    receipt_root: str | Path,
    succession_id: str,
    *,
    object_store_root: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(dry-run receipt, its path record)`` or refuse naming the expected path."""

    path = receipt_path(receipt_root, succession_id, apply=False)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise SuccessionReceiptError(
            f"--apply requires the dry-run receipt of succession {succession_id!r} at {path} ({error}). "
            "Run the same command without --apply and with the same --succession-id first."
        ) from error
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise SuccessionReceiptError(f"Dry-run receipt {path} is not valid JSON: {error}") from error
    expected = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "step": RECEIPT_STEP,
        "dry_run": True,
        "succession_id": succession_id,
    }
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected.items()):
        raise SuccessionReceiptError(f"{path} is not a provision dry-run receipt of succession {succession_id!r}.")
    record = path_record(path, object_store_root=object_store_root, sha256=hashlib.sha256(content).hexdigest())
    return payload, record


def _refuse(dry_run_path: str, field: str, predicted: Any, current: Any, consequence: str) -> None:
    raise SuccessionReceiptError(
        f"--apply refused: {field} differs from the dry-run receipt {dry_run_path}. "
        f"dry-run: {json.dumps(predicted, sort_keys=True)}; this run: {json.dumps(current, sort_keys=True)}. "
        f"{consequence}"
    )


def require_same_inputs(dry_run: Mapping[str, Any], dry_run_path: str, header: Mapping[str, Any]) -> None:
    """Compare what is known before the database is opened; ``operator_id`` is not compared."""

    pairs = (
        ("baseline_registry.sha256", lambda item: (item.get("baseline_registry") or {}).get("sha256")),
        ("object_store_prefix", lambda item: item.get("object_store_prefix")),
        ("output_registry.path", lambda item: (item.get("output_registry") or {}).get("path")),
        ("selected_model_ids", lambda item: item.get("selected_model_ids")),
    )
    for field, read in pairs:
        if read(dry_run) != read(header):
            _refuse(dry_run_path, field, read(dry_run), read(header), _NOTHING_WRITTEN)


def require_same_source_grids(
    dry_run: Mapping[str, Any],
    dry_run_path: str,
    source_grids: Sequence[Mapping[str, Any]],
) -> None:
    def projection(grids: Any) -> list[dict[str, Any]]:
        return [{field: grid.get(field) for field in SOURCE_GRID_FIELDS} for grid in grids or []]

    if projection(dry_run.get("source_grids")) != projection(source_grids):
        _refuse(
            dry_run_path,
            "source_grids",
            projection(dry_run.get("source_grids")),
            projection(source_grids),
            _ROLLED_BACK,
        )


def _variant_keys(models: Any) -> dict[tuple[str, str], str]:
    return {
        (str(model.get("baseline_model_id")), str(model.get("source_id"))): str(model.get("model_id"))
        for model in models or []
    }


def require_predicted_variant(dry_run: Mapping[str, Any], dry_run_path: str, variant: Mapping[str, Any]) -> None:
    """Refuse a variant whose ``model_id`` the dry-run did not predict (called before it is registered)."""

    key = (str(variant["baseline_model_id"]), str(variant["source_id"]))
    predicted = _variant_keys(dry_run.get("models")).get(key)
    if predicted != str(variant["model_id"]):
        _refuse(
            dry_run_path,
            f"model_id of baseline_model_id={key[0]!r} source_id={key[1]!r}",
            predicted,
            str(variant["model_id"]),
            _ROLLED_BACK_AFTER_BUILD,
        )


def require_same_variant_set(
    dry_run: Mapping[str, Any],
    dry_run_path: str,
    variants: Sequence[Mapping[str, Any]],
) -> None:
    predicted = _variant_keys(dry_run.get("models"))
    current = _variant_keys(variants)
    if predicted != current:
        _refuse(
            dry_run_path,
            "the set of (baseline_model_id, source_id, model_id)",
            sorted([*key, value] for key, value in predicted.items()),
            sorted([*key, value] for key, value in current.items()),
            _ROLLED_BACK_AFTER_BUILD,
        )
