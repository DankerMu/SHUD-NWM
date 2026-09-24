"""Basins registry import support: schema/page constants, the import error,
``ImportSources`` and dependency-free leaf helpers (JSON/report IO, required-
field readers, relative-path guards, row fetch/JSON adapters) (#2490 split of
``basins_registry_import``).

``workers.model_registry.basins_registry_import`` stays the stable import path
and re-exports every name defined here.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .basins_geometry import BasinsGeometryError, ParsedBasinsGeometry, TrustedBasinsRoot

BASINS_REGISTRY_IMPORT_SCHEMA_VERSION = "basins.registry_import.v1"
RIVER_SEGMENT_INSERT_PAGE_SIZE = 1000
PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID = "unknown"


class BasinsRegistryImportError(RuntimeError):
    """Raised when a Basins package cannot be imported into the registry."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        model_id: str | None = None,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.model_id = model_id
        self.path = path
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        if self.path is not None:
            payload["path"] = self.path
        payload.update(self.details)
        return payload


@dataclass(frozen=True)
class ImportSources:
    inventory: dict[str, Any]
    manifest: dict[str, Any]
    model: dict[str, Any]
    input_dir: TrustedBasinsRoot
    source_root: Path
    ids: dict[str, str]
    geometry: ParsedBasinsGeometry
    manifest_checksums: dict[str, str]


def _require_existing(condition: bool, resource: str, model_id: str) -> None:
    if not condition:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            f"Existing {resource} row does not match incoming Basins package/source checksums.",
            model_id=model_id,
            details={"resource": resource},
        )


def _raise_geometry_import_error(
    error: BasinsGeometryError,
    model_id: str,
    *,
    relative_path: str | None = None,
) -> None:
    error_code = error.error_code
    details = dict(error.details)
    if relative_path and error.error_code == "BASINS_REGISTRY_SOURCE_MISSING" and relative_path.startswith("gis/"):
        error_code = "BASINS_REGISTRY_GIS_SIDECAR_MISSING"
        details["missing_sidecar"] = relative_path
    raise BasinsRegistryImportError(
        error_code,
        str(error),
        model_id=model_id,
        path=error.path,
        details=details,
    ) from error


def _normalize_relative(value: str, model_id: str) -> str:
    path = _safe_relative(value, model_id)
    return path.as_posix()


def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return normalized or "unknown"


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json_object(path: str | Path, *, error_code: str, not_found_code: str) -> dict[str, Any]:
    payload, _ = _read_json_document(path, error_code=error_code, not_found_code=not_found_code)
    return payload


def _read_json_document(
    path: str | Path,
    *,
    error_code: str,
    not_found_code: str,
) -> tuple[dict[str, Any], bytes]:
    source = Path(path).expanduser()
    try:
        content = source.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except FileNotFoundError as error:
        raise BasinsRegistryImportError(
            not_found_code,
            f"JSON file cannot be read: {source}",
            path=str(source),
        ) from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BasinsRegistryImportError(error_code, f"JSON file is invalid: {source}", path=str(source)) from error
    if not isinstance(payload, dict):
        raise BasinsRegistryImportError(error_code, "JSON file must contain an object.", path=str(source))
    return payload, content


def _write_report(output_path: str | Path, report: dict[str, Any]) -> None:
    output = Path(output_path).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_OUTPUT_WRITE_FAILED",
            f"Basins registry import report cannot be written: {output}",
            path=str(output),
        ) from error


def _required_str(payload: dict[str, Any], key: str, error_code: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise BasinsRegistryImportError(error_code, f"Required field is missing: {key}")
    return value


def _required_model_str(model: dict[str, Any], key: str, model_id: str) -> str:
    value = model.get(key)
    if not isinstance(value, str) or not value:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            f"Basins inventory model record is missing {key}.",
            model_id=model_id,
        )
    return value


def _required_mapping_str(payload: dict[str, Any], key: str, model_id: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            f"Basins suggested_ids is missing {key}.",
            model_id=model_id,
        )
    return value


def _safe_relative(value: str, model_id: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins inventory contains an unsafe root-relative path.",
            model_id=model_id,
            path=value,
        )
    return path


def _recorded_relative_inventory_root(inventory: dict[str, Any]) -> Path | None:
    root = inventory.get("root")
    if not isinstance(root, str) or not root:
        return None
    root_path = Path(root).expanduser()
    if root_path.is_absolute():
        return None
    try:
        normalized = _safe_path_relative(root_path.as_posix())
    except ValueError:
        return None
    if normalized == Path("."):
        return None
    return normalized


def _recorded_path_matches_expected(
    value: str,
    expected_path: Path,
    inventory_root: Path,
    inventory_relative_root: Path | None,
) -> bool:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve() == expected_path
    try:
        normalized = _safe_path_relative(path.as_posix())
    except ValueError:
        return False
    expected_relative_paths: set[Path] = set()
    for base in (expected_path, inventory_root):
        try:
            expected_relative_paths.add(expected_path.relative_to(base))
        except ValueError:
            continue
    if inventory_relative_root is not None:
        try:
            expected_relative_paths.add(inventory_relative_root / expected_path.relative_to(inventory_root))
        except ValueError:
            pass
    return normalized in expected_relative_paths


def _safe_path_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(value)
    return path


def _ensure_under_root(path: Path, root: Path, model_id: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path resolves outside the inventory root.",
            model_id=model_id,
            path=str(path),
        ) from error


def _reject_directory_symlink(path: Path, role: str, model_id: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source directory cannot be safely inspected.",
            model_id=model_id,
            path=str(path),
            details={"role": role},
        ) from error
    if stat.S_ISLNK(mode):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source directory component is a symlink.",
            model_id=model_id,
            path=str(path),
            details={"role": role},
        )


def _basin_name(model: dict[str, Any]) -> str:
    basin_slug = str(model.get("basin_slug") or "")
    return basin_slug.replace("_", " ").replace("/", " / ").replace("-", " ").title() or str(model.get("model_id"))


def _version_label(sources: ImportSources) -> str:
    version = str(sources.manifest.get("version") or "vbasins")
    return version


def _mesh_uri(sources: ImportSources) -> str:
    relative = f"{sources.model['shud_input_name']}.sp.mesh"
    for entry in sources.manifest.get("included_files") or []:
        if isinstance(entry, dict) and entry.get("relative_path") == relative and entry.get("object_uri"):
            return str(entry["object_uri"])
    return str(sources.manifest["model_package_uri"]).rstrip("/") + "/" + relative


def _source_checksum(sources: ImportSources, relative_path: str) -> str | None:
    checksum = sources.manifest_checksums.get(relative_path)
    if isinstance(checksum, str) and checksum:
        return checksum
    checksums = sources.model.get("checksums")
    if isinstance(checksums, dict) and isinstance(checksums.get(relative_path), str):
        return str(checksums[relative_path])
    return None


def _json(value: dict[str, Any]) -> Any:
    try:
        from psycopg2.extras import Json
    except ImportError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PSYCOPG_MISSING",
            "psycopg2 is required for Basins registry import.",
        ) from error
    return Json(value)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _fetch_optional(cursor: Any, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor.execute(statement, parameters)
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]
