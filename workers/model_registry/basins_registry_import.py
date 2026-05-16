from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .basins_geometry import BasinsGeometryError, ParsedBasinsGeometry, parse_basins_geometry

BASINS_REGISTRY_IMPORT_SCHEMA_VERSION = "basins.registry_import.v1"


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
    input_dir: Path
    source_root: Path
    ids: dict[str, str]
    geometry: ParsedBasinsGeometry


def import_basins_registry(
    *,
    inventory_path: str | Path,
    package_manifest_path: str | Path,
    database_url: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    inventory = _read_json_object(
        inventory_path,
        error_code="BASINS_REGISTRY_INVENTORY_INVALID",
        not_found_code="BASINS_REGISTRY_INVENTORY_NOT_FOUND",
    )
    manifest = _read_json_object(
        package_manifest_path,
        error_code="BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
        not_found_code="BASINS_REGISTRY_PACKAGE_MANIFEST_NOT_FOUND",
    )
    sources = _prepare_sources(inventory, manifest)
    resolved_database_url = database_url or os.getenv("DATABASE_URL", "").strip()
    if not resolved_database_url:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_DATABASE_URL_MISSING",
            "DATABASE_URL or --database-url is required for Basins registry import.",
            model_id=str(manifest.get("model_id") or ""),
        )
    report = _import_prepared_sources(sources, resolved_database_url)
    if output_path is not None:
        _write_report(output_path, report)
    return report


def prepare_basins_import_sources(
    *,
    inventory_path: str | Path,
    package_manifest_path: str | Path,
) -> ImportSources:
    inventory = _read_json_object(
        inventory_path,
        error_code="BASINS_REGISTRY_INVENTORY_INVALID",
        not_found_code="BASINS_REGISTRY_INVENTORY_NOT_FOUND",
    )
    manifest = _read_json_object(
        package_manifest_path,
        error_code="BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
        not_found_code="BASINS_REGISTRY_PACKAGE_MANIFEST_NOT_FOUND",
    )
    return _prepare_sources(inventory, manifest)


def _prepare_sources(inventory: dict[str, Any], manifest: dict[str, Any]) -> ImportSources:
    model_id = _required_str(manifest, "model_id", "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID")
    model = _find_inventory_model(inventory, model_id)
    if manifest.get("schema_version") != "basins.package.v1":
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
            "Basins package manifest schema_version must be basins.package.v1.",
            model_id=model_id,
        )
    if manifest.get("package_checksum") in (None, "") or manifest.get("model_package_uri") in (None, ""):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
            "Basins package manifest must include package_checksum and model_package_uri.",
            model_id=model_id,
        )
    if model.get("status") != "valid" or model.get("default_import_eligible") is not True:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_MODEL_NOT_IMPORTABLE",
            "Basins model is not importable from this inventory.",
            model_id=model_id,
            path=str(model.get("source_path") or ""),
        )
    if manifest.get("basin_slug") not in (None, model.get("basin_slug")):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest basin_slug does not match inventory.",
            model_id=model_id,
        )

    inventory_root = _inventory_root(inventory, model_id)
    source_root = _source_root(inventory_root, model, model_id)
    input_dir = _input_dir(source_root, model, model_id)
    required_files = model.get("required_files")
    if not isinstance(required_files, dict):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory model record is missing required_files.",
            model_id=model_id,
        )
    try:
        geometry = parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name=_required_model_str(model, "shud_input_name", model_id),
            required_files=required_files,
        )
    except BasinsGeometryError as error:
        raise BasinsRegistryImportError(
            error.error_code,
            str(error),
            model_id=model_id,
            path=error.path,
            details=error.details,
        ) from error
    return ImportSources(
        inventory=inventory,
        manifest=manifest,
        model=model,
        input_dir=input_dir,
        source_root=source_root,
        ids=_registry_ids(model, model_id),
        geometry=geometry,
    )


def _import_prepared_sources(sources: ImportSources, database_url: str) -> dict[str, Any]:
    try:
        with _transaction(database_url) as cursor:
            row_counts = {
                "basin": _ensure_basin(cursor, sources),
                "basin_version": _ensure_basin_version(cursor, sources),
                "river_network_version": _ensure_river_network(cursor, sources),
                "river_segment": _ensure_river_segments(cursor, sources),
                "mesh_version": _ensure_mesh(cursor, sources),
                "model_instance": _ensure_model_instance(cursor, sources),
            }
    except BasinsRegistryImportError:
        raise
    except Exception as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_DATABASE_ERROR",
            f"Basins registry import database operation failed: {error.__class__.__name__}",
            model_id=str(sources.manifest.get("model_id") or ""),
        ) from error

    status = "already_imported" if all(count == 0 for count in row_counts.values()) else "imported"
    return {
        "schema_version": BASINS_REGISTRY_IMPORT_SCHEMA_VERSION,
        "status": status,
        "model_id": sources.ids["model_id"],
        "basin_id": sources.ids["basin_id"],
        "basin_version_id": sources.ids["basin_version_id"],
        "river_network_version_id": sources.ids["river_network_version_id"],
        "mesh_version_id": sources.ids["mesh_version_id"],
        "active": False
        if row_counts["model_instance"] == 1
        else _model_active_state(database_url, sources.ids["model_id"]),
        "segment_count": sources.geometry.segment_count,
        "row_counts": row_counts,
        "model_package_uri": sources.manifest["model_package_uri"],
        "package_checksum": sources.manifest["package_checksum"],
    }


def _ensure_basin(cursor: Any, sources: ImportSources) -> int:
    basin_id = sources.ids["basin_id"]
    existing = _fetch_optional(cursor, "SELECT basin_id FROM core.basin WHERE basin_id = %s", (basin_id,))
    if existing is not None:
        return 0
    cursor.execute(
        """
        INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
        VALUES (%s, %s, %s, %s)
        """,
        (
            basin_id,
            _basin_name(sources.model),
            "Basins",
            "Imported from Basins discovery inventory and package manifest.",
        ),
    )
    return 1


def _ensure_basin_version(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    source_uri = sources.geometry.domain_source_uri
    checksum = sources.geometry.domain_checksum
    existing = _fetch_optional(
        cursor,
        """
        SELECT basin_id, source_uri, checksum
        FROM core.basin_version
        WHERE basin_version_id = %s
        """,
        (ids["basin_version_id"],),
    )
    if existing is not None:
        _require_existing(
            existing["basin_id"] == ids["basin_id"]
            and existing["source_uri"] == source_uri
            and existing["checksum"] == checksum,
            "basin_version",
            ids["model_id"],
        )
        return 0
    cursor.execute(
        """
        INSERT INTO core.basin_version (
            basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
        )
        VALUES (%s, %s, %s, ST_GeomFromText(%s, 4490), false, %s, %s)
        """,
        (
            ids["basin_version_id"],
            ids["basin_id"],
            _version_label(sources),
            sources.geometry.domain_wkt,
            source_uri,
            checksum,
        ),
    )
    return 1


def _ensure_river_network(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    source_uri = sources.geometry.river_network_source_uri
    checksum = sources.geometry.river_network_checksum
    existing = _fetch_optional(
        cursor,
        """
        SELECT basin_version_id, segment_count, source_uri, checksum
        FROM core.river_network_version
        WHERE river_network_version_id = %s
        """,
        (ids["river_network_version_id"],),
    )
    if existing is not None:
        _require_existing(
            existing["basin_version_id"] == ids["basin_version_id"]
            and int(existing["segment_count"]) == sources.geometry.segment_count
            and existing["source_uri"] == source_uri
            and existing["checksum"] == checksum,
            "river_network_version",
            ids["model_id"],
        )
        return 0
    cursor.execute(
        """
        INSERT INTO core.river_network_version (
            river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            ids["river_network_version_id"],
            ids["basin_version_id"],
            _version_label(sources),
            sources.geometry.segment_count,
            source_uri,
            checksum,
        ),
    )
    return 1


def _ensure_river_segments(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    existing = _fetch_optional(
        cursor,
        "SELECT COUNT(*) AS count FROM core.river_segment WHERE river_network_version_id = %s",
        (ids["river_network_version_id"],),
    )
    existing_count = int(existing["count"]) if existing is not None else 0
    if existing_count:
        _require_existing(existing_count == sources.geometry.segment_count, "river_segment", ids["model_id"])
        return 0
    try:
        from psycopg2.extras import Json, execute_values
    except ImportError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PSYCOPG_MISSING",
            "psycopg2 is required for Basins registry import.",
            model_id=ids["model_id"],
        ) from error
    rows = [
        (
            segment.river_segment_id,
            ids["river_network_version_id"],
            segment.segment_order,
            segment.downstream_segment_id,
            segment.length_m,
            segment.geom_wkt,
            Json(
                {
                    **segment.properties,
                    "basin_slug": sources.model.get("basin_slug"),
                    "shud_input_name": sources.model.get("shud_input_name"),
                }
            ),
        )
        for segment in sources.geometry.river_segments
    ]
    execute_values(
        cursor,
        """
        INSERT INTO core.river_segment (
            river_segment_id, river_network_version_id, segment_order, downstream_segment_id,
            length_m, geom, properties_json
        )
        VALUES %s
        """,
        rows,
        template="(%s, %s, %s, %s, %s, ST_GeomFromText(%s, 4490), %s)",
    )
    return len(rows)


def _ensure_mesh(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    mesh_uri = _mesh_uri(sources)
    checksum = _source_checksum(sources, f"{sources.model['shud_input_name']}.sp.mesh")
    properties = {
        "basin_slug": sources.model.get("basin_slug"),
        "shud_input_name": sources.model.get("shud_input_name"),
        "manifest_uri": sources.manifest.get("manifest_uri"),
        "package_checksum": sources.manifest.get("package_checksum"),
        "source_inventory_checksum": sources.manifest.get("source_inventory_checksum"),
        "source_path": sources.model.get("source_path"),
        "resolved_source_path": sources.model.get("resolved_source_path"),
    }
    existing = _fetch_optional(
        cursor,
        """
        SELECT basin_version_id, mesh_uri, checksum, properties_json
        FROM core.mesh_version
        WHERE mesh_version_id = %s
        """,
        (ids["mesh_version_id"],),
    )
    if existing is not None:
        _require_existing(
            existing["basin_version_id"] == ids["basin_version_id"]
            and existing["mesh_uri"] == mesh_uri
            and existing["checksum"] == checksum
            and _json_dict(existing["properties_json"]).get("package_checksum")
            == sources.manifest.get("package_checksum"),
            "mesh_version",
            ids["model_id"],
        )
        return 0
    cursor.execute(
        """
        INSERT INTO core.mesh_version (
            mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            ids["mesh_version_id"],
            ids["basin_version_id"],
            _version_label(sources),
            mesh_uri,
            checksum,
            _json(properties),
        ),
    )
    return 1


def _ensure_model_instance(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    resource_profile = _resource_profile(sources)
    existing = _fetch_optional(
        cursor,
        """
        SELECT basin_version_id,
               river_network_version_id,
               mesh_version_id,
               model_package_uri,
               active_flag,
               resource_profile
        FROM core.model_instance
        WHERE model_id = %s
        """,
        (ids["model_id"],),
    )
    if existing is not None:
        existing_profile = _json_dict(existing["resource_profile"])
        _require_existing(
            existing["basin_version_id"] == ids["basin_version_id"]
            and existing["river_network_version_id"] == ids["river_network_version_id"]
            and existing["mesh_version_id"] == ids["mesh_version_id"]
            and existing["model_package_uri"] == sources.manifest["model_package_uri"]
            and existing_profile.get("package_checksum") == sources.manifest.get("package_checksum")
            and existing_profile.get("source_inventory_checksum") == sources.manifest.get("source_inventory_checksum"),
            "model_instance",
            ids["model_id"],
        )
        return 0
    cursor.execute(
        """
        INSERT INTO core.model_instance (
            model_id, basin_version_id, river_network_version_id, mesh_version_id,
            calibration_version_id, shud_code_version, model_package_uri, active_flag, resource_profile
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, false, %s)
        """,
        (
            ids["model_id"],
            ids["basin_version_id"],
            ids["river_network_version_id"],
            ids["mesh_version_id"],
            f"{ids['model_id']}_calib_{_version_label(sources)}",
            "basins-shud",
            sources.manifest["model_package_uri"],
            _json(resource_profile),
        ),
    )
    return 1


def _resource_profile(sources: ImportSources) -> dict[str, Any]:
    return {
        "scheduler": "slurm",
        "partition": "standard",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": int(os.getenv("NHMS_BASINS_DEFAULT_CPUS", "4")),
        "memory_mb": int(os.getenv("NHMS_BASINS_DEFAULT_MEMORY_MB", "8192")),
        "walltime_minutes": int(os.getenv("NHMS_BASINS_DEFAULT_WALLTIME_MINUTES", "720")),
        "lineage": "basins_registry_import",
        "basin_slug": sources.model.get("basin_slug"),
        "shud_input_name": sources.model.get("shud_input_name"),
        "manifest_uri": sources.manifest.get("manifest_uri"),
        "package_checksum": sources.manifest.get("package_checksum"),
        "source_inventory_checksum": sources.manifest.get("source_inventory_checksum"),
        "source_inventory_schema_version": sources.manifest.get("source_inventory_schema_version"),
        "source_path": sources.model.get("source_path"),
        "resolved_source_path": sources.model.get("resolved_source_path"),
        "source_is_symlink": bool(sources.model.get("source_is_symlink", False)),
        "segment_count": sources.geometry.segment_count,
        "shud_evidence_counts": sources.geometry.evidence_counts,
    }


def _require_existing(condition: bool, resource: str, model_id: str) -> None:
    if not condition:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            f"Existing {resource} row does not match incoming Basins package/source checksums.",
            model_id=model_id,
            details={"resource": resource},
        )


def _find_inventory_model(inventory: dict[str, Any], model_id: str) -> dict[str, Any]:
    models = inventory.get("models")
    if not isinstance(models, list):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory JSON must contain a models array.",
            model_id=model_id,
        )
    matches = [model for model in models if isinstance(model, dict) and model.get("model_id") == model_id]
    if len(matches) > 1:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_MODEL_ID_DUPLICATE",
            "Basins inventory contains duplicate records for model_id.",
            model_id=model_id,
        )
    if not matches:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_MODEL_NOT_FOUND",
            "Basins model_id was not found in inventory.",
            model_id=model_id,
        )
    return matches[0]


def _inventory_root(inventory: dict[str, Any], model_id: str) -> Path:
    value = inventory.get("resolved_root")
    if not isinstance(value, str) or not value:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory is missing resolved_root.",
            model_id=model_id,
        )
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            "Basins inventory resolved_root does not exist.",
            model_id=model_id,
            path=str(path),
        )
    return path


def _source_root(inventory_root: Path, model: dict[str, Any], model_id: str) -> Path:
    relative = _safe_relative(_required_model_str(model, "root_relative_resolved_path", model_id), model_id)
    source_root = (inventory_root / relative).resolve()
    _ensure_under_root(source_root, inventory_root, model_id)
    recorded = Path(_required_model_str(model, "resolved_source_path", model_id)).expanduser().resolve()
    if recorded != source_root:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory resolved_source_path does not match root_relative_resolved_path.",
            model_id=model_id,
            path=str(recorded),
        )
    if not source_root.is_dir():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            "Basins source directory does not exist.",
            model_id=model_id,
            path=str(source_root),
        )
    return source_root


def _input_dir(source_root: Path, model: dict[str, Any], model_id: str) -> Path:
    shud_input_name = _required_model_str(model, "shud_input_name", model_id)
    expected = (source_root / "input" / shud_input_name).resolve()
    recorded = Path(_required_model_str(model, "input_dir", model_id)).expanduser().resolve()
    if recorded != expected:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory input_dir does not match canonical source root and shud_input_name.",
            model_id=model_id,
            path=str(recorded),
        )
    if not expected.is_dir():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            "Basins input directory does not exist.",
            model_id=model_id,
            path=str(expected),
        )
    return expected


def _registry_ids(model: dict[str, Any], model_id: str) -> dict[str, str]:
    suggested = model.get("suggested_ids")
    if not isinstance(suggested, dict):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory model record is missing suggested_ids.",
            model_id=model_id,
        )
    ids = {
        "basin_id": _required_mapping_str(suggested, "basin_id", model_id),
        "basin_version_id": _required_mapping_str(suggested, "basin_version_id", model_id),
        "river_network_version_id": _required_mapping_str(suggested, "river_network_version_id", model_id),
        "mesh_version_id": _required_mapping_str(suggested, "mesh_version_id", model_id),
        "model_id": model_id,
    }
    if suggested.get("model_id") not in (None, model_id):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins suggested model_id does not match package manifest model_id.",
            model_id=model_id,
        )
    return ids


def _read_json_object(path: str | Path, *, error_code: str, not_found_code: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
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
    return payload


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


def _model_active_state(database_url: str, model_id: str) -> bool:
    try:
        with _transaction(database_url) as cursor:
            row = _fetch_optional(
                cursor,
                "SELECT active_flag FROM core.model_instance WHERE model_id = %s",
                (model_id,),
            )
    except Exception:
        return False
    return bool(row and row["active_flag"])


@contextmanager
def _transaction(database_url: str) -> Any:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor, register_default_json, register_default_jsonb
    except ImportError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PSYCOPG_MISSING",
            "psycopg2 is required for Basins registry import.",
        ) from error
    connection = psycopg2.connect(database_url)
    connection.autocommit = False
    register_default_json(loads=json.loads, conn_or_curs=connection)
    register_default_jsonb(loads=json.loads, conn_or_curs=connection)
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
