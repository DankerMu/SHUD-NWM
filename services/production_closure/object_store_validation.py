from __future__ import annotations

import argparse
import json
import os
import re
import sys
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
)
from services.production_closure import object_store_validation_consumption as _consumption
from services.production_closure import object_store_validation_evidence as _evidence
from services.production_closure import object_store_validation_fixture as _fixture
from services.production_closure import object_store_validation_manifest as _manifest
from services.production_closure import object_store_validation_path_safety as _path_safety
from services.production_closure import object_store_validation_runtime as _runtime
from services.production_closure.object_store_validation_contracts import (
    DEFAULT_BASINS_MIGRATION_SOURCE_URI,
    DEFAULT_CLEANUP_POLICY,
    DEFAULT_OBJECT_STORE_TARGET,
    PackageChecksumReconstruction,
    ProductionObjectStoreValidationError,
    RuntimePrefixCollection,
    RuntimeStagedObject,
    RuntimeStagingBudget,
    RuntimeStagingPreparation,
    _deterministic_manifest_bytes,
    _sha256_json,
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
)

_compatibility_names = (
    re,
    UTC,
    datetime,
    PurePosixPath,
    unquote,
    urlsplit,
    urlunsplit,
    MAX_OBJECT_MANIFEST_BYTES,
    DEFAULT_BASINS_MIGRATION_SOURCE_URI,
    DEFAULT_CLEANUP_POLICY,
    DEFAULT_OBJECT_STORE_TARGET,
)
del _compatibility_names

for _historical_class in (
    ProductionObjectStoreValidationError,
    PackageChecksumReconstruction,
    RuntimePrefixCollection,
    RuntimeStagedObject,
    RuntimeStagingBudget,
    RuntimeStagingPreparation,
):
    _historical_class.__module__ = __name__
del _historical_class


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


_OWNER_BINDINGS = {
    "fixture": _fixture,
    "manifest": _manifest,
    "consumption": _consumption,
    "runtime": _runtime,
    "path_safety": _path_safety,
    "evidence": _evidence,
}


def _call_owner(
    owner: object, name: str, /, *args: object, _globals: dict[str, object] | None = None, **kwargs: object
) -> object:
    function = getattr(owner, name)
    values = dict(vars(owner))
    values.update(_globals or {})
    rebound = type(function)(function.__code__, values, function.__name__, function.__defaults__, function.__closure__)
    rebound.__kwdefaults__ = function.__kwdefaults__
    rebound.__annotations__ = function.__annotations__
    return rebound(*args, **kwargs)


def _fixture_globals() -> dict[str, object]:
    return {
        "ensure_directory_no_follow": ensure_directory_no_follow,
        "atomic_write_bytes_no_follow": atomic_write_bytes_no_follow,
        "_safe_fixture_dir": _safe_fixture_dir,
        "_safe_fixture_write_bytes": _safe_fixture_write_bytes,
        "_safe_fixture_write_text": _safe_fixture_write_text,
        "_write_domain_shapefile": _write_domain_shapefile,
        "_write_river_shapefile": _write_river_shapefile,
        "_write_segment_crosswalk_shapefile": _write_segment_crosswalk_shapefile,
        "_write_wgs84_prj": _write_wgs84_prj,
        "_copy_fixture_shapefile_outputs": _copy_fixture_shapefile_outputs,
    }


def write_synthetic_basins_fixture(root: Path, *, containment_root: Path | None = None) -> dict[str, Any]:
    return _call_owner(
        _OWNER_BINDINGS["fixture"],
        "write_synthetic_basins_fixture",
        root,
        containment_root=containment_root,
        _globals=_fixture_globals(),
    )  # type: ignore[return-value]


def _write_migration_evidence(
    config: ProductionObjectStoreConfig,
    writer: EvidenceWriter,
    basins_root: Path,
    blockers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    return _call_owner(
        _OWNER_BINDINGS["manifest"],
        "_write_migration_evidence",
        config,
        writer,
        basins_root,
        blockers,
        _globals={
            "write_basins_migration_report": write_basins_migration_report,
            "_write_raw_worker_output": _write_raw_worker_output,
            "_cleanup_raw_lane_file": _cleanup_raw_lane_file,
        },
    )  # type: ignore[return-value]


def _write_raw_worker_output(
    config: ProductionObjectStoreConfig,
    raw_path: Path,
    *,
    path_kind: str,
    producer: Callable[[Path], Any],
) -> tuple[Any, bytes]:
    return _call_owner(
        _OWNER_BINDINGS["manifest"],
        "_write_raw_worker_output",
        config,
        raw_path,
        path_kind=path_kind,
        producer=producer,
        _globals={
            "atomic_write_bytes_no_follow": atomic_write_bytes_no_follow,
            "_write_raw_lane_bytes": _write_raw_lane_bytes,
        },
    )  # type: ignore[return-value]


def _write_raw_lane_bytes(
    config: ProductionObjectStoreConfig,
    raw_path: Path,
    content: bytes,
    *,
    path_kind: str,
) -> None:
    _call_owner(
        _OWNER_BINDINGS["manifest"],
        "_write_raw_lane_bytes",
        config,
        raw_path,
        content,
        path_kind=path_kind,
        _globals={"atomic_write_bytes_no_follow": atomic_write_bytes_no_follow},
    )


def _cleanup_raw_lane_file(config: ProductionObjectStoreConfig, raw_path: Path, *, path_kind: str) -> None:
    _call_owner(_OWNER_BINDINGS["manifest"], "_cleanup_raw_lane_file", config, raw_path, path_kind=path_kind)


def _verify_stored_objects(store: LocalObjectStore, manifest: dict[str, Any]) -> dict[str, Any]:
    return _call_owner(
        _OWNER_BINDINGS["manifest"],
        "_verify_stored_objects",
        store,
        manifest,
        _globals={
            "PackageChecksumReconstruction": PackageChecksumReconstruction,
            "_deterministic_manifest_bytes": _deterministic_manifest_bytes,
            "_sha256_json": _sha256_json,
        },
    )  # type: ignore[return-value]


def _consumption_evidence(
    config: ProductionObjectStoreConfig,
    writer: EvidenceWriter,
    store: LocalObjectStore,
    inventory_path: Path,
    package_manifest_raw_path: Path,
    manifest: dict[str, Any],
    stored_verification: dict[str, Any],
) -> dict[str, Any]:
    return _call_owner(
        _OWNER_BINDINGS["consumption"],
        "_consumption_evidence",
        config,
        writer,
        store,
        inventory_path,
        package_manifest_raw_path,
        manifest,
        stored_verification,
        _globals={
            "import_basins_registry": import_basins_registry,
            "_registry_import_evidence": _registry_import_evidence,
            "_runtime_staging_evidence": _runtime_staging_evidence,
        },
    )  # type: ignore[return-value]


def _registry_import_evidence(
    config: ProductionObjectStoreConfig,
    inventory_path: Path,
    package_manifest_raw_path: Path,
    manifest: dict[str, Any],
    sources: Any,
) -> dict[str, Any]:
    return _call_owner(
        _OWNER_BINDINGS["consumption"],
        "_registry_import_evidence",
        config,
        inventory_path,
        package_manifest_raw_path,
        manifest,
        sources,
        _globals={"import_basins_registry": import_basins_registry},
    )  # type: ignore[return-value]


def _runtime_staging_evidence(
    config: ProductionObjectStoreConfig,
    store: LocalObjectStore,
    manifest: dict[str, Any],
    stored_verification: dict[str, Any],
    writer: EvidenceWriter,
) -> dict[str, Any]:
    return _call_owner(
        _OWNER_BINDINGS["runtime"],
        "_runtime_staging_evidence",
        config,
        store,
        manifest,
        stored_verification,
        writer,
        _globals={
            "ensure_directory_no_follow": ensure_directory_no_follow,
            "_prepare_runtime_staging_workspace": _prepare_runtime_staging_workspace,
        },
    )  # type: ignore[return-value]


def _prepare_runtime_staging_workspace(
    config: ProductionObjectStoreConfig,
    store: LocalObjectStore,
    runtime_manifest: dict[str, Any],
    package_manifest: dict[str, Any],
    stored_verification: dict[str, Any],
    input_dir: Path,
    output_dir: Path,
    *,
    allowed_forcing_keys: set[str] | None = None,
) -> RuntimeStagingPreparation:
    return _call_owner(
        _OWNER_BINDINGS["runtime"],
        "_prepare_runtime_staging_workspace",
        config,
        store,
        runtime_manifest,
        package_manifest,
        stored_verification,
        input_dir,
        output_dir,
        allowed_forcing_keys=allowed_forcing_keys,
        _globals={
            "atomic_write_bytes_no_follow": atomic_write_bytes_no_follow,
            "_write_runtime_staging_bytes": _write_runtime_staging_bytes,
        },
    )  # type: ignore[return-value]


def _write_runtime_staging_bytes(config: ProductionObjectStoreConfig, target: Path, content: bytes) -> None:
    _call_owner(
        _OWNER_BINDINGS["runtime"],
        "_write_runtime_staging_bytes",
        config,
        target,
        content,
        _globals={"atomic_write_bytes_no_follow": atomic_write_bytes_no_follow},
    )


def _cleanup_rollback_evidence(
    config: ProductionObjectStoreConfig,
    store: LocalObjectStore,
    model_id: str,
) -> dict[str, Any]:
    return _call_owner(_OWNER_BINDINGS["consumption"], "_cleanup_rollback_evidence", config, store, model_id)  # type: ignore[return-value]


def _safe_fixture_dir(path: Path, *, containment_root: Path | None) -> None:
    _call_owner(
        _OWNER_BINDINGS["fixture"],
        "_safe_fixture_dir",
        path,
        containment_root=containment_root,
        _globals={"ensure_directory_no_follow": ensure_directory_no_follow},
    )


def _safe_fixture_write_bytes(path: Path, content: bytes, *, containment_root: Path | None) -> None:
    _call_owner(
        _OWNER_BINDINGS["fixture"],
        "_safe_fixture_write_bytes",
        path,
        content,
        containment_root=containment_root,
        _globals={"atomic_write_bytes_no_follow": atomic_write_bytes_no_follow},
    )


def _safe_fixture_write_text(path: Path, content: str, *, containment_root: Path | None) -> None:
    _safe_fixture_write_bytes(path, content.encode("utf-8"), containment_root=containment_root)


def _write_domain_shapefile(base: Path, *, containment_root: Path | None = None) -> None:
    _call_owner(
        _OWNER_BINDINGS["fixture"],
        "_write_domain_shapefile",
        base,
        containment_root=containment_root,
        _globals={"_safe_fixture_write_text": _safe_fixture_write_text},
    )


def _write_river_shapefile(base: Path, *, containment_root: Path | None = None) -> None:
    _call_owner(
        _OWNER_BINDINGS["fixture"],
        "_write_river_shapefile",
        base,
        containment_root=containment_root,
        _globals={"_safe_fixture_write_text": _safe_fixture_write_text},
    )


def _write_segment_crosswalk_shapefile(base: Path, *, containment_root: Path | None = None) -> None:
    _call_owner(
        _OWNER_BINDINGS["fixture"],
        "_write_segment_crosswalk_shapefile",
        base,
        containment_root=containment_root,
        _globals={"_safe_fixture_write_text": _safe_fixture_write_text},
    )


def _write_wgs84_prj(path: Path, *, containment_root: Path | None = None) -> None:
    _safe_fixture_write_text(
        path,
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]\n',
        containment_root=containment_root,
    )


def _copy_fixture_shapefile_outputs(source_base: Path, target_base: Path, *, containment_root: Path) -> None:
    _call_owner(
        _OWNER_BINDINGS["fixture"],
        "_copy_fixture_shapefile_outputs",
        source_base,
        target_base,
        containment_root=containment_root,
        _globals={"_safe_fixture_write_bytes": _safe_fixture_write_bytes},
    )


def _validate_config(config: ProductionObjectStoreConfig) -> None:
    _call_owner(_OWNER_BINDINGS["path_safety"], "_validate_config", config)


def _validate_internal_lane_paths(config: ProductionObjectStoreConfig) -> None:
    _call_owner(_OWNER_BINDINGS["path_safety"], "_validate_internal_lane_paths", config)


def _validate_local_object_store_root(config: ProductionObjectStoreConfig) -> None:
    _call_owner(_OWNER_BINDINGS["path_safety"], "_validate_local_object_store_root", config)


def _validate_lane_path_contained(config: ProductionObjectStoreConfig, path: Path, *, path_kind: str) -> None:
    _call_owner(_OWNER_BINDINGS["path_safety"], "_validate_lane_path_contained", config, path, path_kind=path_kind)


def _refuse_existing_descendant_symlinks(root: Path, *, path_kind: str) -> None:
    _call_owner(_OWNER_BINDINGS["path_safety"], "_refuse_existing_descendant_symlinks", root, path_kind=path_kind)


def _refuse_symlink_components(path: Path) -> None:
    _call_owner(_OWNER_BINDINGS["path_safety"], "_refuse_symlink_components", path)


def _safe_resolved_evidence_root(evidence_root: Path) -> Path:
    return _call_owner(_OWNER_BINDINGS["path_safety"], "_safe_resolved_evidence_root", evidence_root)  # type: ignore[return-value]


def _safe_run_id(run_id: str) -> str:
    return _call_owner(_OWNER_BINDINGS["path_safety"], "_safe_run_id", run_id)  # type: ignore[return-value]


def _operational_prefix(value: str) -> str:
    return _call_owner(_OWNER_BINDINGS["path_safety"], "_operational_prefix", value)  # type: ignore[return-value]


def _truthy_env(value: str | None) -> bool:
    return _call_owner(_OWNER_BINDINGS["evidence"], "_truthy_env", value)  # type: ignore[return-value]


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
        write_synthetic_basins_fixture(basins_root, containment_root=config.lane_dir)

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

        consumption = _consumption_evidence(
            config,
            writer,
            store,
            inventory_path,
            package_manifest_raw_path,
            manifest,
            stored_verification,
        )
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


def _package_manifest_evidence(publish_result: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    return _call_owner(_OWNER_BINDINGS["manifest"], "_package_manifest_evidence", publish_result, manifest)  # type: ignore[return-value]


def _preflight_payload(config: ProductionObjectStoreConfig) -> dict[str, Any]:
    return _call_owner(_OWNER_BINDINGS["evidence"], "_preflight_payload", config)  # type: ignore[return-value]


def _environment_payload(config: ProductionObjectStoreConfig) -> dict[str, Any]:
    return _call_owner(_OWNER_BINDINGS["evidence"], "_environment_payload", config)  # type: ignore[return-value]


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
    return _call_owner(
        _OWNER_BINDINGS["evidence"],
        "_summary",
        config,
        status=status,
        blockers=blockers,
        files=files,
        selected_model_id=selected_model_id,
        version=version,
        migration_report=migration_report,
        package_manifest=package_manifest,
        consumption=consumption,
    )  # type: ignore[return-value]


def _result_blockers(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    return _call_owner(_OWNER_BINDINGS["evidence"], "_result_blockers", *payloads)  # type: ignore[return-value]


def _default_model_id(inventory: dict[str, Any]) -> str:
    return _call_owner(_OWNER_BINDINGS["evidence"], "_default_model_id", inventory)  # type: ignore[return-value]


for _reexport_owner in _OWNER_BINDINGS.values():
    for _reexport_name, _reexport_value in vars(_reexport_owner).items():
        if _reexport_name.startswith("__") or _reexport_name.startswith("_Config") or _reexport_name in globals():
            continue
        globals()[_reexport_name] = _reexport_value

for _signature_owner in (_path_safety, _manifest, _runtime, _consumption):
    _signature_owner.ProductionObjectStoreConfig = ProductionObjectStoreConfig
    for _signature_name, _signature_value in vars(_signature_owner).items():
        if not callable(_signature_value) or "config" not in getattr(_signature_value, "__annotations__", {}):
            continue
        _signature_value.__annotations__["config"] = "ProductionObjectStoreConfig"

del _reexport_name, _reexport_owner, _reexport_value, _signature_name, _signature_owner, _signature_value


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
