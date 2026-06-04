"""Canonical M24 live-closure receipt schema, validator, and safe writer.

Every M24 section writes ``artifacts/m24/<run_id>/<section>.json`` where
``section`` is one of :data:`RECEIPT_SECTIONS`. :func:`validate_receipt`
enforces the field contract shared by §0–§4; :func:`write_receipt` validates,
redacts, and atomically persists a receipt at ``0600``.

Field contract (required unless marked nullable):

- ``schema_version`` (str), ``contract_id`` (str), ``run_id`` (str),
  ``node`` (str), ``command`` (str), ``timestamp`` (ISO-8601 str),
  ``status`` (enum ``PASS|BLOCKED``),
  ``execution_mode`` (enum ``live_proof|deterministic``),
  ``live_proof_accepted`` (bool), ``dependency_blocker`` (str, nullable),
  ``redaction`` (obj ``{db_dsn_redacted:bool, bounds:obj}``),
  ``artifact_refs`` (list of ``{kind,uri}``),
  ``identity`` (obj ``{source,cycle_time,model_id,basin_id,basin_version_id,
  river_network_version_id}``),
  ``stages`` (list of ``{stage,status,counts:obj}``),
  ``slurm`` (obj ``{job_id,array_task_id(nullable),original_task_id(nullable),
  accounting,log_uri}``),
  ``published_uri`` (str, nullable),
  ``warm_start_quality`` (enum ``fresh|degraded_stale_init_state|
  cold_start_no_state|cold_start_stale_state``, nullable).

Hard rule: a ``BLOCKED`` receipt MUST set a non-empty ``dependency_blocker``
and MUST NOT set ``live_proof_accepted == true``.

Empty-skeleton convention for baseline-style sections: nullable run-time fields
(``slurm.job_id``, ``identity.model_id``, ...) MAY be ``None``, but the keys
themselves MUST be present. A section with no Slurm semantics declares the
``slurm`` object with every required sub-key set to ``None`` rather than
omitting the object. The same applies to ``identity``: required sub-keys are
present, nullable ones may carry ``None`` placeholders. ``validate_receipt``
checks key presence and type (when non-``None``), never wall-clock truthiness.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.redaction import redact_payload
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
)

SCHEMA_VERSION = "nhms.m24.live_receipt.v1"
CONTRACT_ID = "nhms.m24.live_receipt.contract.v1"

STATUS_VALUES = ("PASS", "BLOCKED")
EXECUTION_MODE_VALUES = ("live_proof", "deterministic")
WARM_START_QUALITY_VALUES = (
    "fresh",
    "degraded_stale_init_state",
    "cold_start_no_state",
    "cold_start_stale_state",
)
RECEIPT_SECTIONS = frozenset(
    {"baseline", "gateway", "warm_start", "concurrency", "multibasin", "daemon"}
)

DEFAULT_RECEIPT_ROOT = "artifacts/m24"

# Top-level keys that are required to be present (may be None when nullable).
_REQUIRED_TOP_KEYS = (
    "schema_version",
    "contract_id",
    "run_id",
    "node",
    "command",
    "timestamp",
    "status",
    "execution_mode",
    "live_proof_accepted",
    "dependency_blocker",
    "redaction",
    "artifact_refs",
    "identity",
    "stages",
    "slurm",
    "published_uri",
    "warm_start_quality",
)
# Top-level keys whose value may be None.
_NULLABLE_TOP_KEYS = frozenset(
    {"dependency_blocker", "published_uri", "warm_start_quality"}
)

_IDENTITY_KEYS = (
    "source",
    "cycle_time",
    "model_id",
    "basin_id",
    "basin_version_id",
    "river_network_version_id",
)
_REDACTION_KEYS = ("db_dsn_redacted", "bounds")
_SLURM_KEYS = (
    "job_id",
    "array_task_id",
    "original_task_id",
    "accounting",
    "log_uri",
)
# Slurm sub-keys that may be None (empty-skeleton baseline convention treats the
# whole object as nullable sub-fields, so every Slurm sub-key is nullable).
_SLURM_NULLABLE_KEYS = frozenset(_SLURM_KEYS)


class ReceiptValidationError(ValueError):
    """Raised when a receipt violates the canonical M24 contract."""


def validate_receipt(receipt: dict) -> None:
    """Validate ``receipt`` against the canonical M24 contract.

    Raises :class:`ReceiptValidationError` on the first contract violation.
    """

    if not isinstance(receipt, Mapping):
        raise ReceiptValidationError("receipt must be a mapping/object")

    _require_keys(receipt, _REQUIRED_TOP_KEYS, where="receipt")

    _require_str(receipt, "schema_version")
    _require_str(receipt, "contract_id")
    _require_str(receipt, "run_id")
    _require_str(receipt, "node")
    _require_str(receipt, "command")

    _validate_timestamp(receipt["timestamp"])
    _require_enum(receipt, "status", STATUS_VALUES)
    _require_enum(receipt, "execution_mode", EXECUTION_MODE_VALUES)
    _require_bool(receipt, "live_proof_accepted")

    _require_nullable_str(receipt, "dependency_blocker")
    _require_nullable_str(receipt, "published_uri")
    _require_nullable_enum(receipt, "warm_start_quality", WARM_START_QUALITY_VALUES)

    _validate_redaction(receipt["redaction"])
    _validate_artifact_refs(receipt["artifact_refs"])
    _validate_identity(receipt["identity"])
    _validate_stages(receipt["stages"])
    _validate_slurm(receipt["slurm"])

    _validate_blocked_rules(receipt)


def receipt_path(run_id: str, section: str, *, root: str | Path = DEFAULT_RECEIPT_ROOT) -> Path:
    """Return ``<root>/<run_id>/<section>.json`` after safely creating the dir.

    ``run_id`` and ``section`` must be safe path components. ``section`` must be
    a known :data:`RECEIPT_SECTIONS` member.
    """

    if section not in RECEIPT_SECTIONS:
        raise ReceiptValidationError(
            f"unknown receipt section {section!r}; expected one of {sorted(RECEIPT_SECTIONS)}"
        )
    _require_safe_component(run_id, "run_id")
    directory = Path(root).expanduser() / run_id
    try:
        ensure_directory_no_follow(directory)
    except SafeFilesystemError as error:
        raise ReceiptValidationError(f"failed to create receipt directory {directory}: {error}") from error
    return directory / f"{section}.json"


def write_receipt(receipt: dict, *, root: str | Path = DEFAULT_RECEIPT_ROOT) -> Path:
    """Validate, redact, and atomically write a receipt at ``0600``.

    The destination section is taken from the receipt's own ``section`` field,
    which MUST be a known :data:`RECEIPT_SECTIONS` member. The file is written
    to ``<root>/<run_id>/<section>.json``. Redaction is applied before writing
    so no DSN/secret can land on disk.
    """

    validate_receipt(receipt)
    section = receipt.get("section")
    if not isinstance(section, str) or section not in RECEIPT_SECTIONS:
        raise ReceiptValidationError(
            "write_receipt requires receipt['section'] to be a known section name"
        )
    run_id = receipt["run_id"]
    target = receipt_path(run_id, section, root=root)

    redacted = redact_payload(_to_plain(receipt))
    content = (json.dumps(redacted, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        atomic_write_bytes_no_follow(target, content)
    except SafeFilesystemError as error:
        raise ReceiptValidationError(f"failed to write receipt {target}: {error}") from error
    try:
        os.chmod(target, 0o600)
    except OSError as error:
        raise ReceiptValidationError(f"failed to chmod receipt {target}: {error}") from error
    return target


# --- internal validation helpers -------------------------------------------------


def _require_keys(value: Mapping[str, Any], keys: tuple[str, ...], *, where: str) -> None:
    missing = [key for key in keys if key not in value]
    if missing:
        raise ReceiptValidationError(f"{where} is missing required keys: {missing}")


def _require_str(value: Mapping[str, Any], key: str) -> None:
    item = value[key]
    if not isinstance(item, str) or not item:
        raise ReceiptValidationError(f"{key!r} must be a non-empty string")


def _require_bool(value: Mapping[str, Any], key: str) -> None:
    if not isinstance(value[key], bool):
        raise ReceiptValidationError(f"{key!r} must be a bool")


def _require_enum(value: Mapping[str, Any], key: str, allowed: tuple[str, ...]) -> None:
    item = value[key]
    if item not in allowed:
        raise ReceiptValidationError(f"{key!r} must be one of {allowed}, got {item!r}")


def _require_nullable_str(value: Mapping[str, Any], key: str) -> None:
    item = value[key]
    if item is None:
        return
    if not isinstance(item, str) or not item:
        raise ReceiptValidationError(f"{key!r} must be a non-empty string or null")


def _require_nullable_enum(value: Mapping[str, Any], key: str, allowed: tuple[str, ...]) -> None:
    item = value[key]
    if item is None:
        return
    if item not in allowed:
        raise ReceiptValidationError(f"{key!r} must be one of {allowed} or null, got {item!r}")


def _check_nullable_nonempty_str(item: Any, label: str) -> None:
    """None passes; otherwise must be a non-empty string."""
    if item is None:
        return
    if not isinstance(item, str) or not item:
        raise ReceiptValidationError(f"{label} must be a non-empty string or null")


def _check_nullable_int_or_str(item: Any, label: str) -> None:
    """None passes; otherwise must be an int or str (bool rejected)."""
    if item is None:
        return
    if isinstance(item, bool) or not isinstance(item, int | str):
        raise ReceiptValidationError(f"{label} must be an int, string, or null")


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ReceiptValidationError("'timestamp' must be a non-empty ISO-8601 string")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise ReceiptValidationError(f"'timestamp' is not ISO-8601 parseable: {value!r}") from error


def _validate_redaction(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ReceiptValidationError("'redaction' must be an object")
    _require_keys(value, _REDACTION_KEYS, where="redaction")
    if not isinstance(value["db_dsn_redacted"], bool):
        raise ReceiptValidationError("'redaction.db_dsn_redacted' must be a bool")
    if not isinstance(value["bounds"], Mapping):
        raise ReceiptValidationError("'redaction.bounds' must be an object")


def _validate_artifact_refs(value: Any) -> None:
    if not isinstance(value, list):
        raise ReceiptValidationError("'artifact_refs' must be a list")
    for index, ref in enumerate(value):
        if not isinstance(ref, Mapping):
            raise ReceiptValidationError(f"artifact_refs[{index}] must be an object")
        _require_keys(ref, ("kind", "uri"), where=f"artifact_refs[{index}]")
        if not isinstance(ref["kind"], str) or not ref["kind"]:
            raise ReceiptValidationError(f"artifact_refs[{index}].kind must be a non-empty string")
        if not isinstance(ref["uri"], str) or not ref["uri"]:
            raise ReceiptValidationError(f"artifact_refs[{index}].uri must be a non-empty string")


def _validate_identity(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ReceiptValidationError("'identity' must be an object")
    _require_keys(value, _IDENTITY_KEYS, where="identity")
    # All identity sub-keys are nullable strings: None passes (baseline-style
    # placeholders), but a present value MUST be a non-empty string. §3B/§4 rely
    # on these id fields being strongly typed when populated.
    for key in _IDENTITY_KEYS:
        _check_nullable_nonempty_str(value[key], f"identity.{key}")


def _validate_stages(value: Any) -> None:
    if not isinstance(value, list):
        raise ReceiptValidationError("'stages' must be a list")
    for index, stage in enumerate(value):
        if not isinstance(stage, Mapping):
            raise ReceiptValidationError(f"stages[{index}] must be an object")
        _require_keys(stage, ("stage", "status", "counts"), where=f"stages[{index}]")
        if not isinstance(stage["stage"], str) or not stage["stage"]:
            raise ReceiptValidationError(f"stages[{index}].stage must be a non-empty string")
        if stage["status"] not in STATUS_VALUES:
            raise ReceiptValidationError(
                f"stages[{index}].status must be one of {STATUS_VALUES}, got {stage['status']!r}"
            )
        if not isinstance(stage["counts"], Mapping):
            raise ReceiptValidationError(f"stages[{index}].counts must be an object")


def _validate_slurm(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ReceiptValidationError("'slurm' must be an object")
    _require_keys(value, _SLURM_KEYS, where="slurm")
    # Empty-skeleton convention: every Slurm sub-key may be None but must exist.
    # When present, job_id/log_uri are strings, accounting is an object.
    if value["job_id"] is not None and (
        isinstance(value["job_id"], bool) or not isinstance(value["job_id"], str | int)
    ):
        raise ReceiptValidationError("slurm.job_id must be a string/int or null")
    # reindex identity-mapping fields (§3B): None or int/str.
    _check_nullable_int_or_str(value["array_task_id"], "slurm.array_task_id")
    _check_nullable_int_or_str(value["original_task_id"], "slurm.original_task_id")
    if value["accounting"] is not None and not isinstance(value["accounting"], Mapping):
        raise ReceiptValidationError("slurm.accounting must be an object or null")
    if value["log_uri"] is not None and (not isinstance(value["log_uri"], str) or not value["log_uri"]):
        raise ReceiptValidationError("slurm.log_uri must be a non-empty string or null")


def _validate_blocked_rules(receipt: Mapping[str, Any]) -> None:
    if receipt["status"] != "BLOCKED":
        return
    blocker = receipt["dependency_blocker"]
    if not isinstance(blocker, str) or not blocker.strip():
        raise ReceiptValidationError(
            "BLOCKED receipt MUST set a non-empty 'dependency_blocker'"
        )
    if receipt["live_proof_accepted"] is True:
        raise ReceiptValidationError(
            "BLOCKED receipt MUST NOT set 'live_proof_accepted' to true"
        )


def _require_safe_component(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ReceiptValidationError(f"{label} must be a non-empty string")
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ReceiptValidationError(f"{label} must be a safe single path component: {value!r}")


def _to_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _to_plain(nested) for key, nested in value.items()}
    if isinstance(value, list | tuple):
        return [_to_plain(item) for item in value]
    return value
