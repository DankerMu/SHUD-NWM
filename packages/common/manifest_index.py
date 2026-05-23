from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, read_bytes_limited_no_follow

LOGGER = logging.getLogger(__name__)

REQUIRED_MANIFEST_ENTRY_FIELDS = (
    "task_id",
    "model_id",
    "basin_version_id",
    "river_network_version_id",
    "run_id",
    "source_id",
    "cycle_time",
    "workspace_dir",
)
OPTIONAL_MANIFEST_ENTRY_FIELDS = ("manifest_path",)
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
MAX_MANIFEST_INDEX_BYTES = 50_000_000
MAX_MANIFEST_INDEX_ENTRIES = 10_000


class ManifestValidationError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = "MANIFEST_INDEX_INVALID"
        self.message = message
        self.details = details or {}


def resolve_task_id(explicit_task_id: int | None) -> int:
    env_task_id = os.getenv("SLURM_ARRAY_TASK_ID")
    if explicit_task_id is not None:
        if env_task_id is not None and str(explicit_task_id) != env_task_id:
            LOGGER.info(
                "task_id resolved from explicit --task-id=%d (SLURM_ARRAY_TASK_ID=%s ignored)",
                explicit_task_id, env_task_id,
            )
        return explicit_task_id
    if env_task_id is None:
        LOGGER.info("task_id defaulted to 0 (no --task-id or SLURM_ARRAY_TASK_ID)")
        return 0
    try:
        resolved = int(env_task_id)
    except ValueError as exc:
        raise ManifestValidationError(
            "SLURM_ARRAY_TASK_ID is not a valid integer.",
            {"SLURM_ARRAY_TASK_ID": env_task_id},
        ) from exc
    LOGGER.info("task_id resolved from SLURM_ARRAY_TASK_ID=%d", resolved)
    return resolved


def validate_manifest_index_entry_count(entry_count: int, *, max_entries: int | None = None) -> None:
    limit = MAX_MANIFEST_INDEX_ENTRIES if max_entries is None else max_entries
    if entry_count > limit:
        raise ManifestValidationError(
            "Manifest index exceeds maximum entry count",
            {"entry_count": entry_count, "entry_limit": limit},
        )


def serialize_manifest_index(entries: Sequence[Mapping[str, Any]], *, max_bytes: int | None = None) -> bytes:
    """Serialize a manifest index while enforcing the worker-side read size limit."""

    limit = MAX_MANIFEST_INDEX_BYTES if max_bytes is None else max_bytes
    validate_manifest_index_entry_count(len(entries), max_entries=MAX_MANIFEST_INDEX_ENTRIES)
    encoder = json.JSONEncoder(indent=2, sort_keys=True)
    payload = bytearray()

    def append(chunk: str) -> None:
        payload.extend(chunk.encode("utf-8"))
        if len(payload) > limit:
            raise ManifestValidationError(
                "Manifest index file exceeds size limit",
                {"size": len(payload), "size_limit": limit},
            )

    append("[")
    for index, entry in enumerate(entries):
        if index:
            append(", ")
        for chunk in encoder.iterencode(entry):
            append(chunk)
    append("]")
    return bytes(payload)


def load_manifest_entry(manifest_index_path: str, task_id: int) -> dict[str, Any]:
    path = Path(manifest_index_path)
    try:
        raw = read_bytes_limited_no_follow(path, max_bytes=MAX_MANIFEST_INDEX_BYTES)
        if len(raw) > MAX_MANIFEST_INDEX_BYTES:
            raise ManifestValidationError(
                "Manifest index file exceeds size limit",
                {"manifest_index_path": manifest_index_path, "size_limit": MAX_MANIFEST_INDEX_BYTES},
            )
        data = json.loads(raw.decode("utf-8"))
    except (OSError, SafeFilesystemError) as exc:
        raise ManifestValidationError(
            f"Unable to safely read manifest index: {exc}",
            {"manifest_index_path": manifest_index_path, "error": str(exc)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(
            "Manifest index is not valid JSON.",
            {"manifest_index_path": manifest_index_path, "error": str(exc)},
        ) from exc

    if not isinstance(data, list):
        raise ManifestValidationError(
            "Manifest index must be a list.",
            {"manifest_index_path": manifest_index_path, "type": type(data).__name__},
        )
    if len(data) > MAX_MANIFEST_INDEX_ENTRIES:
        raise ManifestValidationError(
            "Manifest index exceeds maximum entry count",
            {
                "manifest_index_path": manifest_index_path,
                "entry_count": len(data),
                "entry_limit": MAX_MANIFEST_INDEX_ENTRIES,
            },
        )
    if not data:
        raise ManifestValidationError(
            "Manifest index is empty.",
            {"manifest_index_path": manifest_index_path, "task_id": task_id},
        )
    if task_id < 0 or task_id >= len(data):
        raise ManifestValidationError(
            "Manifest task_id is out of range.",
            {"manifest_index_path": manifest_index_path, "task_id": task_id, "entry_count": len(data)},
        )

    entry = data[task_id]
    if not isinstance(entry, Mapping):
        raise ManifestValidationError(
            "Manifest index entry must be an object.",
            {"manifest_index_path": manifest_index_path, "task_id": task_id, "type": type(entry).__name__},
        )

    result = dict(entry)
    missing = [field for field in REQUIRED_MANIFEST_ENTRY_FIELDS if result.get(field) in (None, "")]
    if missing:
        raise ManifestValidationError(
            "Manifest index entry is missing required fields.",
            {"manifest_index_path": manifest_index_path, "task_id": task_id, "missing_fields": missing},
        )
    for field in ("run_id", "model_id", "source_id", "basin_version_id", "river_network_version_id"):
        value = str(result.get(field, ""))
        if value and not SAFE_IDENTIFIER_RE.fullmatch(value):
            raise ManifestValidationError(
                f"Manifest entry field {field} contains unsafe characters: {value!r}",
                {"manifest_index_path": manifest_index_path, "task_id": task_id, "field": field, "value": value},
            )
    for field in OPTIONAL_MANIFEST_ENTRY_FIELDS:
        if field in result and not isinstance(result[field], str):
            raise ManifestValidationError(
                f"Manifest entry field {field} must be a string when present.",
                {"manifest_index_path": manifest_index_path, "task_id": task_id, "field": field},
            )
    if "manifest_path" in result:
        manifest_path = result["manifest_path"]
        if ".." in PurePath(manifest_path).parts:
            raise ManifestValidationError(
                "Manifest entry field manifest_path contains path traversal segments.",
                {
                    "manifest_index_path": manifest_index_path,
                    "task_id": task_id,
                    "field": "manifest_path",
                    "value": manifest_path,
                },
            )
    try:
        stored_task_id = int(result["task_id"])
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError(
            f"Manifest entry task_id is not a valid integer: {result.get('task_id')!r}",
            {"manifest_index_path": manifest_index_path, "task_id": task_id, "entry_task_id": result.get("task_id")},
        ) from exc
    if stored_task_id != task_id:
        raise ManifestValidationError(
            "Manifest index entry task_id does not match selected task.",
            {"manifest_index_path": manifest_index_path, "task_id": task_id, "entry_task_id": result["task_id"]},
        )
    return result
