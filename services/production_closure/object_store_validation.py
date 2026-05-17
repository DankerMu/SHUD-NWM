from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

from packages.common.object_store import LocalObjectStore, ObjectStoreError
from packages.common.redaction import redact_payload
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.basins_package import (
    BasinsPackageError,
    publish_basins_package,
    write_basins_migration_report,
)
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    prepare_basins_import_sources,
)
from workers.shud_runtime.runtime import SHUDRuntime, SHUDRuntimeConfig, SHUDRuntimeError

DEFAULT_BASINS_MIGRATION_SOURCE_URI = "/volume/data/nwm/Basins"
DEFAULT_OBJECT_STORE_TARGET = "local-production-like"
DEFAULT_CLEANUP_POLICY = "quarantine"
FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS = ("data/Basins", "/volume/")
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ProductionObjectStoreValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass
class EvidenceWriter:
    evidence_root: Path
    lane_dir: Path
    force: bool = False
    _created_paths: set[Path] = field(default_factory=set)

    def prepare(self) -> None:
        _refuse_symlink_components(self.evidence_root)
        _refuse_symlink_components(self.lane_dir.parent)
        if self.lane_dir.exists() or self.lane_dir.is_symlink():
            _refuse_symlink_components(self.lane_dir)
        resolved_lane = self.lane_dir.resolve(strict=False)
        try:
            resolved_lane.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under evidence root.",
            ) from error
        self.lane_dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, path: Path, payload: Any) -> None:
        self._write_bytes(path, json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n")

    def _write_bytes(self, path: Path, content: bytes) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to overwrite an existing run_id bundle.",
            )
        temp_path = safe_path.with_name(f".{safe_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(content)
            os.replace(temp_path, safe_path)
            self._created_paths.add(safe_path)
        except OSError as error:
            temp_path.unlink(missing_ok=True)
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
                f"Failed to write evidence file {safe_path}: {error}",
            ) from error

    def _safe_file_path(self, path: Path) -> Path:
        if path.is_symlink():
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
                f"Evidence file must not be a symlink: {path}",
            )
        _refuse_symlink_components(path.parent)
        resolved_parent = path.parent.resolve(strict=False)
        try:
            resolved_parent.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                "Evidence file path must stay under evidence root.",
            ) from error
        path.parent.mkdir(parents=True, exist_ok=True)
        return resolved_parent / path.name


@dataclass(frozen=True)
class ProductionObjectStoreConfig:
    evidence_root: Path
    run_id: str
    target: str
    endpoint: str
    object_store_root: Path
    object_store_prefix: str
    configured_object_store_prefix: str
    credential_source: str
    cleanup_policy: str
    basins_root: Path | None
    source_uri: str
    model_id: str | None
    version: str
    force: bool = False

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "object-store"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path,
        run_id: str | None,
        basins_root: Path | None = None,
        model_id: str | None = None,
        version: str | None = None,
        force: bool = False,
    ) -> ProductionObjectStoreConfig:
        resolved_evidence_root = _safe_resolved_evidence_root(evidence_root)
        resolved_run_id = _safe_run_id(run_id or datetime.now(UTC).strftime("m10-%Y%m%dT%H%M%SZ"))
        configured_root = (
            os.getenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT")
            or os.getenv("OBJECT_STORE_ROOT")
            or str(resolved_evidence_root / resolved_run_id / "object-store" / "local-object-store")
        )
        configured_prefix = (
            os.getenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX")
            or os.getenv("OBJECT_STORE_PREFIX")
            or f"s3://nhms-production-like/{resolved_run_id}"
        )
        root_from_env = os.getenv("NHMS_PRODUCTION_BASINS_ROOT", "").strip()
        resolved_basins_root = basins_root or (Path(root_from_env).expanduser() if root_from_env else None)
        return cls(
            evidence_root=resolved_evidence_root,
            run_id=resolved_run_id,
            target=os.getenv("NHMS_PRODUCTION_OBJECT_STORE_TARGET", DEFAULT_OBJECT_STORE_TARGET),
            endpoint=os.getenv("NHMS_PRODUCTION_OBJECT_STORE_ENDPOINT", ""),
            object_store_root=Path(configured_root).expanduser(),
            object_store_prefix=_operational_prefix(configured_prefix),
            configured_object_store_prefix=configured_prefix,
            credential_source=os.getenv("NHMS_PRODUCTION_OBJECT_STORE_CREDENTIAL_SOURCE", "none-local-fixture"),
            cleanup_policy=os.getenv("NHMS_PRODUCTION_OBJECT_STORE_CLEANUP_POLICY", DEFAULT_CLEANUP_POLICY),
            basins_root=resolved_basins_root,
            source_uri=os.getenv("NHMS_PRODUCTION_BASINS_SOURCE_URI", DEFAULT_BASINS_MIGRATION_SOURCE_URI),
            model_id=model_id or os.getenv("NHMS_PRODUCTION_BASINS_MODEL_ID") or None,
            version=version or os.getenv("NHMS_PRODUCTION_BASINS_VERSION", "vproduction-object-store-local"),
            force=force,
        )


@dataclass(frozen=True)
class PackageChecksumReconstruction:
    checksum: str | None
    status: str
    identity_basis: str
    limitation: str | None = None


def validate_object_store(config: ProductionObjectStoreConfig) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    _validate_config(config)
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()
    writer.write_json(config.lane_dir / "preflight.json", _preflight_payload(config))

    basins_root = config.basins_root or (config.lane_dir / "synthetic-basins")
    if config.basins_root is None:
        write_synthetic_basins_fixture(basins_root)

    blockers: list[dict[str, Any]] = []
    migration_report = _write_migration_evidence(config, writer, basins_root, blockers)
    if blockers:
        environment = _environment_payload(config)
        writer.write_json(config.lane_dir / "environment.json", environment)
        summary = _summary(
            config,
            status="blocked",
            blockers=blockers,
            files=["preflight.json", "migration_blocker.json", "environment.json"],
        )
        writer.write_json(config.lane_dir / "summary.json", summary)
        return summary

    inventory_path = config.lane_dir / ".inventory.raw.json"
    package_manifest_raw_path = config.lane_dir / ".package_manifest.raw.json"
    cleanup_raw_files = [inventory_path, package_manifest_raw_path]
    try:
        inventory = discover_basins_inventory(basins_root)
        write_inventory(inventory, inventory_path)
        selected_model_id = config.model_id or _default_model_id(inventory)
        store = LocalObjectStore(config.object_store_root, config.object_store_prefix)
        publish_result = publish_basins_package(
            inventory_path=inventory_path,
            model_id=selected_model_id,
            version=config.version,
            output_path=package_manifest_raw_path,
            copy_forcing=False,
            object_store=store,
        )
        manifest = json.loads(package_manifest_raw_path.read_text(encoding="utf-8"))
        writer.write_json(config.lane_dir / "package_manifest.json", manifest)
        package_evidence = _package_manifest_evidence(publish_result, manifest)
        writer.write_json(config.lane_dir / "package_manifest_evidence.json", package_evidence)

        stored_verification = _verify_stored_objects(store, manifest)
        writer.write_json(config.lane_dir / "stored_object_verification.json", stored_verification)

        consumption = _consumption_evidence(config, writer, store, inventory_path, package_manifest_raw_path, manifest)
        writer.write_json(config.lane_dir / "registry_api_runtime_consumption.json", consumption)

        cleanup = _cleanup_rollback_evidence(config, store, selected_model_id)
        writer.write_json(config.lane_dir / "cleanup_rollback.json", cleanup)

        environment = _environment_payload(config)
        writer.write_json(config.lane_dir / "environment.json", environment)

        blocker_codes = _result_blockers(stored_verification, consumption, cleanup)
        status = "ready" if not blocker_codes else "blocked"
        summary = _summary(
            config,
            status=status,
            blockers=blocker_codes,
            files=[
                "preflight.json",
                "migration_report.json",
                "package_manifest.json",
                "package_manifest_evidence.json",
                "stored_object_verification.json",
                "registry_api_runtime_consumption.json",
                "runtime_staging_manifest.json",
                "cleanup_rollback.json",
                "environment.json",
            ],
            selected_model_id=selected_model_id,
            version=config.version,
            migration_report=migration_report,
            package_manifest=manifest,
        )
        writer.write_json(config.lane_dir / "summary.json", summary)
        return summary
    finally:
        for path in cleanup_raw_files:
            path.unlink(missing_ok=True)


def write_synthetic_basins_fixture(root: Path) -> dict[str, Any]:
    input_name = "alias-a"
    model_dir = root / "basin-a"
    input_dir = model_dir / "input" / input_name
    input_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (
        "cfg.para",
        "cfg.ic",
        "cfg.calib",
        "sp.mesh",
        "sp.att",
        "para.soil",
        "para.geol",
        "para.lc",
        "tsd.forc",
        "tsd.lai",
        "tsd.mf",
        "tsd.rl",
    ):
        (input_dir / f"{input_name}.{suffix}").write_text(f"{suffix}\n", encoding="utf-8")
    (input_dir / f"{input_name}.sp.riv").write_text("2 6\n1 0 0 0.01 100 0\n", encoding="utf-8")
    (input_dir / f"{input_name}.sp.rivseg").write_text("2 4\n1 1 1 100\n", encoding="utf-8")
    gis_dir = input_dir / "gis"
    gis_dir.mkdir(exist_ok=True)
    _write_domain_shapefile(gis_dir / "domain")
    _write_line_shapefile(gis_dir / "river")
    _write_line_shapefile(gis_dir / "seg")
    forcing_dir = model_dir / "forcing"
    forcing_dir.mkdir(exist_ok=True)
    (forcing_dir / "X000001.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")
    return discover_basins_inventory(root)


def _write_domain_shapefile(base: Path) -> None:
    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("ID", "N")
    writer.poly([[[100.0, 30.0], [101.0, 30.0], [101.0, 31.0], [100.0, 31.0], [100.0, 30.0]]])
    writer.record(1)
    writer.close()
    _write_wgs84_prj(base.with_suffix(".prj"))


def _write_line_shapefile(base: Path) -> None:
    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYLINE)
    writer.field("SEG_ID", "N")
    writer.field("ORDER", "N")
    writer.field("DOWN_ID", "N")
    writer.field("LENGTH_M", "F", decimal=3)
    writer.line([[[100.1, 30.1], [100.5, 30.4]]])
    writer.record(1, 1, 2, 50000.0)
    writer.line([[[100.5, 30.4], [100.8, 30.8]]])
    writer.record(2, 2, 0, 60000.0)
    writer.close()
    _write_wgs84_prj(base.with_suffix(".prj"))


def _write_wgs84_prj(path: Path) -> None:
    path.write_text(
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]\n',
        encoding="utf-8",
    )


def _write_migration_evidence(
    config: ProductionObjectStoreConfig,
    writer: EvidenceWriter,
    basins_root: Path,
    blockers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    raw_path = config.lane_dir / ".migration_report.raw.json"
    try:
        report = write_basins_migration_report(
            basins_root=basins_root,
            source_uri=config.source_uri,
            output_path=raw_path,
        )
    except BasinsPackageError as error:
        blocker = error.to_payload()
        blocker["status"] = "blocked"
        blockers.append(blocker)
        writer.write_json(config.lane_dir / "migration_blocker.json", blocker)
        return None
    finally:
        raw_path.unlink(missing_ok=True)
    writer.write_json(config.lane_dir / "migration_report.json", report)
    return report


def _package_manifest_evidence(publish_result: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.object_store.package_manifest.v1",
        "status": publish_result["status"],
        "model_id": manifest["model_id"],
        "version": publish_result["version"],
        "manifest_uri": manifest["manifest_uri"],
        "model_package_uri": manifest["model_package_uri"],
        "package_checksum": manifest["package_checksum"],
        "included_file_count": len(manifest.get("included_files", [])),
        "manifest_included": any(
            isinstance(entry, dict) and entry.get("role") == "manifest"
            for entry in manifest.get("included_files", [])
        ),
        "source_is_symlink": manifest.get("source_is_symlink"),
    }


def _verify_stored_objects(store: LocalObjectStore, manifest: dict[str, Any]) -> dict[str, Any]:
    stored_manifest_bytes = store.read_bytes(str(manifest["manifest_uri"]))
    stored_manifest = json.loads(stored_manifest_bytes.decode("utf-8"))
    stored_manifest_sha256 = hashlib.sha256(stored_manifest_bytes).hexdigest()
    package_checksum_reconstruction = _package_checksum_from_stored_manifest(stored_manifest)
    package_checksum_verified = (
        package_checksum_reconstruction.checksum
        == stored_manifest.get("package_checksum")
        == manifest.get("package_checksum")
    )
    package_checksum_matches_manifest = stored_manifest.get("package_checksum") == manifest.get("package_checksum")
    entries = []
    all_verified = package_checksum_verified
    for entry in stored_manifest.get("included_files", []):
        if not isinstance(entry, dict):
            continue
        object_uri = str(entry["object_uri"])
        content = store.read_bytes(object_uri)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        actual_size = len(content)
        expected_sha256 = entry["sha256"]
        manifest_payload_sha256 = None
        final_manifest_sha256 = None
        if entry.get("role") == "manifest":
            manifest_payload = _stored_manifest_payload_without_self_entry(stored_manifest)
            manifest_payload_sha256 = hashlib.sha256(_deterministic_manifest_bytes(manifest_payload)).hexdigest()
            final_manifest_sha256 = actual_sha256
            actual_sha256 = manifest_payload_sha256
        verified = actual_sha256 == expected_sha256 and actual_size == entry["size_bytes"]
        all_verified = all_verified and verified
        entries.append(
            {
                "relative_path": entry["relative_path"],
                "role": entry["role"],
                "object_uri": object_uri,
                "expected_size_bytes": entry["size_bytes"],
                "actual_size_bytes": actual_size,
                "expected_sha256": expected_sha256,
                "manifest_recorded_sha256": entry["sha256"] if entry.get("role") == "manifest" else None,
                "actual_sha256": actual_sha256,
                "manifest_payload_sha256": manifest_payload_sha256,
                "final_manifest_sha256": final_manifest_sha256,
                "verified": verified,
            }
        )
    return {
        "schema": "nhms.production_closure.object_store.stored_object_verification.v1",
        "status": "verified" if all_verified else "blocked",
        "manifest_uri": manifest["manifest_uri"],
        "model_package_uri": manifest["model_package_uri"],
        "package_checksum": manifest["package_checksum"],
        "package_checksum_confirmed_from_stored_manifest": package_checksum_verified,
        "package_checksum_matches_manifest": package_checksum_matches_manifest,
        "package_checksum_reconstruction_status": package_checksum_reconstruction.status,
        "package_checksum_source_model_identity_basis": package_checksum_reconstruction.identity_basis,
        "package_checksum_reconstruction_limitation": package_checksum_reconstruction.limitation,
        "stored_manifest_package_checksum": stored_manifest.get("package_checksum"),
        "recomputed_package_checksum": package_checksum_reconstruction.checksum,
        "stored_manifest_sha256": stored_manifest_sha256,
        "entry_count": len(entries),
        "entries": entries,
    }


def _stored_manifest_payload_without_self_entry(stored_manifest: dict[str, Any]) -> dict[str, Any]:
    payload = dict(stored_manifest)
    payload["included_files"] = [
        entry
        for entry in stored_manifest.get("included_files", [])
        if isinstance(entry, dict) and entry.get("role") != "manifest"
    ]
    return payload


def _package_checksum_from_stored_manifest(stored_manifest: dict[str, Any]) -> PackageChecksumReconstruction:
    source_model_identity = _source_model_identity_for_package_checksum(stored_manifest)
    if source_model_identity["identity"]["root_relative_resolved_path"] is None:
        return PackageChecksumReconstruction(
            checksum=None,
            status="limited",
            identity_basis=str(source_model_identity["basis"]),
            limitation="stored_manifest_does_not_prove_root_relative_resolved_path",
        )
    included_files = [
        {
            "relative_path": entry["relative_path"],
            "role": entry["role"],
            "size_bytes": entry["size_bytes"],
            "sha256": entry["sha256"],
        }
        for entry in stored_manifest.get("included_files", [])
        if isinstance(entry, dict) and entry.get("role") != "manifest"
    ]
    checksum_material = {
        "schema_version": stored_manifest.get("schema_version"),
        "model_id": stored_manifest.get("model_id"),
        "version": stored_manifest.get("version"),
        "included_files": sorted(included_files, key=lambda item: (item["role"], item["relative_path"])),
        "forcing": _forcing_checksum_material(stored_manifest.get("forcing")),
        "copy_forcing": bool(stored_manifest.get("forcing", {}).get("payload_copied", False))
        if isinstance(stored_manifest.get("forcing"), dict)
        else False,
        "source_model_identity": source_model_identity["identity"],
    }
    return PackageChecksumReconstruction(
        checksum=_sha256_json(checksum_material),
        status="confirmed",
        identity_basis=str(source_model_identity["basis"]),
    )


def _source_model_identity_for_package_checksum(stored_manifest: dict[str, Any]) -> dict[str, Any]:
    basin_slug = stored_manifest.get("basin_slug")
    shud_input_name = stored_manifest.get("shud_input_name")
    root_relative = stored_manifest.get("root_relative_resolved_path")
    if isinstance(root_relative, str) and root_relative:
        return {
            "basis": "stored_manifest.root_relative_resolved_path",
            "identity": {
                "basin_slug": basin_slug,
                "shud_input_name": shud_input_name,
                "root_relative_resolved_path": root_relative,
            },
        }

    inferred_root_relative = _infer_copied_root_relative_resolved_path(stored_manifest)
    if inferred_root_relative is not None:
        return {
            "basis": "documented_148_copied_root_non_symlink_source_suffix",
            "identity": {
                "basin_slug": basin_slug,
                "shud_input_name": shud_input_name,
                "root_relative_resolved_path": inferred_root_relative,
            },
        }

    return {
        "basis": "unavailable",
        "identity": {
            "basin_slug": basin_slug,
            "shud_input_name": shud_input_name,
            "root_relative_resolved_path": None,
        },
    }


def _infer_copied_root_relative_resolved_path(stored_manifest: dict[str, Any]) -> str | None:
    """Infer only the documented #148 copied-root case.

    Basins discovery sets root_relative_resolved_path equal to the basin slug when
    a non-symlink copied root is scanned and the source/resolved model paths both
    end with the basin slug. Without those manifest facts, the package checksum is
    intentionally left unconfirmed instead of treating basin_slug as that field.
    """
    if stored_manifest.get("source_is_symlink") is not False:
        return None
    basin_slug = stored_manifest.get("basin_slug")
    source_path = stored_manifest.get("source_path")
    resolved_source_path = stored_manifest.get("resolved_source_path")
    if not all(isinstance(value, str) and value for value in (basin_slug, source_path, resolved_source_path)):
        return None
    basin_parts = PurePosixPath(str(basin_slug)).parts
    if not basin_parts or any(part in {"", ".", ".."} for part in basin_parts):
        return None
    if PurePosixPath(str(basin_slug)).is_absolute():
        return None
    source_parts = Path(str(source_path)).parts
    resolved_parts = Path(str(resolved_source_path)).parts
    if tuple(source_parts[-len(basin_parts) :]) != basin_parts:
        return None
    if tuple(resolved_parts[-len(basin_parts) :]) != basin_parts:
        return None
    return str(basin_slug)


def _forcing_checksum_material(forcing: Any) -> Any:
    if not isinstance(forcing, dict):
        return forcing
    return {
        "policy": forcing.get("policy"),
        "csv_count": forcing.get("csv_count"),
        "byte_count": forcing.get("byte_count"),
        "aggregate_checksum": forcing.get("aggregate_checksum"),
        "payload_copied": forcing.get("payload_copied"),
        "copied_file_count": forcing.get("copied_file_count"),
        "copied_byte_count": forcing.get("copied_byte_count"),
    }


def _deterministic_manifest_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _consumption_evidence(
    config: ProductionObjectStoreConfig,
    writer: EvidenceWriter,
    store: LocalObjectStore,
    inventory_path: Path,
    package_manifest_raw_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    registry: dict[str, Any]
    try:
        sources = prepare_basins_import_sources(
            inventory_path=inventory_path,
            package_manifest_path=package_manifest_raw_path,
        )
        registry = {
            "status": "local_sources_prepared",
            "db_import_status": "not_executed",
            "db_import_reason": (
                "fast lane does not require PostgreSQL/PostGIS; geometry and manifest contracts were validated locally."
            ),
            "model_id": sources.ids["model_id"],
            "basin_id": sources.ids["basin_id"],
            "basin_version_id": sources.ids["basin_version_id"],
            "river_network_version_id": sources.ids["river_network_version_id"],
            "mesh_version_id": sources.ids["mesh_version_id"],
            "segment_count": sources.geometry.segment_count,
            "active": False,
            "implicit_activation": False,
            "model_package_uri": manifest["model_package_uri"],
            "package_checksum": manifest["package_checksum"],
        }
    except BasinsRegistryImportError as error:
        registry = {"status": "blocked", **error.to_payload(), "implicit_activation": False}

    runtime = _runtime_staging_evidence(config, store, manifest, writer)
    api = {
        "status": "local_contract",
        "live_api_status": "not_executed",
        "live_api_reason": "fast lane does not require a running API or registry database.",
        "model_response_fixture": {
            "model_id": manifest["model_id"],
            "active": False,
            "model_package_uri": manifest["model_package_uri"],
            "manifest_uri": manifest["manifest_uri"],
            "package_checksum": manifest["package_checksum"],
        },
    }
    runtime_source_values = [
        manifest.get("model_package_uri"),
        runtime.get("runtime_manifest", {}).get("model_package_uri"),
        api["model_response_fixture"]["model_package_uri"],
    ]
    forbidden = _forbidden_runtime_source_fragments(runtime_source_values)
    prefix_ok = all(
        isinstance(value, str) and value.startswith(config.object_store_prefix.rstrip("/") + "/")
        for value in runtime_source_values
        if value
    )
    consumption_ready = (
        registry.get("status") != "blocked"
        and runtime.get("status") == "prepared"
        and prefix_ok
        and not forbidden
    )
    return {
        "schema": "nhms.production_closure.object_store.consumption.v1",
        "status": "ready" if consumption_ready else "blocked",
        "registry": registry,
        "api": api,
        "runtime": runtime,
        "object_uri_prefix": config.object_store_prefix,
        "uses_object_uri_prefix": prefix_ok,
        "forbidden_runtime_source_fragments": forbidden,
        "runtime_dev_path_leak": bool(forbidden),
        "implicit_activation": False,
    }


def _runtime_staging_evidence(
    config: ProductionObjectStoreConfig,
    store: LocalObjectStore,
    manifest: dict[str, Any],
    writer: EvidenceWriter,
) -> dict[str, Any]:
    forcing_key = f"forcing/gfs/2026051600/basin_v1/{manifest['model_id']}/forcing.tsd.forc"
    store.write_bytes_atomic(forcing_key, b"forcing\n")
    runtime_manifest = {
        "run_id": f"{config.run_id}_runtime_staging",
        "run_type": "forecast",
        "scenario_id": "production_object_store_validation",
        "source_id": "GFS",
        "cycle_time": "2026-05-16T00:00:00Z",
        "start_time": "2026-05-16T00:00:00Z",
        "end_time": "2026-05-17T00:00:00Z",
        "model": {
            "model_id": manifest["model_id"],
            "basin_version_id": "basin_v1",
            "model_package_uri": manifest["model_package_uri"],
            "project_name": manifest.get("shud_input_name") or manifest["model_id"],
            "segment_count": 2,
        },
        "initial_state": {"state_id": None, "ic_file_uri": None},
        "forcing": {
            "forcing_version_id": "forc_gfs_2026051600",
            "forcing_uri": store.uri_for_key(forcing_key.rsplit("/", maxsplit=1)[0] + "/"),
        },
        "runtime": {"output_interval_minutes": 1440},
        "outputs": {
            "run_manifest_uri": store.uri_for_key(f"runs/{config.run_id}_runtime_staging/input/manifest.json"),
            "output_uri": store.uri_for_key(f"runs/{config.run_id}_runtime_staging/output/"),
            "log_uri": store.uri_for_key(f"runs/{config.run_id}_runtime_staging/logs/"),
        },
    }
    runtime_config = SHUDRuntimeConfig(
        workspace_root=config.lane_dir / "runtime-workspace",
        object_store_root=config.object_store_root,
        object_store_prefix=config.object_store_prefix,
        shud_executable="/bin/false",
        output_interval_minutes=1440,
        dry_run=True,
    )
    runtime = SHUDRuntime(
        config=runtime_config,
        object_store=store,
    )
    input_dir = config.lane_dir / "runtime-workspace" / "runs" / runtime_manifest["run_id"] / "input"
    output_dir = config.lane_dir / "runtime-workspace" / "runs" / runtime_manifest["run_id"] / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        runtime.prepare_workspace(runtime_manifest, input_dir)
        cfg_path = runtime.generate_cfg_para(runtime_manifest, input_dir, output_dir)
    except SHUDRuntimeError as error:
        return {
            "status": "blocked",
            "error_code": error.error_code,
            "message": error.message,
            "runtime_manifest": {
                "model_package_uri": runtime_manifest["model"]["model_package_uri"],
                "forcing_uri": runtime_manifest["forcing"]["forcing_uri"],
            },
        }
    staged_files = sorted(path.relative_to(input_dir).as_posix() for path in input_dir.rglob("*") if path.is_file())
    evidence = {
        "status": "prepared",
        "execution_status": "not_executed",
        "execution_reason": (
            "fast lane verifies object-URI staging and cfg generation without running a live SHUD solver."
        ),
        "runtime_manifest": {
            "model_package_uri": runtime_manifest["model"]["model_package_uri"],
            "forcing_uri": runtime_manifest["forcing"]["forcing_uri"],
            "run_manifest_uri": runtime_manifest["outputs"]["run_manifest_uri"],
        },
        "staged_file_count": len(staged_files),
        "staged_files": staged_files,
        "generated_cfg_path": str(cfg_path),
    }
    writer.write_json(config.lane_dir / "runtime_staging_manifest.json", runtime_manifest)
    return evidence


def _cleanup_rollback_evidence(
    config: ProductionObjectStoreConfig,
    store: LocalObjectStore,
    model_id: str,
) -> dict[str, Any]:
    partial_key = f"models/{model_id}/{config.version}-failed-import/partial-package.bin"
    store.write_bytes_atomic(partial_key, b"partial object written before simulated import failure\n")
    written_keys = [partial_key]
    rows = [
        {
            "table": "core.model_instance",
            "natural_key": f"{model_id}:{config.version}-failed-import",
            "status": "simulated_not_committed",
        }
    ]
    cleanup_status = "retained"
    quarantine_key = None
    if config.cleanup_policy == "delete":
        store.delete(partial_key)
        cleanup_status = "deleted"
    elif config.cleanup_policy == "quarantine":
        quarantine_key = f"runs/{config.run_id}/logs/quarantine/{partial_key}"
        content = store.read_bytes(partial_key)
        store.write_bytes_atomic(quarantine_key, content)
        store.delete(partial_key)
        cleanup_status = "quarantined"
    partial_exists_after = store.exists(partial_key)
    return {
        "schema": "nhms.production_closure.object_store.cleanup_rollback.v1",
        "status": "ready",
        "simulated_failure": {
            "stage": "registry_import",
            "error_code": "SIMULATED_REGISTRY_IMPORT_FAILURE",
            "message": (
                "Synthetic failure after partial object write exercises rollback evidence "
                "without touching a live database."
            ),
        },
        "written_object_keys": written_keys,
        "written_db_rows": rows,
        "cleanup_policy": config.cleanup_policy,
        "cleanup_status": cleanup_status,
        "quarantine_key": quarantine_key,
        "partial_objects_remaining": [partial_key] if partial_exists_after else [],
        "implicit_model_activation": False,
        "active_model_state": "unchanged",
    }


def _preflight_payload(config: ProductionObjectStoreConfig) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.object_store.preflight.v1",
        "run_id": config.run_id,
        "target": config.target,
        "endpoint": config.endpoint,
        "object_store_root": str(config.object_store_root),
        "object_store_prefix": config.configured_object_store_prefix,
        "operational_object_store_prefix": config.object_store_prefix,
        "credential_source": config.credential_source,
        "cleanup_policy": config.cleanup_policy,
        "copied_basins_root": str(config.basins_root) if config.basins_root else "synthetic-local-fixture",
        "source_uri": config.source_uri,
        "selected_model": config.model_id or "first-valid-model",
        "version": config.version,
        "evidence_root": str(config.evidence_root),
    }


def _environment_payload(config: ProductionObjectStoreConfig) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.object_store.environment.v1",
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": str(Path.cwd()),
        "run_production_closure": os.getenv("NHMS_RUN_PRODUCTION_CLOSURE", ""),
        "target": config.target,
        "object_store_prefix": config.object_store_prefix,
    }


def _summary(
    config: ProductionObjectStoreConfig,
    *,
    status: str,
    blockers: list[dict[str, Any]],
    files: list[str],
    selected_model_id: str | None = None,
    version: str | None = None,
    migration_report: dict[str, Any] | None = None,
    package_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "nhms.production_closure.object_store.v1",
        "issue": 148,
        "run_id": config.run_id,
        "status": status,
        "evidence_dir": str(config.lane_dir),
        "target": config.target,
        "object_store_prefix": config.object_store_prefix,
        "blockers": blockers,
        "files": [*files, "summary.json"],
    }
    if selected_model_id is not None:
        payload["model_id"] = selected_model_id
    if version is not None:
        payload["version"] = version
    if migration_report is not None:
        payload["migration_production_ready"] = migration_report.get("production_ready")
        payload["migration_inventory_checksum"] = migration_report.get("inventory_checksum")
    if package_manifest is not None:
        payload["manifest_uri"] = package_manifest.get("manifest_uri")
        payload["model_package_uri"] = package_manifest.get("model_package_uri")
        payload["package_checksum"] = package_manifest.get("package_checksum")
    return payload


def _result_blockers(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    blockers = []
    for payload in payloads:
        if payload.get("status") not in {"ready", "verified"}:
            blockers.append(
                {
                    "error_code": "PRODUCTION_OBJECT_STORE_VALIDATION_BLOCKED",
                    "schema": payload.get("schema"),
                    "status": payload.get("status"),
                }
            )
    return blockers


def _default_model_id(inventory: dict[str, Any]) -> str:
    for model in inventory.get("models", []):
        if isinstance(model, dict) and model.get("status") == "valid" and model.get("default_publish_eligible") is True:
            return str(model["model_id"])
    raise ProductionObjectStoreValidationError(
        "PRODUCTION_OBJECT_STORE_NO_PUBLISHABLE_MODEL",
        "Basins inventory does not contain a valid publishable model.",
    )


def _forbidden_runtime_source_fragments(values: Sequence[Any]) -> list[str]:
    found: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        for fragment in FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS:
            if fragment in value:
                found.add(fragment)
    return sorted(found)


def _validate_config(config: ProductionObjectStoreConfig) -> None:
    if config.target not in {"s3", "minio", "local-production-like"}:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_TARGET_INVALID",
            "Object-store target must be one of: s3, minio, local-production-like.",
        )
    if config.cleanup_policy not in {"delete", "quarantine", "retain"}:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_CLEANUP_POLICY_INVALID",
            "Cleanup policy must be one of: delete, quarantine, retain.",
        )
    if not config.version:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VERSION_MISSING",
            "Basins package version must not be empty.",
        )
    if config.object_store_prefix and "://" not in config.object_store_prefix:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_INVALID",
            "Object-store prefix must be an object URI prefix such as s3://bucket/prefix.",
        )


def _operational_prefix(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _safe_run_id(run_id: str) -> str:
    if SAFE_RUN_ID_RE.fullmatch(run_id):
        return run_id
    raise ProductionObjectStoreValidationError(
        "PRODUCTION_OBJECT_STORE_RUN_ID_UNSAFE",
        "run_id may contain only alphanumeric characters, underscores, and hyphens.",
    )


def _safe_resolved_evidence_root(evidence_root: Path) -> Path:
    root = evidence_root.expanduser()
    if root.exists() or root.is_symlink():
        _refuse_symlink_components(root)
    parent = root.parent
    if parent.exists() or parent.is_symlink():
        _refuse_symlink_components(parent)
    return root.resolve(strict=False)


def _refuse_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("validate-object-store")
    @click.option("--evidence-root", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id")
    @click.option("--basins-root", type=click.Path(path_type=Path), default=None)
    @click.option("--model-id", default=None)
    @click.option("--version", default=None)
    @click.option("--force", is_flag=True, default=False)
    def validate_object_store_command(
        evidence_root: Path,
        run_id: str | None,
        basins_root: Path | None,
        model_id: str | None,
        version: str | None,
        force: bool,
    ) -> None:
        try:
            summary = validate_object_store(
                ProductionObjectStoreConfig.from_env(
                    evidence_root=evidence_root,
                    run_id=run_id,
                    basins_root=basins_root,
                    model_id=model_id,
                    version=version,
                    force=force,
                )
            )
            click.echo(json.dumps(summary, sort_keys=True))
        except (
            ProductionObjectStoreValidationError,
            BasinsPackageError,
            BasinsRegistryImportError,
            ObjectStoreError,
            OSError,
            ValueError,
        ) as error:
            if isinstance(error, ProductionObjectStoreValidationError):
                click.echo(f"{error.error_code}: {error.message}", err=True)
            elif hasattr(error, "to_payload"):
                click.echo(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), err=True)
            else:
                click.echo(f"PRODUCTION_OBJECT_STORE_VALIDATION_FAILED: {error}", err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=False)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-production")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-object-store")
    validate_parser.add_argument("--evidence-root", type=Path, required=True)
    validate_parser.add_argument("--run-id")
    validate_parser.add_argument("--basins-root", type=Path, default=None)
    validate_parser.add_argument("--model-id", default=None)
    validate_parser.add_argument("--version", default=None)
    validate_parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate-object-store":
        try:
            print(
                json.dumps(
                    validate_object_store(
                        ProductionObjectStoreConfig.from_env(
                            evidence_root=args.evidence_root,
                            run_id=args.run_id,
                            basins_root=args.basins_root,
                            model_id=args.model_id,
                            version=args.version,
                            force=args.force,
                        )
                    ),
                    sort_keys=True,
                )
            )
        except (
            ProductionObjectStoreValidationError,
            BasinsPackageError,
            BasinsRegistryImportError,
            ObjectStoreError,
            OSError,
            ValueError,
        ) as error:
            if isinstance(error, ProductionObjectStoreValidationError):
                print(f"{error.error_code}: {error.message}", file=sys.stderr)
            elif hasattr(error, "to_payload"):
                print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
            else:
                print(f"PRODUCTION_OBJECT_STORE_VALIDATION_FAILED: {error}", file=sys.stderr)
            return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
