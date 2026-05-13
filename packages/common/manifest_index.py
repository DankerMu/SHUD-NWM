from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REQUIRED_MANIFEST_ENTRY_FIELDS = ("task_id", "model_id", "basin_version_id", "run_id", "workspace_dir")


class ManifestValidationError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = "MANIFEST_INDEX_INVALID"
        self.message = message
        self.details = details or {}


def load_manifest_entry(manifest_index_path: str, task_id: int) -> dict[str, Any]:
    try:
        data = json.loads(Path(manifest_index_path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestValidationError(
            "Unable to read manifest index.",
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
    if int(result["task_id"]) != task_id:
        raise ManifestValidationError(
            "Manifest index entry task_id does not match selected task.",
            {"manifest_index_path": manifest_index_path, "task_id": task_id, "entry_task_id": result["task_id"]},
        )
    return result
