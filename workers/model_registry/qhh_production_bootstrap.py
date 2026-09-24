from __future__ import annotations

import errno as errno
import hashlib
import json
import math as math
import os
import stat
from collections import Counter as Counter
from collections.abc import Mapping as Mapping
from collections.abc import Sequence as Sequence
from dataclasses import dataclass as dataclass
from pathlib import Path
from pathlib import PurePosixPath as PurePosixPath
from typing import Any

from packages.common.forcing_ts_render import (
    FORCING_STORES,
    FORCING_TABLE_TOKEN,
    ForcingTemplatePair,
    render_forcing_ts_sql,
)
from packages.common.safe_fs import SafeFilesystemError, stat_no_follow
from packages.common.safe_fs import atomic_write_bytes_no_follow as atomic_write_bytes_no_follow
from packages.common.safe_fs import ensure_directory_no_follow as ensure_directory_no_follow
from packages.common.safe_fs import read_bytes_limited_no_follow as read_bytes_limited_no_follow
from packages.common.safe_fs import unlink_no_follow as unlink_no_follow
from packages.common.safe_fs import verify_directory_no_follow as verify_directory_no_follow

from .basins_discovery import BasinsDiscoveryError, DiscoveryBudget, discover_basins_inventory, resolve_basins_root
from .basins_geometry import BasinsGeometryError as BasinsGeometryError
from .basins_geometry import TrustedBasinsRoot
from .basins_geometry import trusted_basins_root as trusted_basins_root
from .basins_package import BasinsPackageError, publish_basins_package
from .basins_registry_import import (
    BasinsRegistryImportError,
    _backfill_output_segment_geometry,
    _json,
    _json_dict,
    _lock_river_network_version,
    _transaction,
    import_basin_into_registry_core,
)
from .basins_registry_import import ImportSources as ImportSources
from .basins_registry_import import _fetch_optional as _fetch_optional
from .basins_registry_import import _find_inventory_model as _find_inventory_model
from .basins_registry_import import _input_dir as _input_dir
from .basins_registry_import import _inventory_root as _inventory_root
from .basins_registry_import import _prepare_sources as _prepare_sources
from .basins_registry_import import _recorded_relative_inventory_root as _recorded_relative_inventory_root
from .basins_registry_import import _source_root as _source_root
from .qhh_bootstrap_contracts import _EVIDENCE_DIR_FLAGS as _EVIDENCE_DIR_FLAGS
from .qhh_bootstrap_contracts import _EVIDENCE_FILE_FLAGS as _EVIDENCE_FILE_FLAGS
from .qhh_bootstrap_contracts import (
    DEFAULT_QHH_BASIN_SLUG,
    DEFAULT_QHH_MODEL_ID,
    DEFAULT_QHH_PACKAGE_VERSION,
    DEFAULT_QHH_PROJECT_NAME,
    DEFAULT_QHH_SHUD_CODE_VERSION,
    MAX_QHH_BOOTSTRAP_DISCOVERY_DEPTH,
    MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES,
    MAX_QHH_BOOTSTRAP_DISCOVERY_FILE_DEPTH,
    QHH_BOOTSTRAP_SCHEMA_VERSION,
    QhhBootstrapContext,
    QhhBootstrapPaths,
    QhhEvidenceReservation,
    QhhProductionBootstrapError,
    _from_package_error,
    _from_registry_error,
    _qhh_code,
    _safe_identifier,
)
from .qhh_bootstrap_contracts import MAX_QHH_CHECKSUM_BYTES as MAX_QHH_CHECKSUM_BYTES
from .qhh_bootstrap_contracts import MAX_QHH_JSON_BYTES as MAX_QHH_JSON_BYTES
from .qhh_bootstrap_contracts import MAX_QHH_OUTPUT_SEGMENTS as MAX_QHH_OUTPUT_SEGMENTS
from .qhh_bootstrap_contracts import MAX_QHH_SP_RIV_BYTES as MAX_QHH_SP_RIV_BYTES
from .qhh_bootstrap_contracts import MAX_QHH_TSD_FORC_BYTES as MAX_QHH_TSD_FORC_BYTES
from .qhh_bootstrap_contracts import MAX_QHH_TSD_FORC_STATIONS as MAX_QHH_TSD_FORC_STATIONS
from .qhh_bootstrap_contracts import (
    QHH_RESOURCE_PROFILE_OVERRIDE_ALLOWED_FIELDS as QHH_RESOURCE_PROFILE_OVERRIDE_ALLOWED_FIELDS,
)
from .qhh_bootstrap_contracts import (
    QHH_RESOURCE_PROFILE_PRESERVED_OPERATIONAL_FIELDS as QHH_RESOURCE_PROFILE_PRESERVED_OPERATIONAL_FIELDS,
)
from .qhh_bootstrap_contracts import QHH_RESOURCE_PROFILE_RUN_SCOPED_FIELDS as QHH_RESOURCE_PROFILE_RUN_SCOPED_FIELDS
from .qhh_bootstrap_contracts import QhhForcingStation as QhhForcingStation
from .qhh_bootstrap_contracts import QhhPreflightSources as QhhPreflightSources
from .qhh_bootstrap_evidence import (
    _cleanup_reserved_evidence_path,
    _close_reserved_evidence_fd,
    _reserve_evidence_path,
    _unlink_reserved_evidence_path,
    _write_reserved_evidence_path,
)
from .qhh_bootstrap_evidence import _open_evidence_parent_dir as _open_evidence_parent_dir
from .qhh_bootstrap_evidence import _require_reserved_evidence_identity as _require_reserved_evidence_identity
from .qhh_bootstrap_fs import (
    _bootstrap_work_dir,
    _coerce_trusted_root,
    _require_contained_optional_existing_json,
    _require_contained_regular_file,
    _resolve_qhh_source_root,
    _trusted_basins_root,
    _trusted_child_dir,
    _write_generated_json,
)
from .qhh_bootstrap_fs import _read_contained_file_limited as _read_contained_file_limited
from .qhh_bootstrap_fs import _read_standalone_file_limited as _read_standalone_file_limited
from .qhh_bootstrap_fs import _safe_directory_binding as _safe_directory_binding
from .qhh_bootstrap_fs import _safe_trusted_root_binding as _safe_trusted_root_binding
from .qhh_bootstrap_registry import (
    _activate_qhh_model,
    _active_qhh_identity_rows,
    _assert_complete_qhh_output_segment_geometry,
    _assert_complete_qhh_output_segment_stream_type,
    _assert_dynamic_forcing_unchanged,
    _assert_exact_qhh_output_segment_set,
    _assert_exact_qhh_station_set,
    _existing_output_segment_digests,
    _fetch_model_identity,
    _lock_qhh_basin_scope,
    _output_segment_digest,
    _output_segment_expected_properties,
    _persist_inactive_on_scheduler_visibility_blocker,
    _qhh_output_segment_geometry_counts,
    _registry_report_from_row_counts,
    _seed_station_rows,
    _upsert_scheduler_ready_model,
)
from .qhh_bootstrap_registry import _canonical_json as _canonical_json
from .qhh_bootstrap_registry import (
    _canonical_scheduler_ready_resource_profile as _canonical_scheduler_ready_resource_profile,
)
from .qhh_bootstrap_registry import _existing_station_digests as _existing_station_digests
from .qhh_bootstrap_registry import _output_segment_idempotency_properties as _output_segment_idempotency_properties
from .qhh_bootstrap_registry import _persistently_mark_qhh_model_inactive as _persistently_mark_qhh_model_inactive
from .qhh_bootstrap_registry import _safe_resource_profile_overrides as _safe_resource_profile_overrides
from .qhh_bootstrap_registry import _scheduler_ready_resource_profile as _scheduler_ready_resource_profile
from .qhh_bootstrap_registry import _station_digest as _station_digest
from .qhh_bootstrap_sources import _parse_sp_riv_segment_token as _parse_sp_riv_segment_token
from .qhh_bootstrap_sources import _parse_tsd_forc_filename as _parse_tsd_forc_filename
from .qhh_bootstrap_sources import _parse_tsd_forc_station_index as _parse_tsd_forc_station_index
from .qhh_bootstrap_sources import (
    _prepare_preflight_sources_from_bounded_json,
    _prepare_sources_from_preflight,
    _require_qhh_physical_source_binding,
    _require_qhh_preflight_physical_source_binding,
    _require_qhh_preflight_source_identity,
    _require_qhh_source_identity,
    _validate_manifest_checksum,
    read_qhh_output_segment_count,
    read_qhh_tsd_forc,
)
from .qhh_bootstrap_sources import _prepare_sources_from_bounded_json as _prepare_sources_from_bounded_json
from .qhh_bootstrap_sources import _read_json_object_bounded as _read_json_object_bounded
from .qhh_bootstrap_sources import _sp_riv_numeric_columns_valid as _sp_riv_numeric_columns_valid


def bootstrap_qhh_production(
    *,
    database_url: str | None = None,
    basins_root: str | Path | None = None,
    qhh_project_name: str = DEFAULT_QHH_PROJECT_NAME,
    qhh_basin_slug: str = DEFAULT_QHH_BASIN_SLUG,
    model_id: str = DEFAULT_QHH_MODEL_ID,
    package_version: str = DEFAULT_QHH_PACKAGE_VERSION,
    inventory_path: str | Path | None = None,
    package_manifest_path: str | Path | None = None,
    work_dir: str | Path | None = None,
    evidence_dir: str | Path | None = None,
    evidence_path: str | Path | None = None,
    shud_code_version: str = DEFAULT_QHH_SHUD_CODE_VERSION,
    resource_profile_overrides: dict[str, Any] | None = None,
    fail_after_model_metadata: bool = False,
    fail_during_station_seed: bool = False,
    fail_during_output_segment_seed: bool = False,
    trusted_internal: bool = True,
) -> dict[str, Any]:
    resolved_database_url = database_url or os.getenv("DATABASE_URL", "").strip()
    if not resolved_database_url:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_DATABASE_URL_MISSING",
            "DATABASE_URL or --database-url is required for QHH production bootstrap.",
            model_id=model_id,
            details={"no_mutation_expected": True},
        )
    if not _safe_identifier(model_id):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_IDENTITY_INVALID",
            "QHH model_id contains unsupported characters.",
            model_id=model_id,
            details={"field": "model_id", "no_mutation_expected": True},
        )
    if not _safe_identifier(qhh_project_name):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_IDENTITY_INVALID",
            "QHH project name contains unsupported characters.",
            model_id=model_id,
            details={"field": "project_name", "no_mutation_expected": True},
        )

    paths = _prepare_bootstrap_paths(
        basins_root=basins_root,
        qhh_basin_slug=qhh_basin_slug,
        qhh_project_name=qhh_project_name,
        model_id=model_id,
        package_version=package_version,
        inventory_path=inventory_path,
        package_manifest_path=package_manifest_path,
        work_dir=work_dir,
    )
    preflight_sources = _prepare_preflight_sources_from_bounded_json(
        paths.inventory_path,
        paths.package_manifest_path,
        model_id=model_id,
    )
    _require_qhh_preflight_source_identity(
        preflight_sources,
        qhh_basin_slug=qhh_basin_slug,
        qhh_project_name=qhh_project_name,
        model_id=model_id,
    )
    _require_qhh_preflight_physical_source_binding(preflight_sources, paths, model_id=model_id)
    sources = _prepare_sources_from_preflight(preflight_sources, model_id=model_id)
    _require_qhh_source_identity(
        sources,
        qhh_basin_slug=qhh_basin_slug,
        qhh_project_name=qhh_project_name,
        model_id=model_id,
    )
    _require_qhh_physical_source_binding(sources, paths, model_id=model_id)
    _validate_manifest_checksum(paths.package_manifest_path, sources.manifest, model_id=model_id)
    stations, tsd_forc_checksum = read_qhh_tsd_forc(
        paths.tsd_forc_path,
        paths.qhh_input_dir,
        model_id=model_id,
        project_name=qhh_project_name,
    )
    output_segment_count, sp_riv_checksum = read_qhh_output_segment_count(
        paths.qhh_input_dir.path / f"{qhh_project_name}.sp.riv",
        paths.qhh_input_dir,
        model_id=model_id,
    )
    evidence_reservation: QhhEvidenceReservation | None = None
    if evidence_path is not None:
        evidence_reservation = _reserve_evidence_path(evidence_path, evidence_dir=evidence_dir, model_id=model_id)

    context = QhhBootstrapContext(
        sources=sources,
        paths=paths,
        stations=stations,
        output_segment_count=output_segment_count,
        tsd_forc_checksum=tsd_forc_checksum,
        sp_riv_checksum=sp_riv_checksum,
        shud_code_version=shud_code_version,
    )
    database_succeeded = False
    try:
        report = _bootstrap_database(
            context,
            resolved_database_url,
            resource_profile_overrides=resource_profile_overrides or {},
            fail_after_model_metadata=fail_after_model_metadata,
            fail_during_station_seed=fail_during_station_seed,
            fail_during_output_segment_seed=fail_during_output_segment_seed,
            trusted_internal=trusted_internal,
        )
        database_succeeded = True
    except QhhProductionBootstrapError as error:
        _persist_inactive_on_scheduler_visibility_blocker(resolved_database_url, error, model_id=model_id)
        raise
    except BasinsRegistryImportError as error:
        raise _from_registry_error(error, model_id=model_id) from error
    except Exception as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_DATABASE_ERROR",
            f"QHH production bootstrap database operation failed: {error.__class__.__name__}",
            model_id=model_id,
        ) from error
    finally:
        if evidence_reservation is not None and not database_succeeded:
            _cleanup_reserved_evidence_path(evidence_reservation, model_id=model_id)

    if evidence_reservation is not None:
        report = _finalize_evidence_after_commit(evidence_reservation, report, model_id=model_id)
    return report


def seed_qhh_forcing_stations(
    *,
    database_url: str,
    model_id: str = DEFAULT_QHH_MODEL_ID,
    project_name: str = DEFAULT_QHH_PROJECT_NAME,
    tsd_forc_path: str | Path,
    containment_root: str | Path | TrustedBasinsRoot,
) -> dict[str, Any]:
    root = _coerce_trusted_root(containment_root, role="qhh_tsd_forc")
    stations, checksum = read_qhh_tsd_forc(tsd_forc_path, root, model_id=model_id, project_name=project_name)
    with _transaction(database_url) as cursor:
        model = _fetch_model_identity(cursor, model_id)
        counts = _seed_station_rows(
            cursor,
            model=model,
            stations=stations,
            project_name=project_name,
            tsd_forc_path=Path(tsd_forc_path),
            tsd_forc_checksum=checksum,
        )
    return {
        "schema_version": "qhh.forcing_station_seed.v1",
        "status": "seeded",
        "model_id": model_id,
        "basin_version_id": model["basin_version_id"],
        "station_count": len(stations),
        "station_row_counts": counts,
        "source_file": str(Path(tsd_forc_path)),
        "source_sha256": checksum,
    }


def seed_qhh_output_segments(
    *,
    database_url: str,
    model_id: str = DEFAULT_QHH_MODEL_ID,
    project_name: str = DEFAULT_QHH_PROJECT_NAME,
    sp_riv_path: str | Path,
    containment_root: str | Path | TrustedBasinsRoot,
) -> dict[str, Any]:
    root = _coerce_trusted_root(containment_root, role="qhh_sp_riv")
    output_segment_count, checksum = read_qhh_output_segment_count(sp_riv_path, root, model_id=model_id)
    with _transaction(database_url) as cursor:
        model = _fetch_model_identity(cursor, model_id)
        # #2157 lock order: parent network row BEFORE the upsert takes row locks
        # on existing output segments. Locking only inside the trailing backfill
        # would turn this path into children -> parent and deadlock against an
        # autopipeline backfill (parent -> children) on the same network.
        _lock_river_network_version(cursor, model["river_network_version_id"])
        counts = _seed_output_segment_rows(
            cursor,
            model=model,
            project_name=project_name,
            output_segment_count=output_segment_count,
            sp_riv_path=Path(sp_riv_path),
            sp_riv_checksum=checksum,
        )
        geometry_backfilled_count = _backfill_output_segment_geometry(cursor, model["river_network_version_id"])
        _assert_complete_qhh_output_segment_stream_type(
            cursor,
            model["river_network_version_id"],
            model_id=model_id,
        )
        geometry_counts = _qhh_output_segment_geometry_counts(
            cursor,
            model["river_network_version_id"],
            geometry_backfilled_count=geometry_backfilled_count,
        )
    return {
        "schema_version": "qhh.output_segment_seed.v1",
        "status": "seeded",
        "model_id": model_id,
        "river_network_version_id": model["river_network_version_id"],
        "segment_count": output_segment_count,
        **geometry_counts,
        "output_segment_row_counts": counts,
        "source_file": str(Path(sp_riv_path)),
        "source_sha256": checksum,
    }


def _prepare_bootstrap_paths(
    *,
    basins_root: str | Path | None,
    qhh_basin_slug: str,
    qhh_project_name: str,
    model_id: str,
    package_version: str,
    inventory_path: str | Path | None,
    package_manifest_path: str | Path | None,
    work_dir: str | Path | None,
) -> QhhBootstrapPaths:
    resolved_root = _trusted_basins_root(resolve_basins_root(str(basins_root) if basins_root is not None else None))
    qhh_source_root = _resolve_qhh_source_root(
        resolved_root,
        qhh_basin_slug=qhh_basin_slug,
        qhh_project_name=qhh_project_name,
        model_id=model_id,
    )
    qhh_input_dir = _trusted_child_dir(
        qhh_source_root / "input" / qhh_project_name,
        resolved_root,
        model_id=model_id,
        role="qhh_input_dir",
    )
    tsd_forc_path = qhh_input_dir.path / f"{qhh_project_name}.tsd.forc"
    _require_contained_regular_file(tsd_forc_path, qhh_input_dir, model_id=model_id, role="qhh_tsd_forc")

    output_dir = _bootstrap_work_dir(work_dir, model_id=model_id)
    resolved_inventory_path = (
        Path(inventory_path).expanduser() if inventory_path is not None else output_dir / "inventory.json"
    )
    resolved_manifest_path = (
        Path(package_manifest_path).expanduser()
        if package_manifest_path is not None
        else output_dir / "package-manifest.json"
    )

    if inventory_path is None:
        inventory = _discover_qhh_inventory(resolved_root, model_id=model_id, qhh_source_root=qhh_source_root)
        _write_generated_json(resolved_inventory_path, inventory, model_id=model_id, role="inventory")
    else:
        _require_contained_optional_existing_json(
            resolved_inventory_path,
            containment_root=resolved_inventory_path.parent,
            model_id=model_id,
            role="inventory",
        )
    if package_manifest_path is None:
        try:
            publish_basins_package(
                inventory_path=resolved_inventory_path,
                model_id=model_id,
                version=package_version,
                output_path=resolved_manifest_path,
                copy_forcing=False,
            )
        except BasinsPackageError as error:
            raise _from_package_error(error, model_id=model_id) from error
    else:
        _require_contained_optional_existing_json(
            resolved_manifest_path,
            containment_root=resolved_manifest_path.parent,
            model_id=model_id,
            role="package_manifest",
        )

    return QhhBootstrapPaths(
        basins_root=resolved_root,
        inventory_path=resolved_inventory_path,
        package_manifest_path=resolved_manifest_path,
        qhh_source_root=qhh_source_root,
        qhh_input_dir=qhh_input_dir,
        tsd_forc_path=tsd_forc_path,
    )


def _discover_qhh_inventory(root: Path, *, model_id: str, qhh_source_root: Path) -> dict[str, Any]:
    _bounded_discovery_preflight(root, model_id=model_id, qhh_source_root=qhh_source_root)
    try:
        inventory = discover_basins_inventory(
            root,
            budget=DiscoveryBudget(
                max_depth=MAX_QHH_BOOTSTRAP_DISCOVERY_FILE_DEPTH,
                max_entries=MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES,
                error_code_prefix="QHH_BOOTSTRAP",
                root=root,
            ),
        )
    except BasinsDiscoveryError as error:
        raise QhhProductionBootstrapError(
            _qhh_code(error.error_code),
            str(error),
            model_id=model_id,
            path=error.path,
            details={"no_mutation_expected": True},
        ) from error
    matches = [
        model
        for model in inventory.get("models", [])
        if isinstance(model, dict) and model.get("model_id") == model_id
    ]
    if len(matches) != 1:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MODEL_DISCOVERY_MISMATCH",
            "QHH Basins discovery must find exactly one requested model.",
            model_id=model_id,
            path=str(root),
            details={"match_count": len(matches), "no_mutation_expected": True},
        )
    return inventory


def _bounded_discovery_preflight(root: Path, *, model_id: str, qhh_source_root: Path) -> None:
    try:
        root_stat = stat_no_follow(root)
    except (OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_BASINS_ROOT_UNSAFE",
            "QHH Basins root cannot be safely inspected.",
            model_id=model_id,
            path=str(root),
            details={"no_mutation_expected": True},
        ) from error
    if not stat.S_ISDIR(root_stat.st_mode):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_BASINS_ROOT_UNSAFE",
            "QHH Basins root must be a directory.",
            model_id=model_id,
            path=str(root),
            details={"no_mutation_expected": True},
        )
    stack: list[tuple[Path, int]] = [(root, 0)]
    entry_count = 0
    qhh_seen = False
    while stack:
        directory, depth = stack.pop()
        if depth > MAX_QHH_BOOTSTRAP_DISCOVERY_DEPTH:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_DISCOVERY_DEPTH_EXCEEDED",
                "QHH bootstrap discovery exceeded the allowed directory depth.",
                model_id=model_id,
                path=str(directory),
                details={
                    "max_depth": MAX_QHH_BOOTSTRAP_DISCOVERY_DEPTH,
                    "observed_depth": depth,
                    "no_mutation_expected": True,
                },
            )
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES:
                        raise QhhProductionBootstrapError(
                            "QHH_BOOTSTRAP_DISCOVERY_ENTRY_LIMIT_EXCEEDED",
                            "QHH bootstrap discovery exceeded the allowed entry count.",
                            model_id=model_id,
                            path=str(root),
                            details={
                                "max_entries": MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES,
                                "observed_entries": entry_count,
                                "no_mutation_expected": True,
                            },
                        )
                    child = directory / entry.name
                    try:
                        child_stat = stat_no_follow(child, containment_root=root)
                    except FileNotFoundError:
                        continue
                    except SafeFilesystemError as error:
                        raise QhhProductionBootstrapError(
                            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
                            "QHH bootstrap discovery encountered an unsafe path.",
                            model_id=model_id,
                            path=str(child),
                            details={"reason": error.kind, "no_mutation_expected": True},
                        ) from error
                    if not stat.S_ISDIR(child_stat.st_mode):
                        continue
                    try:
                        if child.resolve() == qhh_source_root:
                            qhh_seen = True
                    except OSError:
                        pass
                    if depth < MAX_QHH_BOOTSTRAP_DISCOVERY_DEPTH:
                        stack.append((child, depth + 1))
        except QhhProductionBootstrapError:
            raise
        except OSError as error:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_DISCOVERY_UNREADABLE",
                "QHH bootstrap discovery cannot read a directory.",
                model_id=model_id,
                path=str(directory),
                details={"no_mutation_expected": True},
            ) from error
    if not qhh_seen:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_NOT_FOUND",
            "QHH package source root was not found during bounded discovery.",
            model_id=model_id,
            path=str(qhh_source_root),
            details={"no_mutation_expected": True},
        )


def _bootstrap_database(
    context: QhhBootstrapContext,
    database_url: str,
    *,
    resource_profile_overrides: dict[str, Any],
    fail_after_model_metadata: bool,
    fail_during_station_seed: bool,
    fail_during_output_segment_seed: bool,
    trusted_internal: bool,
) -> dict[str, Any]:
    sources = context.sources
    model_id = sources.ids["model_id"]
    with _transaction(database_url) as cursor:
        _lock_qhh_basin_scope(cursor, sources.ids["basin_version_id"])
        duplicate_rows = _active_qhh_identity_rows(cursor, sources, model_id=model_id)
        if duplicate_rows:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_DUPLICATE_ACTIVE_MODEL",
                "More than one active QHH model identity is present before bootstrap.",
                model_id=model_id,
                details={"active_models": duplicate_rows, "no_downstream_mutation": True},
            )
        forcing_before = _dynamic_forcing_counts(cursor, model_id)
        del database_url, trusted_internal
        # Delegate the per-basin registry-core write to the shared sequence
        # owned by ``basins_registry_import.import_basin_into_registry_core``
        # so QHH bootstrap and the generic registry CLI populate
        # ``core.river_segment_crosswalk`` (PR 2 Path C contract) and run the
        # legacy purge in lock-step. Output-river seeding + geometry backfill
        # stay with the QHH-specific helpers below: they write QHH-specific
        # ``properties_json`` keys that diverge from the generic
        # ``_ensure_output_river_segments`` digest, so sharing that step would
        # otherwise trip ``BASINS_REGISTRY_CHECKSUM_CONFLICT`` on a second
        # bootstrap of the same QHH model.
        row_counts = import_basin_into_registry_core(
            cursor,
            sources,
            seed_output_river_segments=False,
            backfill_output_segment_geometry=False,
        )
        registry_report = _registry_report_from_row_counts(sources, row_counts)
        model_counts = _upsert_scheduler_ready_model(
            cursor,
            context,
            resource_profile_overrides=resource_profile_overrides,
        )
        if fail_after_model_metadata:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_PARTIAL_BOOTSTRAP_ROLLBACK",
                "Injected failure after model/package metadata for rollback verification.",
                model_id=model_id,
                details={"rollback_expected": True},
            )
        if fail_during_station_seed:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_PARTIAL_BOOTSTRAP_ROLLBACK",
                "Injected failure at station seeding for rollback verification.",
                model_id=model_id,
                details={"rollback_expected": True, "failure_point": "station_seed"},
            )
        station_counts = _seed_station_rows(
            cursor,
            model=_fetch_model_identity(cursor, model_id),
            stations=context.stations,
            project_name=str(sources.model["shud_input_name"]),
            tsd_forc_path=context.paths.tsd_forc_path,
            tsd_forc_checksum=context.tsd_forc_checksum,
        )
        if fail_during_output_segment_seed:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_PARTIAL_BOOTSTRAP_ROLLBACK",
                "Injected failure at output identity seeding for rollback verification.",
                model_id=model_id,
                details={"rollback_expected": True, "failure_point": "output_segment_seed"},
            )
        # #2157 lock order: `import_basin_into_registry_core` already UPDATEd the
        # parent row above, so this re-take is a same-transaction no-op; it keeps
        # "parent lock before the upsert" explicit and identical on both entry
        # points of `_seed_output_segment_rows`.
        _lock_river_network_version(cursor, sources.ids["river_network_version_id"])
        output_counts = _seed_output_segment_rows(
            cursor,
            model=_fetch_model_identity(cursor, model_id),
            project_name=str(sources.model["shud_input_name"]),
            output_segment_count=context.output_segment_count,
            sp_riv_path=context.paths.qhh_input_dir.path / f"{sources.model['shud_input_name']}.sp.riv",
            sp_riv_checksum=context.sp_riv_checksum,
        )
        geometry_backfilled_count = _backfill_output_segment_geometry(
            cursor,
            sources.ids["river_network_version_id"],
        )
        _assert_exact_qhh_station_set(cursor, context)
        _assert_exact_qhh_output_segment_set(cursor, context)
        output_geometry_counts = _assert_complete_qhh_output_segment_geometry(
            cursor,
            context,
            geometry_backfilled_count=geometry_backfilled_count,
        )
        _assert_complete_qhh_output_segment_stream_type(
            cursor,
            sources.ids["river_network_version_id"],
            model_id=model_id,
        )
        forcing_after = _dynamic_forcing_counts(cursor, model_id)
        _assert_dynamic_forcing_unchanged(model_id, before=forcing_before, after=forcing_after)
        _activate_qhh_model(cursor, model_id)
        active_model = _fetch_model_identity(cursor, model_id)

    return {
        "schema_version": QHH_BOOTSTRAP_SCHEMA_VERSION,
        "status": "bootstrapped",
        "model_id": model_id,
        "basin_id": active_model["basin_id"],
        "basin_version_id": active_model["basin_version_id"],
        "river_network_version_id": active_model["river_network_version_id"],
        "model_package_uri": active_model["model_package_uri"],
        "shud_code_version": active_model["shud_code_version"],
        "active": bool(active_model["active_flag"]),
        "lifecycle_state": active_model.get("lifecycle_state") or "active",
        "station_count": len(context.stations),
        "output_segment_count": context.output_segment_count,
        "geometry_backfilled_count": output_geometry_counts["geometry_backfilled_count"],
        "geometry_missing_count": output_geometry_counts["geometry_missing_count"],
        "registry_import": registry_report,
        "model_row_counts": model_counts,
        "station_row_counts": station_counts,
        "output_segment_row_counts": output_counts,
        "package_identity": {
            "manifest_uri": sources.manifest["manifest_uri"],
            "model_package_uri": sources.manifest["model_package_uri"],
            "package_checksum": sources.manifest["package_checksum"],
            "source_inventory_checksum": sources.manifest.get("source_inventory_checksum"),
            "manifest_sha256": hashlib.sha256(
                json.dumps(sources.manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
        },
        "source_files": {
            "qhh_tsd_forc": {
                "path": str(context.paths.tsd_forc_path),
                "sha256": context.tsd_forc_checksum,
                "station_count": len(context.stations),
            },
            "qhh_sp_riv": {
                "path": str(context.paths.qhh_input_dir.path / f"{sources.model['shud_input_name']}.sp.riv"),
                "sha256": context.sp_riv_checksum,
                "output_segment_count": context.output_segment_count,
            },
        },
        "scheduler_readiness": {
            "ready": True,
            "output_segment_geometry": output_geometry_counts,
            "required_fields": {
                "model_id": active_model["model_id"],
                "basin_id": active_model["basin_id"],
                "basin_version_id": active_model["basin_version_id"],
                "river_network_version_id": active_model["river_network_version_id"],
                "model_package_uri": active_model["model_package_uri"],
                "shud_code_version": active_model["shud_code_version"],
                "resource_profile_runnable": _json_dict(active_model["resource_profile"]).get("runnable"),
            },
        },
        "non_goal_proof": {
            "forcing_version_rows_created": forcing_after["forcing_version_count"]
            - forcing_before["forcing_version_count"],
            "forcing_station_timeseries_rows_created": forcing_after["forcing_station_timeseries_count"]
            - forcing_before["forcing_station_timeseries_count"],
            "shud_runtime_executed": False,
            "slurm_submitted": False,
            "published_display_artifacts": False,
        },
    }


def _seed_output_segment_rows(
    cursor: Any,
    *,
    model: dict[str, Any],
    project_name: str,
    output_segment_count: int,
    sp_riv_path: Path,
    sp_riv_checksum: str,
) -> dict[str, int]:
    from psycopg2.extras import execute_values

    order_offset = _output_segment_order_offset(cursor, model["river_network_version_id"])
    rows = []
    for index in range(1, output_segment_count + 1):
        segment_order = order_offset + index
        properties = _output_segment_expected_properties(
            model=model,
            project_name=project_name,
            index=index,
            sp_riv_path=sp_riv_path,
            sp_riv_checksum=sp_riv_checksum,
        )
        rows.append(
            (
                f"{model['model_id']}_shud_riv_{index:06d}",
                model["river_network_version_id"],
                segment_order,
                _json(properties),
                _output_segment_digest(
                    river_network_version_id=model["river_network_version_id"],
                    segment_order=segment_order,
                    properties_json=properties,
                ),
                properties,
            )
        )
    existing = _existing_output_segment_digests(
        cursor,
        model["river_network_version_id"],
        [row[0] for row in rows],
        expected_properties_by_id={row[0]: row[5] for row in rows},
    )
    created = sum(1 for row in rows if row[0] not in existing)
    unchanged = sum(1 for row in rows if existing.get(row[0]) == row[4])
    updated = len(rows) - created - unchanged
    if rows:
        execute_values(
            cursor,
            """
            INSERT INTO core.river_segment (
                river_segment_id,
                river_network_version_id,
                segment_order,
                properties_json
            )
            VALUES %s
            ON CONFLICT (river_segment_id, river_network_version_id) DO UPDATE
            SET segment_order = EXCLUDED.segment_order,
                properties_json = EXCLUDED.properties_json
            """,
            [row[:4] for row in rows],
            template="(%s, %s, %s, %s)",
            page_size=1000,
        )
    return {"created": created, "updated": updated, "unchanged": unchanged}


def _output_segment_order_offset(cursor: Any, river_network_version_id: str) -> int:
    cursor.execute(
        """
        SELECT COALESCE(MAX(segment_order), 0) AS order_offset
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'
        """,
        (river_network_version_id,),
    )
    return int(cursor.fetchone()["order_offset"] or 0)


# Reader #8 (#1990 task 7.2, routed by #1991 task 7.3). This reader does NOT
# join `met.forcing_version` at all — it counts fact rows by MODEL across every
# forcing version, so "look the store up by forcing version" does not map onto
# it. It is one of the two readers that SPAN both stores, and the only one whose
# templates had to change shape at 7.3.
#
# THE VARIANTS PROJECT ROWS, NOT A COUNT. Invariant I7 requires the two rendered
# fact-row subrelations to be composed inside the owning reader BEFORE any outer
# aggregate; a pair of `SELECT COUNT(*)` texts can only be combined by summing
# two answers in Python, which is exactly the shape the invariant forbids. So
# each variant emits one row per fact row (`SELECT 1 AS present`), the reader
# composes them, and ONE `COUNT(*)` runs over the composition. The projection is
# the same single column in the same order on both sides (I5).
#
# There is no store filter on either leg, and that is the difference from the
# display-coverage composition: this reader counts rows, not versions, and the
# two tables are disjoint per version by construction (000061 classified by row
# existence; the writers are narrow-only). A filter here would add a
# `met.forcing_version` join this reader deliberately does not have.
_DYNAMIC_FORCING_COUNT_TEMPLATES = ForcingTemplatePair(
    legacy=f"""
        SELECT 1 AS present
        FROM {FORCING_TABLE_TOKEN} fst
        JOIN met.met_station ms
          ON ms.station_id = fst.station_id
        WHERE ms.properties_json->>'model_id' = %s
        """,
    narrow=f"""
        SELECT 1 AS present
        FROM {FORCING_TABLE_TOKEN} fst
        JOIN met.met_station ms
          ON ms.station_key = fst.station_key
        WHERE ms.properties_json->>'model_id' = %s
        """,
)


def _dynamic_forcing_count_sql() -> str:
    """One count over both stores' fact rows, composed inside this reader (I7)."""
    legs = [
        render_forcing_ts_sql(
            _DYNAMIC_FORCING_COUNT_TEMPLATES,
            store,
            entry="qhh_production_bootstrap.dynamic_forcing_count",
        ).sql
        for store in FORCING_STORES
    ]
    return "SELECT COUNT(*) AS count FROM (" + " UNION ALL ".join(legs) + ") AS forcing_rows"


def _dynamic_forcing_counts(cursor: Any, model_id: str) -> dict[str, int]:
    cursor.execute("SELECT COUNT(*) AS count FROM met.forcing_version WHERE model_id = %s", (model_id,))
    forcing_versions = int(cursor.fetchone()["count"])
    # Both legs bind the same `model_id`, in the order `FORCING_STORES` spells.
    cursor.execute(_dynamic_forcing_count_sql(), (model_id, model_id))
    timeseries_rows = int(cursor.fetchone()["count"])
    return {
        "forcing_version_count": forcing_versions,
        "forcing_station_timeseries_count": timeseries_rows,
    }


def _finalize_evidence_after_commit(
    reservation: QhhEvidenceReservation,
    report: dict[str, Any],
    *,
    model_id: str,
) -> dict[str, Any]:
    try:
        _write_reserved_evidence_path(reservation, report, model_id=model_id)
    except QhhProductionBootstrapError as error:
        _close_reserved_evidence_fd(reservation)
        try:
            _unlink_reserved_evidence_path(
                reservation.target,
                reservation.root,
                model_id=model_id,
                expected_identity=reservation.identity,
            )
        except QhhProductionBootstrapError as cleanup_error:
            error.details["cleanup_error"] = cleanup_error.to_payload()
        return {
            **report,
            "evidence_path": str(reservation.target),
            "evidence_write_omitted": True,
            "evidence_write_error": error.to_payload(),
        }
    return {
        **report,
        "evidence_path": str(reservation.target),
        "evidence_write_omitted": False,
    }
