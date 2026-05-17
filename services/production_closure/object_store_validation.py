from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import tempfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

from packages.common.object_store import MAX_OBJECT_MANIFEST_BYTES, LocalObjectStore, ObjectStoreError
from packages.common.redaction import redact_payload
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    unlink_no_follow,
)
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.basins_package import (
    BasinsPackageError,
    publish_basins_package,
    write_basins_migration_report,
)
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    import_basins_registry,
    prepare_basins_import_sources,
)
from workers.shud_runtime.runtime import SHUDRuntime, SHUDRuntimeConfig, SHUDRuntimeError

DEFAULT_BASINS_MIGRATION_SOURCE_URI = "/volume/data/nwm/Basins"
DEFAULT_OBJECT_STORE_TARGET = "local-production-like"
DEFAULT_CLEANUP_POLICY = "quarantine"
FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS = ("data/Basins", "/volume/")
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
MAX_PERCENT_DECODE_ROUNDS = 4
ENCODED_SEPARATOR_RE = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
MAX_STORED_MANIFEST_BYTES = MAX_OBJECT_MANIFEST_BYTES
MAX_RAW_INTERMEDIATE_BYTES = 64 * 1024 * 1024
SENSITIVE_PREFIX_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;?#&])[^=/?#;&]*(?:token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|"
    r"session[_-]?key|signature|x-amz-signature)[^=/?#;&]*=",
    re.IGNORECASE,
)
SENSITIVE_PREFIX_SEPARATOR_RE = re.compile(r"[/;?#&]")


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
            if not self.lane_dir.is_dir():
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                    f"Evidence lane path must be a directory: {self.lane_dir}.",
                )
        resolved_lane = self.lane_dir.resolve(strict=False)
        try:
            resolved_lane.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under evidence root.",
            ) from error
        try:
            ensure_directory_no_follow(self.evidence_root)
            ensure_directory_no_follow(self.lane_dir, containment_root=self.evidence_root)
        except SafeFilesystemError as error:
            error_code = (
                "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
            )
            raise ProductionObjectStoreValidationError(
                error_code,
                f"Failed to prepare evidence lane {self.lane_dir}: {error}",
            ) from error

    def write_json(self, path: Path, payload: Any) -> None:
        self._write_bytes(path, json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n")

    def _write_bytes(self, path: Path, content: bytes) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to overwrite an existing run_id bundle.",
            )
        try:
            atomic_write_bytes_no_follow(safe_path, content, containment_root=self.evidence_root)
            self._created_paths.add(safe_path)
        except SafeFilesystemError as error:
            error_code = (
                "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
            )
            raise ProductionObjectStoreValidationError(
                error_code,
                f"Failed to write evidence file {safe_path}: {error}",
            ) from error
        except OSError as error:
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
        try:
            ensure_directory_no_follow(path.parent, containment_root=self.evidence_root)
        except SafeFilesystemError as error:
            error_code = (
                "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
            )
            raise ProductionObjectStoreValidationError(
                error_code,
                f"Failed to prepare evidence file parent {path.parent}: {error}",
            ) from error
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
    run_registry_import: bool = False
    registry_database_url: str | None = None
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
        registry_database_url = (
            os.getenv("NHMS_PRODUCTION_OBJECT_STORE_REGISTRY_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
        ).strip()
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
            run_registry_import=_truthy_env(os.getenv("NHMS_PRODUCTION_OBJECT_STORE_RUN_REGISTRY_IMPORT")),
            registry_database_url=registry_database_url or None,
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
    _validate_internal_lane_paths(config)
    writer.write_json(config.lane_dir / "preflight.json", _preflight_payload(config))

    basins_root = config.basins_root or (config.lane_dir / "synthetic-basins")
    if config.basins_root is None:
        _validate_lane_path_contained(config, basins_root, path_kind="synthetic basins fixture")
        _refuse_existing_descendant_symlinks(basins_root, path_kind="synthetic basins fixture")
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
        _write_raw_worker_output(
            config,
            inventory_path,
            path_kind="raw inventory file",
            producer=lambda output_path: write_inventory(inventory, output_path),
        )
        selected_model_id = config.model_id or _default_model_id(inventory)
        _validate_local_object_store_root(config)
        store = LocalObjectStore(config.object_store_root, config.object_store_prefix)
        publish_result, package_manifest_bytes = _write_raw_worker_output(
            config,
            package_manifest_raw_path,
            path_kind="raw package manifest file",
            producer=lambda output_path: publish_basins_package(
                inventory_path=inventory_path,
                model_id=selected_model_id,
                version=config.version,
                output_path=output_path,
                copy_forcing=False,
                object_store=store,
            ),
        )
        manifest = json.loads(package_manifest_bytes.decode("utf-8"))
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
            consumption=consumption,
        )
        writer.write_json(config.lane_dir / "summary.json", summary)
        return summary
    finally:
        for path in cleanup_raw_files:
            _cleanup_raw_lane_file(config, path, path_kind="raw cleanup file")


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
        report, _report_bytes = _write_raw_worker_output(
            config,
            raw_path,
            path_kind="raw migration report file",
            producer=lambda output_path: write_basins_migration_report(
                basins_root=basins_root,
                source_uri=config.source_uri,
                output_path=output_path,
            ),
        )
    except BasinsPackageError as error:
        blocker = error.to_payload()
        blocker["status"] = "blocked"
        blockers.append(blocker)
        writer.write_json(config.lane_dir / "migration_blocker.json", blocker)
        return None
    finally:
        _cleanup_raw_lane_file(config, raw_path, path_kind="raw migration report file")
    writer.write_json(config.lane_dir / "migration_report.json", report)
    return report


def _write_raw_worker_output(
    config: ProductionObjectStoreConfig,
    raw_path: Path,
    *,
    path_kind: str,
    producer: Callable[[Path], Any],
) -> tuple[Any, bytes]:
    _validate_lane_path_contained(config, raw_path, path_kind=path_kind)
    with tempfile.TemporaryDirectory(prefix="nhms-object-store-validation-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        temp_path = temp_dir / raw_path.name
        producer_succeeded = False
        try:
            result = producer(temp_path)
            content = _read_raw_worker_output(temp_path, path_kind=path_kind)
            producer_succeeded = True
        finally:
            try:
                unlink_no_follow(temp_path, containment_root=temp_dir, missing_ok=True)
            except (OSError, SafeFilesystemError) as error:
                if producer_succeeded:
                    raise ProductionObjectStoreValidationError(
                        "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
                        f"Failed to safely remove temporary {path_kind} {temp_path}: {error}",
                    ) from error
    _write_raw_lane_bytes(config, raw_path, content, path_kind=path_kind)
    return result, content


def _read_raw_worker_output(path: Path, *, path_kind: str) -> bytes:
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_RAW_INTERMEDIATE_BYTES + 1)
    except OSError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
            f"Failed to read temporary {path_kind} {path}: {error}",
        ) from error
    if len(content) > MAX_RAW_INTERMEDIATE_BYTES:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
            f"Temporary {path_kind} exceeds {MAX_RAW_INTERMEDIATE_BYTES} bytes: {path}",
        )
    return content


def _write_raw_lane_bytes(
    config: ProductionObjectStoreConfig,
    raw_path: Path,
    content: bytes,
    *,
    path_kind: str,
) -> None:
    _validate_lane_path_contained(config, raw_path, path_kind=path_kind)
    try:
        atomic_write_bytes_no_follow(raw_path, content, containment_root=config.lane_dir)
    except SafeFilesystemError as error:
        error_code = (
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
            if error.kind == "io"
            else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
        )
        raise ProductionObjectStoreValidationError(
            error_code,
            f"Failed to safely write {path_kind} {raw_path}: {error}",
        ) from error
    except OSError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
            f"Failed to safely write {path_kind} {raw_path}: {error}",
        ) from error


def _cleanup_raw_lane_file(config: ProductionObjectStoreConfig, raw_path: Path, *, path_kind: str) -> None:
    try:
        unlink_no_follow(raw_path, containment_root=config.lane_dir, missing_ok=True)
    except SafeFilesystemError as error:
        error_code = (
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
            if error.kind == "io"
            else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
        )
        raise ProductionObjectStoreValidationError(
            error_code,
            f"Failed to safely remove {path_kind} {raw_path}: {error}",
        ) from error
    except OSError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
            f"Failed to safely remove {path_kind} {raw_path}: {error}",
        ) from error


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
    stored_manifest_bytes = store.read_bytes_limited(str(manifest["manifest_uri"]), max_bytes=MAX_STORED_MANIFEST_BYTES)
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
        actual_size, actual_sha256 = store.size_and_checksum(object_uri)
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
        registry = _registry_import_evidence(config, inventory_path, package_manifest_raw_path, manifest, sources)
    except BasinsRegistryImportError as error:
        registry = {"status": "blocked", **error.to_payload(), "implicit_activation": False}

    runtime = _runtime_staging_evidence(config, store, manifest, writer)
    api_contract_source = (
        "live_registry_import" if registry.get("live_registry_import") is True else "local_import_source"
    )
    api_fixture_model_package_uri = str(registry.get("model_package_uri") or manifest["model_package_uri"])
    api_fixture_manifest_uri = str(registry.get("manifest_uri") or manifest["manifest_uri"])
    api_fixture_package_checksum = str(registry.get("package_checksum") or manifest["package_checksum"])
    api = {
        "status": "local_contract",
        "api_contract_source": api_contract_source,
        "live_api_status": "not_executed",
        "live_api_reason": "fast lane does not require a running API or registry database.",
        "live_api": False,
        "acceptance_evidence": "registry_import_contract_smoke"
        if api_contract_source == "live_registry_import"
        else "local_contract_smoke",
        "model_response_fixture": {
            "model_id": registry.get("model_id") or manifest["model_id"],
            "active": False,
            "model_package_uri": api_fixture_model_package_uri,
            "manifest_uri": api_fixture_manifest_uri,
            "package_checksum": api_fixture_package_checksum,
        },
    }
    runtime_source_values = [
        manifest.get("model_package_uri"),
        manifest.get("manifest_uri"),
        runtime.get("runtime_manifest", {}).get("model_package_uri"),
        runtime.get("runtime_manifest", {}).get("manifest_uri"),
        api["model_response_fixture"]["model_package_uri"],
        api["model_response_fixture"]["manifest_uri"],
        registry.get("model_package_uri"),
        registry.get("manifest_uri"),
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
    acceptance_evidence = _consumption_acceptance_evidence(registry)
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
        "live_registry_import": registry.get("live_registry_import") is True,
        "live_api": api["live_api"] is True,
        "api_contract_source": api_contract_source,
        "acceptance_evidence": acceptance_evidence,
        "acceptance_note": _consumption_acceptance_note(registry),
    }


def _registry_import_evidence(
    config: ProductionObjectStoreConfig,
    inventory_path: Path,
    package_manifest_raw_path: Path,
    manifest: dict[str, Any],
    sources: Any,
) -> dict[str, Any]:
    local_contract = {
        "model_id": sources.ids["model_id"],
        "basin_id": sources.ids["basin_id"],
        "basin_version_id": sources.ids["basin_version_id"],
        "river_network_version_id": sources.ids["river_network_version_id"],
        "mesh_version_id": sources.ids["mesh_version_id"],
        "segment_count": sources.geometry.segment_count,
        "active": False,
        "implicit_activation": False,
        "model_package_uri": manifest["model_package_uri"],
        "manifest_uri": manifest["manifest_uri"],
        "package_checksum": manifest["package_checksum"],
    }
    if not config.run_registry_import:
        return {
            "status": "local_contract_prepared",
            "db_import_status": "not_executed",
            "db_import_reason": (
                "fast lane does not require PostgreSQL/PostGIS; geometry and manifest contracts were validated locally."
            ),
            "live_registry_import": False,
            "acceptance_evidence": "local_contract_smoke",
            **local_contract,
        }
    if not config.registry_database_url:
        return {
            "status": "blocked",
            "db_import_status": "blocked",
            "error_code": "PRODUCTION_OBJECT_STORE_REGISTRY_DATABASE_URL_MISSING",
            "message": (
                "NHMS_PRODUCTION_OBJECT_STORE_RUN_REGISTRY_IMPORT=1 requires "
                "NHMS_PRODUCTION_OBJECT_STORE_REGISTRY_DATABASE_URL or DATABASE_URL."
            ),
            "live_registry_import": False,
            "acceptance_evidence": "live_registry_import_blocked",
            **local_contract,
        }
    try:
        report = import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=package_manifest_raw_path,
            database_url=config.registry_database_url,
        )
    except BasinsRegistryImportError as error:
        return {
            "status": "blocked",
            "db_import_status": "blocked",
            **error.to_payload(),
            "live_registry_import": False,
            "acceptance_evidence": "live_registry_import_blocked",
            **local_contract,
        }
    row_counts = report.get("row_counts") if isinstance(report.get("row_counts"), dict) else {}
    inserted_row_counts = {str(key): int(value) for key, value in row_counts.items() if isinstance(value, int)}
    inserted_total = sum(inserted_row_counts.values())
    return {
        "status": "imported",
        "db_import_status": report.get("status", "imported"),
        "live_registry_import": True,
        "acceptance_evidence": "live_registry_import",
        "registry_import_report": report,
        "inserted_row_counts": inserted_row_counts,
        "inserted_total": inserted_total,
        "updated_row_counts": {},
        "updated_total": 0,
        "idempotent": report.get("status") == "already_imported" or inserted_total == 0,
        **local_contract,
        "active": report.get("active", False),
        "model_package_uri": report.get("model_package_uri", manifest["model_package_uri"]),
        "manifest_uri": report.get("manifest_uri", manifest["manifest_uri"]),
        "package_checksum": report.get("package_checksum", manifest["package_checksum"]),
    }


def _consumption_acceptance_note(registry: dict[str, Any]) -> str:
    if registry.get("live_registry_import") is True:
        return (
            "Live registry DB import evidence ran by explicit opt-in. The API contract smoke is deterministic "
            "and sourced from that live registry import report; live API execution remains explicitly not executed."
        )
    if registry.get("status") == "blocked":
        return (
            "Registry/API/runtime consumption is blocked because live registry import was requested but did not "
            "produce successful DB import evidence."
        )
    return (
        "Default fast validation prepares local registry import sources and proves the API/runtime object-URI "
        "contract locally. Live DB import and live API execution are explicitly not executed in this lane."
    )


def _consumption_acceptance_evidence(registry: dict[str, Any]) -> str:
    if registry.get("live_registry_import") is True:
        return "live_registry_import_contract_smoke"
    if registry.get("status") == "blocked":
        return "live_registry_import_blocked"
    return "local_contract_smoke"


def _runtime_staging_evidence(
    config: ProductionObjectStoreConfig,
    store: LocalObjectStore,
    manifest: dict[str, Any],
    writer: EvidenceWriter,
) -> dict[str, Any]:
    scratch_prefix = f"runs/{config.run_id}/input/scratch/runtime-staging"
    forcing_key = f"{scratch_prefix}/forcing/gfs/2026051600/basin_v1/{manifest['model_id']}/forcing.tsd.forc"
    _write_validation_scratch_object(store, forcing_key, b"forcing\n")
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
            "run_manifest_uri": store.uri_for_key(f"runs/{config.run_id}/input/runtime-staging/manifest.json"),
            "output_uri": store.uri_for_key(f"runs/{config.run_id}/output/runtime-staging/"),
            "log_uri": store.uri_for_key(f"runs/{config.run_id}/logs/runtime-staging/"),
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
    _validate_lane_path_contained(config, runtime_config.workspace_root, path_kind="runtime workspace")
    runtime = SHUDRuntime(
        config=runtime_config,
        object_store=store,
    )
    input_dir = config.lane_dir / "runtime-workspace" / "runs" / runtime_manifest["run_id"] / "input"
    output_dir = config.lane_dir / "runtime-workspace" / "runs" / runtime_manifest["run_id"] / "output"
    _validate_lane_path_contained(config, input_dir, path_kind="runtime input directory")
    _validate_lane_path_contained(config, output_dir, path_kind="runtime output directory")
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    _validate_lane_path_contained(config, input_dir, path_kind="runtime input directory")
    _validate_lane_path_contained(config, output_dir, path_kind="runtime output directory")
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
                "manifest_uri": manifest["manifest_uri"],
                "forcing_uri": runtime_manifest["forcing"]["forcing_uri"],
            },
            "validation_object_keys": [forcing_key],
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
            "manifest_uri": manifest["manifest_uri"],
            "forcing_uri": runtime_manifest["forcing"]["forcing_uri"],
            "run_manifest_uri": runtime_manifest["outputs"]["run_manifest_uri"],
        },
        "scratch_prefix": scratch_prefix,
        "validation_object_keys": [forcing_key],
        "staged_file_count": len(staged_files),
        "staged_files": staged_files,
        "generated_cfg_path": str(cfg_path),
    }
    writer.write_json(config.lane_dir / "runtime_staging_manifest.json", runtime_manifest)
    return evidence


def _write_validation_scratch_object(store: LocalObjectStore, key: str, content: bytes) -> str:
    normalized_key = store.normalize_key(key)
    if not normalized_key.startswith("runs/"):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_KEY_UNSAFE",
            "Validation-created runtime scratch objects must stay under runs/<run_id>/.",
        )
    if store.exists(normalized_key):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_OBJECT_EXISTS",
            f"Validation scratch object already exists and will not be overwritten: {normalized_key}",
        )
    return store.write_bytes_atomic(normalized_key, content)


def _cleanup_rollback_evidence(
    config: ProductionObjectStoreConfig,
    store: LocalObjectStore,
    model_id: str,
) -> dict[str, Any]:
    scratch_prefix = f"runs/{config.run_id}/input/scratch/cleanup-rollback/{model_id}/{config.version}-failed-import"
    partial_key = f"{scratch_prefix}/partial-package.bin"
    created_keys: set[str] = set()
    _write_validation_run_scratch_object(
        config,
        store,
        partial_key,
        b"partial object written before simulated import failure\n",
    )
    created_keys.add(store.normalize_key(partial_key))
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
        _delete_validation_run_object(config, store, partial_key, created_keys)
        cleanup_status = "deleted"
    elif config.cleanup_policy == "quarantine":
        quarantine_key = f"runs/{config.run_id}/logs/quarantine/{partial_key}"
        content = store.read_bytes(partial_key)
        _write_validation_run_scratch_object(config, store, quarantine_key, content)
        created_keys.add(store.normalize_key(quarantine_key))
        _delete_validation_run_object(config, store, partial_key, created_keys)
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


def _write_validation_run_scratch_object(
    config: ProductionObjectStoreConfig,
    store: LocalObjectStore,
    key: str,
    content: bytes,
) -> str:
    normalized_key = store.normalize_key(key)
    if not _is_validation_run_object(config, normalized_key):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_KEY_UNSAFE",
            f"Validation-created cleanup objects must stay under runs/{config.run_id}/.",
        )
    return _write_validation_scratch_object(store, normalized_key, content)


def _delete_validation_run_object(
    config: ProductionObjectStoreConfig,
    store: LocalObjectStore,
    key: str,
    created_keys: set[str],
) -> None:
    normalized_key = store.normalize_key(key)
    if not _is_validation_run_object(config, normalized_key):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_KEY_UNSAFE",
            f"Validation cleanup may only delete objects under runs/{config.run_id}/.",
        )
    if normalized_key not in created_keys:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_KEY_UNSAFE",
            "Validation cleanup may only delete objects created by the current validation run.",
        )
    store.delete(normalized_key)


def _is_validation_run_object(config: ProductionObjectStoreConfig, key: str) -> bool:
    return key == f"runs/{config.run_id}" or key.startswith(f"runs/{config.run_id}/")


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
        "run_registry_import": config.run_registry_import,
        "registry_database_url_configured": config.registry_database_url is not None,
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
    consumption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    live_registry_import = consumption.get("live_registry_import") is True if consumption is not None else False
    live_api = consumption.get("live_api") is True if consumption is not None else False
    api_contract_source = str(consumption.get("api_contract_source", "")) if consumption is not None else ""
    live_api_status = "not_executed"
    if consumption is not None and isinstance(consumption.get("api"), dict):
        live_api_status = str(consumption["api"].get("live_api_status", "not_executed"))
    deterministic_fixture = not (live_registry_import and live_api)
    payload: dict[str, Any] = {
        "schema": "nhms.production_closure.object_store.v1",
        "issue": 148,
        "run_id": config.run_id,
        "status": status,
        "evidence_dir": str(config.lane_dir),
        "target": config.target,
        "object_store_prefix": config.object_store_prefix,
        "execution_mode": "live_registry_import_and_live_api"
        if not deterministic_fixture
        else (
            "live_registry_import_with_deterministic_api_contract"
            if live_registry_import
            else "deterministic_fixture"
        ),
        "deterministic_fixture": deterministic_fixture,
        "live_registry_import": live_registry_import,
        "live_api": live_api,
        "live_api_status": live_api_status,
        "api_contract_source": api_contract_source or "not_executed",
        "final_production_readiness_claimed": False,
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


def _truthy_env(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


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
    _validate_object_store_prefix_safe(config.configured_object_store_prefix)
    if config.object_store_prefix != config.configured_object_store_prefix:
        _validate_object_store_prefix_safe(config.object_store_prefix)


def _validate_internal_lane_paths(config: ProductionObjectStoreConfig) -> None:
    for path, path_kind in (
        (config.lane_dir / "synthetic-basins", "synthetic basins fixture"),
        (config.lane_dir / ".inventory.raw.json", "raw inventory file"),
        (config.lane_dir / ".package_manifest.raw.json", "raw package manifest file"),
        (config.lane_dir / ".migration_report.raw.json", "raw migration report file"),
        (config.lane_dir / "runtime-workspace", "runtime workspace"),
    ):
        _validate_lane_path_contained(config, path, path_kind=path_kind)
    _validate_local_object_store_root(config)


def _validate_local_object_store_root(config: ProductionObjectStoreConfig) -> None:
    if config.object_store_root.is_symlink():
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
            f"local object store root must not be a symlink: {config.object_store_root}",
        )
    _refuse_symlink_components(config.object_store_root)
    resolved_lane = config.lane_dir.resolve(strict=False)
    try:
        config.object_store_root.expanduser().resolve(strict=False).relative_to(resolved_lane)
    except ValueError:
        pass
    else:
        _validate_lane_path_contained(config, config.object_store_root, path_kind="local object store root")
        _refuse_existing_descendant_symlinks(config.object_store_root, path_kind="local object store root")
        return
    try:
        configured_root = config.object_store_root.expanduser().resolve(strict=False)
        default_root = (config.lane_dir / "local-object-store").resolve(strict=False)
        configured_root.relative_to(default_root)
    except ValueError:
        _refuse_run_scoped_local_object_store_symlinks(config)
        return
    _validate_lane_path_contained(config, config.object_store_root, path_kind="local object store root")
    _refuse_existing_descendant_symlinks(config.object_store_root, path_kind="local object store root")


def _validate_lane_path_contained(
    config: ProductionObjectStoreConfig,
    path: Path,
    *,
    path_kind: str,
) -> None:
    _refuse_symlink_components(path)
    if path.is_symlink():
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
            f"{path_kind} must not be a symlink: {path}",
        )
    resolved_path = path.resolve(strict=False)
    resolved_lane = config.lane_dir.resolve(strict=False)
    try:
        resolved_path.relative_to(config.evidence_root)
        resolved_path.relative_to(resolved_lane)
    except ValueError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"{path_kind} must stay under the current object-store evidence lane.",
        ) from error


def _refuse_run_scoped_local_object_store_symlinks(config: ProductionObjectStoreConfig) -> None:
    root = config.object_store_root.expanduser()
    for prefix in _run_scoped_local_object_store_prefixes(config):
        _refuse_existing_descendant_symlinks(root / prefix, path_kind="local object store run prefix")


def _run_scoped_local_object_store_prefixes(config: ProductionObjectStoreConfig) -> tuple[Path, ...]:
    prefix_path = PurePosixPath(unquote(urlsplit(config.object_store_prefix).path).strip("/"))
    prefixes = {
        Path("runs") / config.run_id,
        Path(*prefix_path.parts) if prefix_path.parts else Path(),
        Path(config.version).parent,
    }
    return tuple(sorted((prefix for prefix in prefixes if str(prefix) != "."), key=str))


def _refuse_existing_descendant_symlinks(root: Path, *, path_kind: str) -> None:
    if root.is_symlink():
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
            f"{path_kind} must not contain symlinks: {root}",
        )
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
                f"{path_kind} must not contain symlinks: {path}",
            )


def _validate_object_store_prefix_safe(prefix: str) -> None:
    if not prefix:
        return
    try:
        parsed = urlsplit(prefix)
    except ValueError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix must not contain credential material.",
        ) from error
    if parsed.username or parsed.password:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix must not contain userinfo credentials.",
        )
    if parsed.query:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix must not contain query parameters.",
        )
    if parsed.fragment:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix must not contain fragments.",
        )
    for decoded in _canonical_decode_steps(prefix):
        if ENCODED_SEPARATOR_RE.search(decoded):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                "Object-store prefix path must not contain encoded separators.",
            )
        if SENSITIVE_PREFIX_ASSIGNMENT_RE.search(decoded):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                "Object-store prefix must not contain credential assignments.",
            )
        decoded_parts = SENSITIVE_PREFIX_SEPARATOR_RE.split(decoded)
        if any(SENSITIVE_PREFIX_ASSIGNMENT_RE.search(part) for part in decoded_parts):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                "Object-store prefix must not contain credential assignments.",
            )
        decoded_parsed = urlsplit(decoded)
        if decoded_parsed.username or decoded_parsed.password:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                "Object-store prefix must not contain userinfo credentials.",
            )
        _guard_url_authority(decoded_parsed.netloc)
        for segment in decoded_parsed.path.split("/"):
            if segment in {".", ".."} or "\\" in segment:
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                    "Object-store prefix path must not contain traversal.",
                )


def _guard_url_authority(netloc: str) -> None:
    if not netloc:
        return
    if "/" in netloc or "\\" in netloc:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix URL authority must not contain separators.",
        )
    host = netloc.rsplit("@", maxsplit=1)[-1].split(":", maxsplit=1)[0]
    if host in {".", ".."} or any(segment in {"", ".", ".."} for segment in host.split(".")):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix URL authority must not contain traversal.",
        )


def _canonical_decode_steps(value: str) -> tuple[str, ...]:
    steps = [value]
    current = value
    for _ in range(MAX_PERCENT_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        steps.append(decoded)
        current = decoded
    if unquote(current) != current:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix contains over-encoded percent escapes.",
        )
    return tuple(steps)


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
    _refuse_symlink_components_to_deepest_existing(root)
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


def _refuse_symlink_components_to_deepest_existing(path: Path) -> None:
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
        if not current.exists():
            break


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
            click.echo(json.dumps(redact_payload(summary), sort_keys=True))
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

    try:
        cli.main(args=list(argv) if argv is not None else None, standalone_mode=False)
    except click.ClickException as error:
        error.show()
        raise SystemExit(error.exit_code) from error
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
                    redact_payload(
                        validate_object_store(
                            ProductionObjectStoreConfig.from_env(
                                evidence_root=args.evidence_root,
                                run_id=args.run_id,
                                basins_root=args.basins_root,
                                model_id=args.model_id,
                                version=args.version,
                                force=args.force,
                            )
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
