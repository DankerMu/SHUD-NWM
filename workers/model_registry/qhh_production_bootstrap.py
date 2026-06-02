from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
    unlink_no_follow,
    verify_directory_no_follow,
)

from .basins_discovery import BasinsDiscoveryError, discover_basins_inventory, resolve_basins_root
from .basins_geometry import BasinsGeometryError, TrustedBasinsRoot, trusted_basins_root
from .basins_package import BasinsPackageError, publish_basins_package
from .basins_registry_import import (
    BasinsRegistryImportError,
    ImportSources,
    _ensure_basin,
    _ensure_basin_version,
    _ensure_mesh,
    _ensure_model_instance,
    _ensure_river_network,
    _ensure_river_segments,
    _fetch_optional,
    _find_inventory_model,
    _input_dir,
    _inventory_root,
    _json,
    _json_dict,
    _prepare_sources,
    _recorded_relative_inventory_root,
    _source_root,
    _transaction,
)

QHH_BOOTSTRAP_SCHEMA_VERSION = "qhh.production_bootstrap.v1"
DEFAULT_QHH_PROJECT_NAME = "qhh"
DEFAULT_QHH_MODEL_ID = "basins_qhh_shud"
DEFAULT_QHH_BASIN_SLUG = "qhh"
DEFAULT_QHH_PACKAGE_VERSION = "vbasins-qhh-production"
DEFAULT_QHH_SHUD_CODE_VERSION = "basins-shud"
MAX_QHH_BOOTSTRAP_DISCOVERY_DEPTH = 3
MAX_QHH_BOOTSTRAP_DISCOVERY_ENTRIES = 2048
MAX_QHH_TSD_FORC_BYTES = 8 * 1024 * 1024
MAX_QHH_TSD_FORC_STATIONS = 250_000
MAX_QHH_JSON_BYTES = 16 * 1024 * 1024
MAX_QHH_CHECKSUM_BYTES = 4096
MAX_QHH_SP_RIV_BYTES = 8 * 1024 * 1024
MAX_QHH_OUTPUT_SEGMENTS = 250_000
_EVIDENCE_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_EVIDENCE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(
    os,
    "O_CLOEXEC",
    0,
)


class QhhProductionBootstrapError(RuntimeError):
    """Raised when QHH production bootstrap cannot safely finish."""

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
class QhhBootstrapPaths:
    basins_root: Path
    inventory_path: Path
    package_manifest_path: Path
    qhh_source_root: Path
    qhh_input_dir: TrustedBasinsRoot
    tsd_forc_path: Path


@dataclass(frozen=True)
class QhhForcingStation:
    station_id: str
    station_name: str
    forcing_index: int
    longitude: float
    latitude: float
    x: float
    y: float
    z: float
    elevation_m: float
    forcing_filename: str
    original_id: str


@dataclass(frozen=True)
class QhhBootstrapContext:
    sources: ImportSources
    paths: QhhBootstrapPaths
    stations: tuple[QhhForcingStation, ...]
    output_segment_count: int
    tsd_forc_checksum: str
    sp_riv_checksum: str
    shud_code_version: str


@dataclass(frozen=True)
class QhhPreflightSources:
    inventory: dict[str, Any]
    manifest: dict[str, Any]
    model: dict[str, Any]
    input_dir: TrustedBasinsRoot
    source_root: Path
    inventory_raw_checksum: str


@dataclass
class QhhEvidenceReservation:
    root: Path
    target: Path
    fd: int
    identity: tuple[int, int, int]
    closed: bool = False


QHH_RESOURCE_PROFILE_OVERRIDE_ALLOWED_FIELDS = frozenset(
    {
        "scheduler",
        "partition",
        "account",
        "nodes",
        "ntasks",
        "cpus_per_task",
        "memory_mb",
        "memory_gb",
        "walltime",
        "walltime_minutes",
        "max_concurrent",
        "shud_threads",
        "slurm_env",
        "display_capabilities",
        "frequency_capabilities",
        "resource_profile_id",
    }
)


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
    except QhhProductionBootstrapError:
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


def read_qhh_tsd_forc(
    path: str | Path,
    containment_root: Path | TrustedBasinsRoot,
    *,
    model_id: str = DEFAULT_QHH_MODEL_ID,
    project_name: str = DEFAULT_QHH_PROJECT_NAME,
) -> tuple[tuple[QhhForcingStation, ...], str]:
    root = _coerce_trusted_root(containment_root, role="qhh_tsd_forc")
    source = Path(path).expanduser()
    content = _read_contained_file_limited(
        source,
        root,
        max_bytes=MAX_QHH_TSD_FORC_BYTES,
        error_code="QHH_BOOTSTRAP_TSD_FORC_OVERSIZED",
        model_id=model_id,
        role="qhh_tsd_forc",
    )
    checksum = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc is not valid UTF-8 text.",
            model_id=model_id,
            path=str(source),
            details={"no_mutation_expected": True},
        ) from error

    raw_lines = text.splitlines()
    lines = [line.strip() for line in raw_lines if line.strip()]
    if len(lines) < 4:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc must include a count header, metadata lines, and station rows.",
            model_id=model_id,
            path=str(source),
            details={"line_count": len(lines), "no_mutation_expected": True},
        )
    try:
        expected_count = int(lines[0].split()[0])
    except (IndexError, ValueError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc first token must be the forcing station count.",
            model_id=model_id,
            path=str(source),
            details={"header": lines[0][:120], "no_mutation_expected": True},
        ) from error
    if expected_count < 1 or expected_count > MAX_QHH_TSD_FORC_STATIONS:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_STATION_COUNT_INVALID",
            "QHH forcing station count is outside bootstrap bounds.",
            model_id=model_id,
            path=str(source),
            details={
                "expected_count": expected_count,
                "max_station_count": MAX_QHH_TSD_FORC_STATIONS,
                "no_mutation_expected": True,
            },
        )

    stations: list[QhhForcingStation] = []
    malformed_rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(lines[3:], start=4):
        parts = raw.split()
        if len(parts) < 7:
            malformed_rows.append({"line_number": line_number, "reason": "too_few_columns"})
            continue
        forcing_index = _parse_tsd_forc_station_index(parts[0])
        forcing_filename = _parse_tsd_forc_filename(parts[6])
        if forcing_index is None:
            malformed_rows.append({"line_number": line_number, "reason": "invalid_forcing_index"})
            continue
        if forcing_filename is None:
            malformed_rows.append({"line_number": line_number, "reason": "invalid_forcing_filename"})
            continue
        try:
            longitude = float(parts[1])
            latitude = float(parts[2])
            x = float(parts[3])
            y = float(parts[4])
            z = float(parts[5])
        except ValueError:
            malformed_rows.append({"line_number": line_number, "reason": "non_numeric_column"})
            continue
        if not (
            math.isfinite(longitude)
            and math.isfinite(latitude)
            and math.isfinite(x)
            and math.isfinite(y)
            and math.isfinite(z)
        ):
            malformed_rows.append({"line_number": line_number, "reason": "non_finite_xyz"})
            continue
        if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
            malformed_rows.append({"line_number": line_number, "reason": "invalid_lon_lat"})
            continue
        station_id = f"qhh_forc_{forcing_index:03d}"
        stations.append(
            QhhForcingStation(
                station_id=station_id,
                station_name=f"QHH forcing station {forcing_index:03d}",
                forcing_index=forcing_index,
                longitude=longitude,
                latitude=latitude,
                x=x,
                y=y,
                z=z,
                elevation_m=0.0 if z <= -9990 else z,
                forcing_filename=forcing_filename,
                original_id=parts[0],
            )
        )
    index_counts = Counter(station.forcing_index for station in stations)
    duplicate_indexes = sorted(index for index, count in index_counts.items() if count > 1)
    if malformed_rows:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc contains malformed forcing station rows.",
            model_id=model_id,
            path=str(source),
            details={"malformed_rows": malformed_rows[:20], "no_mutation_expected": True},
        )
    if duplicate_indexes:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_TSD_FORC_MALFORMED",
            "QHH qhh.tsd.forc contains duplicate forcing station indexes.",
            model_id=model_id,
            path=str(source),
            details={"duplicate_forcing_indexes": duplicate_indexes[:20], "no_mutation_expected": True},
        )
    if len(stations) != expected_count:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_STATION_COUNT_MISMATCH",
            "QHH forcing station row count does not match qhh.tsd.forc header.",
            model_id=model_id,
            path=str(source),
            details={
                "expected_count": expected_count,
                "parsed_count": len(stations),
                "no_mutation_expected": True,
            },
        )
    stations.sort(key=lambda item: item.forcing_index)
    return tuple(stations), checksum


def _parse_tsd_forc_station_index(value: str) -> int | None:
    if not value or not value.isascii() or not value.isdecimal():
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 1 or parsed > MAX_QHH_TSD_FORC_STATIONS:
        return None
    return parsed


def _parse_tsd_forc_filename(value: str) -> str | None:
    if not value or "\x00" in value or value in {".", ".."} or "\\" in value:
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        return None
    part = candidate.parts[0]
    if part in {"", ".", ".."}:
        return None
    return part


def read_qhh_output_segment_count(
    path: str | Path,
    containment_root: Path | TrustedBasinsRoot,
    *,
    model_id: str = DEFAULT_QHH_MODEL_ID,
) -> tuple[int, str]:
    root = _coerce_trusted_root(containment_root, role="qhh_sp_riv")
    source = Path(path).expanduser()
    content = _read_contained_file_limited(
        source,
        root,
        max_bytes=MAX_QHH_SP_RIV_BYTES,
        error_code="QHH_BOOTSTRAP_SP_RIV_OVERSIZED",
        model_id=model_id,
        role="qhh_sp_riv",
    )
    checksum = hashlib.sha256(content).hexdigest()
    try:
        first_line = next(line.strip() for line in content.decode("utf-8").splitlines() if line.strip())
        count = int(first_line.split()[0])
    except (StopIteration, UnicodeDecodeError, IndexError, ValueError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SP_RIV_MALFORMED",
            "QHH SHUD river output identity file has an invalid count header.",
            model_id=model_id,
            path=str(source),
            details={"no_mutation_expected": True},
        ) from error
    if count < 1 or count > MAX_QHH_OUTPUT_SEGMENTS:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_OUTPUT_SEGMENT_COUNT_INVALID",
            "QHH SHUD output river count is outside bootstrap bounds.",
            model_id=model_id,
            path=str(source),
            details={
                "output_segment_count": count,
                "max_output_segment_count": MAX_QHH_OUTPUT_SEGMENTS,
                "no_mutation_expected": True,
            },
        )
    return count, checksum


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
        counts = _seed_output_segment_rows(
            cursor,
            model=model,
            project_name=project_name,
            output_segment_count=output_segment_count,
            sp_riv_path=Path(sp_riv_path),
            sp_riv_checksum=checksum,
        )
    return {
        "schema_version": "qhh.output_segment_seed.v1",
        "status": "seeded",
        "model_id": model_id,
        "river_network_version_id": model["river_network_version_id"],
        "segment_count": output_segment_count,
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
        inventory = discover_basins_inventory(root)
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


def _prepare_preflight_sources_from_bounded_json(
    inventory_path: Path,
    manifest_path: Path,
    *,
    model_id: str,
) -> QhhPreflightSources:
    inventory, inventory_bytes = _read_json_object_bounded(
        inventory_path,
        max_bytes=MAX_QHH_JSON_BYTES,
        error_code="QHH_BOOTSTRAP_INVENTORY_INVALID",
        not_found_code="QHH_BOOTSTRAP_INVENTORY_NOT_FOUND",
        model_id=model_id,
    )
    manifest, _manifest_bytes = _read_json_object_bounded(
        manifest_path,
        max_bytes=MAX_QHH_JSON_BYTES,
        error_code="QHH_BOOTSTRAP_PACKAGE_MANIFEST_INVALID",
        not_found_code="QHH_BOOTSTRAP_PACKAGE_MANIFEST_NOT_FOUND",
        model_id=model_id,
    )
    try:
        manifest_model_id = manifest.get("model_id")
        if not isinstance(manifest_model_id, str) or not manifest_model_id:
            raise BasinsRegistryImportError(
                "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
                "Required field is missing: model_id",
                model_id=model_id,
            )
        if manifest_model_id != model_id:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_MODEL_ID_MISMATCH",
                "QHH package manifest model_id does not match requested bootstrap model_id.",
                model_id=model_id,
                details={"actual_model_id": manifest_model_id, "no_mutation_expected": True},
            )
        model = _find_inventory_model(inventory, model_id)
        inventory_root = _inventory_root(inventory, model_id)
        inventory_relative_root = _recorded_relative_inventory_root(inventory)
        source_root = _source_root(inventory_root, inventory_relative_root, model, model_id)
        input_dir = _input_dir(inventory_root, inventory_relative_root, source_root, model, model_id)
    except QhhProductionBootstrapError:
        raise
    except BasinsRegistryImportError as error:
        raise _from_registry_error(error, model_id=model_id) from error
    return QhhPreflightSources(
        inventory=inventory,
        manifest=manifest,
        model=model,
        input_dir=input_dir,
        source_root=source_root,
        inventory_raw_checksum=hashlib.sha256(inventory_bytes).hexdigest(),
    )


def _prepare_sources_from_preflight(preflight_sources: QhhPreflightSources, *, model_id: str) -> ImportSources:
    try:
        return _prepare_sources(
            preflight_sources.inventory,
            preflight_sources.manifest,
            inventory_raw_checksum=preflight_sources.inventory_raw_checksum,
        )
    except BasinsRegistryImportError as error:
        raise _from_registry_error(error, model_id=model_id) from error


def _prepare_sources_from_bounded_json(inventory_path: Path, manifest_path: Path, *, model_id: str) -> ImportSources:
    preflight_sources = _prepare_preflight_sources_from_bounded_json(
        inventory_path,
        manifest_path,
        model_id=model_id,
    )
    return _prepare_sources_from_preflight(preflight_sources, model_id=model_id)


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
        row_counts = {
            "basin": _ensure_basin(cursor, sources),
            "basin_version": _ensure_basin_version(cursor, sources),
            "river_network_version": _ensure_river_network(cursor, sources),
            "river_segment": _ensure_river_segments(cursor, sources),
            "mesh_version": _ensure_mesh(cursor, sources),
            "model_instance": _ensure_model_instance(cursor, sources),
        }
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
        output_counts = _seed_output_segment_rows(
            cursor,
            model=_fetch_model_identity(cursor, model_id),
            project_name=str(sources.model["shud_input_name"]),
            output_segment_count=context.output_segment_count,
            sp_riv_path=context.paths.qhh_input_dir.path / f"{sources.model['shud_input_name']}.sp.riv",
            sp_riv_checksum=context.sp_riv_checksum,
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


def _upsert_scheduler_ready_model(
    cursor: Any,
    context: QhhBootstrapContext,
    *,
    resource_profile_overrides: dict[str, Any],
) -> dict[str, int]:
    sources = context.sources
    ids = sources.ids
    expected_profile = _scheduler_ready_resource_profile(context, resource_profile_overrides=resource_profile_overrides)
    existing = _fetch_optional(
        cursor,
        """
        SELECT model_id,
               shud_code_version,
               model_package_uri,
               active_flag,
               COALESCE(lifecycle_state, CASE WHEN active_flag THEN 'active' ELSE 'inactive' END) AS lifecycle_state,
               resource_profile
        FROM core.model_instance
        WHERE model_id = %s
        FOR UPDATE
        """,
        (ids["model_id"],),
    )
    if existing is None:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MODEL_INSTANCE_MISSING",
            "Basins registry import did not create the expected QHH model instance.",
            model_id=ids["model_id"],
        )
    existing_profile = _json_dict(existing["resource_profile"])
    merged_profile = {**existing_profile, **expected_profile}
    updates: list[str] = []
    params: list[Any] = []
    if existing["shud_code_version"] != context.shud_code_version:
        updates.append("shud_code_version = %s")
        params.append(context.shud_code_version)
    if existing["model_package_uri"] != sources.manifest["model_package_uri"]:
        updates.append("model_package_uri = %s")
        params.append(sources.manifest["model_package_uri"])
    if existing_profile != merged_profile:
        updates.append("resource_profile = %s")
        params.append(_json(merged_profile))
    if updates:
        cursor.execute(
            f"""
            UPDATE core.model_instance
            SET {", ".join(updates)}
            WHERE model_id = %s
            """,
            (*params, ids["model_id"]),
        )
        return {"created": 0, "updated": 1, "unchanged": 0}
    return {"created": 0, "updated": 0, "unchanged": 1}


def _registry_report_from_row_counts(sources: ImportSources, row_counts: dict[str, int]) -> dict[str, Any]:
    status = "already_imported" if all(count == 0 for count in row_counts.values()) else "imported"
    return {
        "schema_version": "basins.registry_import.v1",
        "status": status,
        "model_id": sources.ids["model_id"],
        "basin_id": sources.ids["basin_id"],
        "basin_version_id": sources.ids["basin_version_id"],
        "river_network_version_id": sources.ids["river_network_version_id"],
        "mesh_version_id": sources.ids["mesh_version_id"],
        "active": False,
        "segment_count": sources.geometry.segment_count,
        "row_counts": row_counts,
        "model_package_uri": sources.manifest["model_package_uri"],
        "manifest_uri": sources.manifest["manifest_uri"],
        "package_checksum": sources.manifest["package_checksum"],
    }


def _scheduler_ready_resource_profile(
    context: QhhBootstrapContext,
    *,
    resource_profile_overrides: dict[str, Any],
) -> dict[str, Any]:
    sources = context.sources
    profile = {
        "runnable": True,
        "lineage": "qhh_production_bootstrap",
        "resource_profile_id": "qhh-production-default",
        "scheduler": "slurm",
        "partition": os.getenv("NHMS_QHH_SLURM_PARTITION", "standard"),
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": int(os.getenv("NHMS_QHH_CPUS_PER_TASK", "4")),
        "memory_mb": int(os.getenv("NHMS_QHH_MEMORY_MB", "8192")),
        "memory_gb": int(os.getenv("NHMS_QHH_MEMORY_GB", "8")),
        "walltime_minutes": int(os.getenv("NHMS_QHH_WALLTIME_MINUTES", "720")),
        "display_capabilities": {"q_down": True, "tiles": True},
        "frequency_capabilities": {"return_periods": False},
        "model_id": sources.ids["model_id"],
        "basin_id": sources.ids["basin_id"],
        "basin_version_id": sources.ids["basin_version_id"],
        "river_network_version_id": sources.ids["river_network_version_id"],
        "mesh_version_id": sources.ids["mesh_version_id"],
        "project_name": sources.model["shud_input_name"],
        "shud_input_name": sources.model["shud_input_name"],
        "basin_slug": sources.model["basin_slug"],
        "manifest_uri": sources.manifest["manifest_uri"],
        "model_package_uri": sources.manifest["model_package_uri"],
        "package_checksum": sources.manifest["package_checksum"],
        "source_inventory_checksum": sources.manifest.get("source_inventory_checksum"),
        "source_inventory_schema_version": sources.manifest.get("source_inventory_schema_version"),
        "station_count": len(context.stations),
        "output_segment_count": context.output_segment_count,
        "qhh_tsd_forc_sha256": context.tsd_forc_checksum,
        "qhh_sp_riv_sha256": context.sp_riv_checksum,
    }
    profile.update(_safe_resource_profile_overrides(resource_profile_overrides))
    return profile


def _safe_resource_profile_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(overrides).items() if key in QHH_RESOURCE_PROFILE_OVERRIDE_ALLOWED_FIELDS}


def _seed_station_rows(
    cursor: Any,
    *,
    model: dict[str, Any],
    stations: Sequence[QhhForcingStation],
    project_name: str,
    tsd_forc_path: Path,
    tsd_forc_checksum: str,
) -> dict[str, int]:
    from psycopg2.extras import execute_values

    rows = []
    for station in stations:
        expected_properties = {
            "seed": "qhh_production_bootstrap",
            "model_id": model["model_id"],
            "basin_id": model["basin_id"],
            "basin_version_id": model["basin_version_id"],
            "project_name": project_name,
            "source": "qhh.tsd.forc",
            "source_file": str(tsd_forc_path),
            "source_sha256": tsd_forc_checksum,
            "shud_forcing_index": station.forcing_index,
            "forcing_filename": station.forcing_filename,
            "forcing_source_identity": f"{project_name}.tsd.forc:{station.forcing_index}:{station.forcing_filename}",
            "original_id": station.original_id,
            "x": station.x,
            "y": station.y,
            "z": station.z,
            "elevation_metadata": {
                "source": "qhh.tsd.forc",
                "raw_z": station.z,
                "normalized_missing_to_zero": station.z <= -9990,
            },
        }
        rows.append(
            (
                station.station_id,
                model["basin_version_id"],
                station.station_name,
                station.longitude,
                station.latitude,
                station.elevation_m,
                "forcing_grid",
                True,
                _json(expected_properties),
                _station_digest(
                    basin_version_id=model["basin_version_id"],
                    station_name=station.station_name,
                    lon=station.longitude,
                    lat=station.latitude,
                    elevation_m=station.elevation_m,
                    station_role="forcing_grid",
                    active_flag=True,
                    properties_json=expected_properties,
                ),
            )
        )
    existing = _existing_station_digests(cursor, [row[0] for row in rows])
    created = sum(1 for row in rows if row[0] not in existing)
    unchanged = sum(1 for row in rows if existing.get(row[0]) == row[9])
    updated = len(rows) - created - unchanged
    if rows:
        execute_values(
            cursor,
            """
            INSERT INTO met.met_station (
                station_id,
                basin_version_id,
                station_name,
                geom,
                elevation_m,
                station_role,
                active_flag,
                properties_json
            )
            VALUES %s
            ON CONFLICT (station_id) DO UPDATE
            SET basin_version_id = EXCLUDED.basin_version_id,
                station_name = EXCLUDED.station_name,
                geom = EXCLUDED.geom,
                elevation_m = EXCLUDED.elevation_m,
                station_role = EXCLUDED.station_role,
                active_flag = EXCLUDED.active_flag,
                properties_json = EXCLUDED.properties_json
            """,
            [row[:9] for row in rows],
            template="(%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4490), %s, %s, %s, %s)",
            page_size=1000,
        )
    return {"created": created, "updated": updated, "unchanged": unchanged}


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
        _backfill_output_segment_geometry(cursor, model["river_network_version_id"])
    return {"created": created, "updated": updated, "unchanged": unchanged}


def _output_segment_expected_properties(
    *,
    model: dict[str, Any],
    project_name: str,
    index: int,
    sp_riv_path: Path,
    sp_riv_checksum: str,
) -> dict[str, Any]:
    return {
        "seed": "qhh_production_bootstrap",
        "model_id": model["model_id"],
        "basin_id": model["basin_id"],
        "basin_version_id": model["basin_version_id"],
        "shud_output_river": True,
        "shud_riv_index": index,
        "source": f"{project_name}.sp.riv",
        "source_file": str(sp_riv_path),
        "source_sha256": sp_riv_checksum,
        "geometry_source": "gis_rivseg_iRiv",
        "output_identity": f"{project_name}.sp.riv:{index}",
    }


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


def _backfill_output_segment_geometry(cursor: Any, river_network_version_id: str) -> None:
    cursor.execute(
        """
        WITH gis_points AS (
            SELECT
                (properties_json->>'source_raw_segment_id')::int AS shud_riv_index,
                segment_order,
                length_m,
                (dump).path[1] AS point_order,
                (dump).geom AS point_geom
            FROM (
                SELECT properties_json, segment_order, length_m, ST_DumpPoints(geom) AS dump
                FROM core.river_segment
                WHERE river_network_version_id = %s
                  AND geom IS NOT NULL
                  AND COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'
                  AND properties_json ? 'source_raw_segment_id'
                  AND (properties_json->>'source_raw_segment_id') ~ '^[0-9]+$'
            ) source
        ),
        numbered_points AS (
            SELECT
                shud_riv_index,
                segment_order,
                length_m,
                point_order,
                point_geom,
                LAG(ST_AsEWKB(point_geom)) OVER (
                    PARTITION BY shud_riv_index
                    ORDER BY segment_order, point_order
                ) AS previous_point
            FROM gis_points
        ),
        deduped_points AS (
            SELECT *
            FROM numbered_points
            WHERE previous_point IS NULL
               OR previous_point <> ST_AsEWKB(point_geom)
        ),
        gis_by_riv AS (
            SELECT
                shud_riv_index,
                ST_MakeLine(point_geom ORDER BY segment_order, point_order)::geometry(LineString, 4490) AS geom,
                SUM(DISTINCT length_m) AS length_m,
                COUNT(DISTINCT segment_order) AS source_segment_count
            FROM deduped_points
            GROUP BY shud_riv_index
            HAVING COUNT(*) >= 2
        )
        UPDATE core.river_segment target
        SET geom = gis.geom,
            length_m = gis.length_m,
            properties_json = target.properties_json
                || jsonb_build_object(
                    'geometry_source', 'gis_rivseg_iRiv',
                    'geometry_source_segment_count', gis.source_segment_count,
                    'geometry_source_length_m', gis.length_m
                )
        FROM gis_by_riv gis
        WHERE target.river_network_version_id = %s
          AND COALESCE(target.properties_json->>'shud_output_river', 'false') = 'true'
          AND (target.properties_json->>'shud_riv_index')::int = gis.shud_riv_index
        """,
        (river_network_version_id, river_network_version_id),
    )


def _activate_qhh_model(cursor: Any, model_id: str) -> None:
    cursor.execute(
        """
        UPDATE core.model_instance
        SET active_flag = true,
            lifecycle_state = 'active'
        WHERE model_id = %s
        """,
        (model_id,),
    )


def _lock_qhh_basin_scope(cursor: Any, basin_version_id: str) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"qhh-bootstrap:{basin_version_id}",))
    cursor.execute(
        """
        SELECT basin_version_id
        FROM core.basin_version
        WHERE basin_version_id = %s
        FOR UPDATE
        """,
        (basin_version_id,),
    )


def _active_qhh_identity_rows(cursor: Any, sources: ImportSources, *, model_id: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT mi.model_id,
               bv.basin_id,
               mi.basin_version_id,
               mi.river_network_version_id,
               mi.model_package_uri,
               mi.resource_profile,
               CASE
                 WHEN mi.model_id = %s THEN 'model_id'
                 WHEN bv.basin_id = %s THEN 'basin_id'
                 WHEN mi.basin_version_id = %s THEN 'basin_version_id'
                 WHEN mi.river_network_version_id = %s THEN 'river_network_version_id'
                 WHEN mi.model_package_uri = %s THEN 'model_package_uri'
                 WHEN mi.resource_profile->>'package_checksum' = %s THEN 'package_checksum'
                 WHEN mi.resource_profile->>'source_inventory_checksum' = %s THEN 'source_inventory_checksum'
                 WHEN mi.resource_profile->>'basin_slug' = %s THEN 'basin_slug'
                 WHEN mi.resource_profile->>'project_name' = %s THEN 'project_name'
                 WHEN mi.resource_profile->>'shud_input_name' = %s THEN 'shud_input_name'
                 ELSE 'unknown'
               END AS duplicate_reason
        FROM core.model_instance mi
        JOIN core.basin_version bv
          ON bv.basin_version_id = mi.basin_version_id
        WHERE mi.active_flag = true
          AND COALESCE(mi.lifecycle_state, 'active') = 'active'
          AND (
            mi.model_id = %s
            OR bv.basin_id = %s
            OR mi.basin_version_id = %s
            OR mi.river_network_version_id = %s
            OR mi.model_package_uri = %s
            OR mi.resource_profile->>'package_checksum' = %s
            OR mi.resource_profile->>'source_inventory_checksum' = %s
            OR mi.resource_profile->>'basin_slug' = %s
            OR mi.resource_profile->>'project_name' = %s
            OR mi.resource_profile->>'shud_input_name' = %s
          )
        ORDER BY mi.model_id
        """,
        (
            model_id,
            sources.ids["basin_id"],
            sources.ids["basin_version_id"],
            sources.ids["river_network_version_id"],
            sources.manifest["model_package_uri"],
            sources.manifest.get("package_checksum"),
            sources.manifest.get("source_inventory_checksum"),
            sources.model["basin_slug"],
            sources.model["shud_input_name"],
            sources.model["shud_input_name"],
            model_id,
            sources.ids["basin_id"],
            sources.ids["basin_version_id"],
            sources.ids["river_network_version_id"],
            sources.manifest["model_package_uri"],
            sources.manifest.get("package_checksum"),
            sources.manifest.get("source_inventory_checksum"),
            sources.model["basin_slug"],
            sources.model["shud_input_name"],
            sources.model["shud_input_name"],
        ),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    if len(rows) <= 1 and (not rows or rows[0]["model_id"] == model_id):
        return []
    return [
        {
            "model_id": str(row["model_id"]),
            "basin_id": str(row["basin_id"]),
            "basin_version_id": str(row["basin_version_id"]),
            "river_network_version_id": str(row["river_network_version_id"]),
            "duplicate_reason": str(row.get("duplicate_reason") or "unknown"),
        }
        for row in rows
    ]


def _fetch_model_identity(cursor: Any, model_id: str) -> dict[str, Any]:
    row = _fetch_optional(
        cursor,
        """
        SELECT mi.model_id,
               bv.basin_id,
               mi.basin_version_id,
               mi.river_network_version_id,
               mi.mesh_version_id,
               mi.shud_code_version,
               mi.model_package_uri,
               mi.active_flag,
               COALESCE(mi.lifecycle_state, CASE WHEN mi.active_flag THEN 'active' ELSE 'inactive' END)
                    AS lifecycle_state,
               mi.resource_profile
        FROM core.model_instance mi
        JOIN core.basin_version bv
          ON bv.basin_version_id = mi.basin_version_id
        WHERE mi.model_id = %s
        """,
        (model_id,),
    )
    if row is None:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MODEL_INSTANCE_MISSING",
            "QHH model instance is missing.",
            model_id=model_id,
        )
    return row


def _dynamic_forcing_counts(cursor: Any, model_id: str) -> dict[str, int]:
    cursor.execute("SELECT COUNT(*) AS count FROM met.forcing_version WHERE model_id = %s", (model_id,))
    forcing_versions = int(cursor.fetchone()["count"])
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM met.forcing_station_timeseries fst
        JOIN met.met_station ms
          ON ms.station_id = fst.station_id
        WHERE ms.properties_json->>'model_id' = %s
        """,
        (model_id,),
    )
    timeseries_rows = int(cursor.fetchone()["count"])
    return {
        "forcing_version_count": forcing_versions,
        "forcing_station_timeseries_count": timeseries_rows,
    }


def _assert_dynamic_forcing_unchanged(model_id: str, *, before: dict[str, int], after: dict[str, int]) -> None:
    if before != after:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_DYNAMIC_FORCING_PRESENT",
            "QHH bootstrap must not create future-cycle forcing rows in this task.",
            model_id=model_id,
            details={"before": before, "after": after},
        )


def _existing_station_digests(cursor: Any, station_ids: Sequence[str]) -> dict[str, str]:
    if not station_ids:
        return {}
    cursor.execute(
        """
        SELECT station_id,
               basin_version_id,
               station_name,
               ST_X(geom) AS lon,
               ST_Y(geom) AS lat,
               elevation_m,
               station_role,
               active_flag,
               properties_json
        FROM met.met_station
        WHERE station_id = ANY(%s)
        """,
        (list(station_ids),),
    )
    return {
        str(row["station_id"]): _station_digest(
            basin_version_id=row["basin_version_id"],
            station_name=row["station_name"],
            lon=row["lon"],
            lat=row["lat"],
            elevation_m=row["elevation_m"],
            station_role=row["station_role"],
            active_flag=bool(row["active_flag"]),
            properties_json=_json_dict(row["properties_json"]),
        )
        for row in cursor.fetchall()
    }


def _existing_output_segment_digests(
    cursor: Any,
    river_network_version_id: str,
    river_segment_ids: Sequence[str],
    *,
    expected_properties_by_id: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    if not river_segment_ids:
        return {}
    cursor.execute(
        """
        SELECT river_segment_id,
               river_network_version_id,
               segment_order,
               properties_json
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND river_segment_id = ANY(%s)
        """,
        (river_network_version_id, list(river_segment_ids)),
    )
    digests: dict[str, str] = {}
    expected_properties_by_id = expected_properties_by_id or {}
    for row in cursor.fetchall():
        river_segment_id = str(row["river_segment_id"])
        properties_json = _json_dict(row["properties_json"])
        expected_properties = expected_properties_by_id.get(river_segment_id)
        if expected_properties is not None:
            properties_json = _output_segment_idempotency_properties(properties_json, expected_properties)
        digests[river_segment_id] = _output_segment_digest(
            river_network_version_id=row["river_network_version_id"],
            segment_order=row["segment_order"],
            properties_json=properties_json,
        )
    return digests


def _output_segment_idempotency_properties(
    stored_properties: dict[str, Any],
    expected_properties: dict[str, Any],
) -> dict[str, Any]:
    return {key: stored_properties[key] for key in expected_properties if key in stored_properties}


def _station_digest(
    *,
    basin_version_id: Any,
    station_name: Any,
    lon: Any,
    lat: Any,
    elevation_m: Any,
    station_role: Any,
    active_flag: bool,
    properties_json: dict[str, Any],
) -> str:
    return _canonical_json(
        {
            "basin_version_id": basin_version_id,
            "station_name": station_name,
            "lon": None if lon is None else float(lon),
            "lat": None if lat is None else float(lat),
            "elevation_m": None if elevation_m is None else float(elevation_m),
            "station_role": station_role,
            "active_flag": bool(active_flag),
            "properties_json": properties_json,
        }
    )


def _output_segment_digest(
    *,
    river_network_version_id: Any,
    segment_order: Any,
    properties_json: dict[str, Any],
) -> str:
    return _canonical_json(
        {
            "river_network_version_id": river_network_version_id,
            "segment_order": None if segment_order is None else int(segment_order),
            "properties_json": properties_json,
        }
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_qhh_source_identity(
    sources: ImportSources,
    *,
    qhh_basin_slug: str,
    qhh_project_name: str,
    model_id: str,
) -> None:
    if sources.ids["model_id"] != model_id:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MODEL_ID_MISMATCH",
            "QHH package manifest model_id does not match requested bootstrap model_id.",
            model_id=model_id,
            details={"actual_model_id": sources.ids["model_id"], "no_mutation_expected": True},
        )
    if sources.model.get("basin_slug") != qhh_basin_slug or sources.model.get("shud_input_name") != qhh_project_name:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SOURCE_IDENTITY_MISMATCH",
            "QHH package source identity does not match requested basin/project.",
            model_id=model_id,
            details={
                "expected_basin_slug": qhh_basin_slug,
                "actual_basin_slug": sources.model.get("basin_slug"),
                "expected_project_name": qhh_project_name,
                "actual_project_name": sources.model.get("shud_input_name"),
                "no_mutation_expected": True,
            },
        )


def _require_qhh_preflight_source_identity(
    sources: QhhPreflightSources,
    *,
    qhh_basin_slug: str,
    qhh_project_name: str,
    model_id: str,
) -> None:
    if sources.manifest.get("model_id") != model_id:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MODEL_ID_MISMATCH",
            "QHH package manifest model_id does not match requested bootstrap model_id.",
            model_id=model_id,
            details={"actual_model_id": sources.manifest.get("model_id"), "no_mutation_expected": True},
        )
    identity_fields = {
        "inventory_basin_slug": sources.model.get("basin_slug"),
        "inventory_project_name": sources.model.get("shud_input_name"),
        "manifest_basin_slug": sources.manifest.get("basin_slug"),
        "manifest_project_name": sources.manifest.get("shud_input_name"),
    }
    if (
        identity_fields["inventory_basin_slug"] != qhh_basin_slug
        or identity_fields["inventory_project_name"] != qhh_project_name
        or identity_fields["manifest_basin_slug"] != qhh_basin_slug
        or identity_fields["manifest_project_name"] != qhh_project_name
    ):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SOURCE_IDENTITY_MISMATCH",
            "QHH package source identity does not match requested basin/project.",
            model_id=model_id,
            details={
                "expected_basin_slug": qhh_basin_slug,
                "expected_project_name": qhh_project_name,
                **identity_fields,
                "no_mutation_expected": True,
            },
        )


def _require_qhh_physical_source_binding(
    sources: ImportSources,
    paths: QhhBootstrapPaths,
    *,
    model_id: str,
) -> None:
    source_actual = _safe_directory_binding(sources.source_root, model_id=model_id, role="inventory_source_root")
    source_expected = _safe_directory_binding(paths.qhh_source_root, model_id=model_id, role="qhh_source_root")
    input_actual = _safe_trusted_root_binding(sources.input_dir, model_id=model_id, role="inventory_input_dir")
    input_expected = _safe_trusted_root_binding(paths.qhh_input_dir, model_id=model_id, role="qhh_input_dir")
    mismatches: list[str] = []
    if source_actual != source_expected:
        mismatches.append("source_root")
    if input_actual != input_expected:
        mismatches.append("input_dir")
    if mismatches:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SOURCE_ROOT_MISMATCH",
            "QHH inventory/package physical source paths do not match the configured Basins root.",
            model_id=model_id,
            path=str(paths.basins_root),
            details={
                "fields": mismatches,
                "expected_source_root": str(source_expected["path"]),
                "actual_source_root": str(source_actual["path"]),
                "expected_input_dir": str(input_expected["path"]),
                "actual_input_dir": str(input_actual["path"]),
                "no_mutation_expected": True,
            },
        )


def _require_qhh_preflight_physical_source_binding(
    sources: QhhPreflightSources,
    paths: QhhBootstrapPaths,
    *,
    model_id: str,
) -> None:
    source_actual = _safe_directory_binding(sources.source_root, model_id=model_id, role="inventory_source_root")
    source_expected = _safe_directory_binding(paths.qhh_source_root, model_id=model_id, role="qhh_source_root")
    input_actual = _safe_trusted_root_binding(sources.input_dir, model_id=model_id, role="inventory_input_dir")
    input_expected = _safe_trusted_root_binding(paths.qhh_input_dir, model_id=model_id, role="qhh_input_dir")
    mismatches: list[str] = []
    if source_actual != source_expected:
        mismatches.append("source_root")
    if input_actual != input_expected:
        mismatches.append("input_dir")
    if mismatches:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_SOURCE_ROOT_MISMATCH",
            "QHH inventory/package physical source paths do not match the configured Basins root.",
            model_id=model_id,
            path=str(paths.basins_root),
            details={
                "fields": mismatches,
                "expected_source_root": str(source_expected["path"]),
                "actual_source_root": str(source_actual["path"]),
                "expected_input_dir": str(input_expected["path"]),
                "actual_input_dir": str(input_actual["path"]),
                "no_mutation_expected": True,
            },
        )


def _validate_manifest_checksum(path: Path, manifest: dict[str, Any], *, model_id: str) -> None:
    del manifest
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    if not checksum_path.exists():
        return
    content = _read_standalone_file_limited(
        checksum_path,
        max_bytes=MAX_QHH_CHECKSUM_BYTES,
        error_code="QHH_BOOTSTRAP_CHECKSUM_OVERSIZED",
        model_id=model_id,
        role="manifest_checksum",
    )
    try:
        expected = content.decode("utf-8", errors="replace").strip().split()[0]
    except IndexError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_CHECKSUM_MALFORMED",
            "QHH package manifest checksum sidecar is empty or malformed.",
            model_id=model_id,
            path=str(checksum_path),
            details={"no_mutation_expected": True},
        ) from error
    manifest_bytes = _read_standalone_file_limited(
        path,
        max_bytes=MAX_QHH_JSON_BYTES,
        error_code="QHH_BOOTSTRAP_PACKAGE_MANIFEST_OVERSIZED",
        model_id=model_id,
        role="package_manifest",
    )
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if expected != actual:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_MANIFEST_DIGEST_MISMATCH",
            "QHH package manifest checksum sidecar does not match manifest content.",
            model_id=model_id,
            path=str(checksum_path),
            details={"expected_sha256": expected, "actual_sha256": actual, "no_mutation_expected": True},
        )


def _read_json_object_bounded(
    path: Path,
    *,
    max_bytes: int,
    error_code: str,
    not_found_code: str,
    model_id: str,
) -> tuple[dict[str, Any], bytes]:
    try:
        content = _read_standalone_file_limited(
            path,
            max_bytes=max_bytes,
            error_code=f"{error_code}_OVERSIZED",
            model_id=model_id,
            role="json",
        )
    except FileNotFoundError as error:
        raise QhhProductionBootstrapError(
            not_found_code,
            "QHH bootstrap JSON input does not exist.",
            model_id=model_id,
            path=str(path),
            details={"no_mutation_expected": True},
        ) from error
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QhhProductionBootstrapError(
            error_code,
            "QHH bootstrap JSON input is malformed.",
            model_id=model_id,
            path=str(path),
            details={"no_mutation_expected": True},
        ) from error
    if not isinstance(payload, dict):
        raise QhhProductionBootstrapError(
            error_code,
            "QHH bootstrap JSON input must contain an object.",
            model_id=model_id,
            path=str(path),
            details={"no_mutation_expected": True},
        )
    return payload, content


def _resolve_qhh_source_root(
    basins_root: Path,
    *,
    qhh_basin_slug: str,
    qhh_project_name: str,
    model_id: str,
) -> Path:
    if Path(qhh_basin_slug).is_absolute() or ".." in Path(qhh_basin_slug).parts:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PROJECT_PATH_UNSAFE",
            "QHH basin/project path must be relative and contained by NHMS_BASINS_ROOT.",
            model_id=model_id,
            details={"qhh_basin_slug": qhh_basin_slug, "no_mutation_expected": True},
        )
    source_root = basins_root / qhh_basin_slug
    _trusted_child_dir(source_root, basins_root, model_id=model_id, role="qhh_source_root")
    input_dir = source_root / "input" / qhh_project_name
    if not input_dir.exists():
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PROJECT_FILE_MISSING",
            "QHH project input directory is missing.",
            model_id=model_id,
            path=str(input_dir),
            details={"missing": [f"{qhh_basin_slug}/input/{qhh_project_name}"], "no_mutation_expected": True},
        )
    return source_root


def _trusted_basins_root(path: Path) -> Path:
    root = Path(path).expanduser()
    root = root if root.is_absolute() else Path(os.path.abspath(root))
    try:
        verify_directory_no_follow(root)
    except (OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_BASINS_ROOT_UNSAFE",
            "QHH Basins root must be an existing no-symlink directory.",
            path=str(root),
            details={"no_mutation_expected": True},
        ) from error
    return root


def _trusted_child_dir(path: Path, root: Path, *, model_id: str, role: str) -> TrustedBasinsRoot:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package path escapes the configured Basins root.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        ) from error
    try:
        verify_directory_no_follow(path)
        return trusted_basins_root(path, role=role)
    except (BasinsGeometryError, OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package directory must be a no-symlink contained directory.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        ) from error


def _safe_directory_binding(path: Path, *, model_id: str, role: str) -> dict[str, Any]:
    try:
        verified_path = verify_directory_no_follow(path)
        root = trusted_basins_root(verified_path, role=role)
    except (BasinsGeometryError, OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package directory cannot be safely bound.",
            model_id=model_id,
            path=getattr(error, "path", str(path)),
            details={"role": role, **getattr(error, "details", {}), "no_mutation_expected": True},
        ) from error
    return _safe_trusted_root_binding(root, model_id=model_id, role=role)


def _safe_trusted_root_binding(root: TrustedBasinsRoot, *, model_id: str, role: str) -> dict[str, Any]:
    try:
        verify_directory_no_follow(root.path)
        verified = trusted_basins_root(root.path, role=role)
    except (BasinsGeometryError, OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package directory cannot be safely rebound.",
            model_id=model_id,
            path=getattr(error, "path", str(root.path)),
            details={"role": role, **getattr(error, "details", {}), "no_mutation_expected": True},
        ) from error
    if verified != root:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package directory binding changed during bootstrap preflight.",
            model_id=model_id,
            path=str(root.path),
            details={"role": role, "no_mutation_expected": True},
        )
    return {"path": root.resolved_path, "identity": root.identity}


def _require_contained_regular_file(path: Path, root: TrustedBasinsRoot, *, model_id: str, role: str) -> None:
    try:
        st = stat_no_follow(path, containment_root=root.path)
    except FileNotFoundError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PROJECT_FILE_MISSING",
            "QHH required project file is missing.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "missing": [path.name], "no_mutation_expected": True},
        ) from error
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH required project file path is unsafe.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if not stat.S_ISREG(st.st_mode):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PROJECT_FILE_UNSAFE",
            "QHH required project file must be a regular file.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        )


def _read_contained_file_limited(
    path: Path,
    root: TrustedBasinsRoot,
    *,
    max_bytes: int,
    error_code: str,
    model_id: str,
    role: str,
) -> bytes:
    _require_contained_regular_file(path, root, model_id=model_id, role=role)
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=root.path)
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH project file cannot be safely read.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if len(content) > max_bytes:
        raise QhhProductionBootstrapError(
            error_code,
            "QHH project file exceeds the bounded read limit.",
            model_id=model_id,
            path=str(path),
            details={"max_bytes": max_bytes, "observed_more_than": max_bytes, "no_mutation_expected": True},
        )
    return content


def _read_standalone_file_limited(
    path: Path,
    *,
    max_bytes: int,
    error_code: str,
    model_id: str,
    role: str,
) -> bytes:
    path = Path(path).expanduser()
    root = path.parent if path.parent != Path("") else Path(".")
    try:
        st = stat_no_follow(path, containment_root=root)
    except FileNotFoundError:
        raise
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input path is unsafe.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if not stat.S_ISREG(st.st_mode):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input path must be a regular file.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        )
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=root)
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input path cannot be safely read.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if len(content) > max_bytes:
        raise QhhProductionBootstrapError(
            error_code,
            "QHH bootstrap input exceeds the bounded read limit.",
            model_id=model_id,
            path=str(path),
            details={
                "role": role,
                "max_bytes": max_bytes,
                "observed_more_than": max_bytes,
                "no_mutation_expected": True,
            },
        )
    return content


def _bootstrap_work_dir(work_dir: str | Path | None, *, model_id: str) -> Path:
    raw = work_dir or os.getenv("NHMS_QHH_BOOTSTRAP_WORK_DIR", ".nhms-qhh-bootstrap")
    path = Path(raw).expanduser()
    path = path if path.is_absolute() else Path.cwd() / path
    try:
        ensure_directory_no_follow(path)
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_WORK_DIR_UNSAFE",
            "QHH bootstrap work directory is unsafe.",
            model_id=model_id,
            path=str(path),
            details={"reason": error.kind, "no_mutation_expected": True},
        ) from error
    return path


def _write_generated_json(path: Path, payload: dict[str, Any], *, model_id: str, role: str) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        ensure_directory_no_follow(path.parent)
        atomic_write_bytes_no_follow(path, content, containment_root=path.parent)
    except (OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_WORK_FILE_WRITE_FAILED",
            "QHH bootstrap generated file cannot be safely written.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        ) from error


def _require_contained_optional_existing_json(
    path: Path,
    *,
    containment_root: Path,
    model_id: str,
    role: str,
) -> None:
    try:
        st = stat_no_follow(path, containment_root=containment_root)
    except FileNotFoundError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_NOT_FOUND",
            "QHH bootstrap input file does not exist.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        ) from error
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input file is unsafe.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if not stat.S_ISREG(st.st_mode):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input file must be regular.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        )


def _reserve_evidence_path(
    evidence_path: str | Path,
    *,
    evidence_dir: str | Path | None,
    model_id: str,
) -> QhhEvidenceReservation:
    if evidence_dir is None:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_ROOT_REQUIRED",
            "--evidence-dir is required when --evidence-path is provided.",
            model_id=model_id,
            path=str(evidence_path),
            details={"no_mutation_expected": True},
        )
    root = Path(evidence_dir).expanduser()
    root = root if root.is_absolute() else Path.cwd() / root
    target = Path(evidence_path).expanduser()
    target = target if target.is_absolute() else root / target
    try:
        verify_directory_no_follow(root)
        target.relative_to(root)
    except (ValueError, OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
            "QHH bootstrap evidence path must stay under evidence dir.",
            model_id=model_id,
            path=str(target),
            details={"evidence_dir": str(root), "no_mutation_expected": True},
        ) from error
    try:
        parent_fd = _open_evidence_parent_dir(target, root)
    except (OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
            "QHH bootstrap evidence parent cannot be safely prepared.",
            model_id=model_id,
            path=str(target),
            details={"reason": getattr(error, "kind", "io"), "no_mutation_expected": True},
        ) from error
    try:
        try:
            fd = os.open(target.name, _EVIDENCE_FILE_FLAGS, 0o666, dir_fd=parent_fd)
        except FileExistsError as error:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_EVIDENCE_NO_CLOBBER",
                "QHH bootstrap evidence path already exists.",
                model_id=model_id,
                path=str(target),
                details={"no_mutation_expected": True},
            ) from error
        except OSError as error:
            reason = "unsafe" if error.errno == errno.ELOOP else "io"
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_EVIDENCE_WRITE_FAILED",
                "QHH bootstrap evidence cannot be safely reserved.",
                model_id=model_id,
                path=str(target),
                details={"reason": reason, "no_mutation_expected": True},
            ) from error
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise QhhProductionBootstrapError(
                    "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
                    "QHH bootstrap evidence path must be a regular file.",
                    model_id=model_id,
                    path=str(target),
                    details={"no_mutation_expected": True},
                )
            identity = (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            return QhhEvidenceReservation(root=root, target=target, fd=fd, identity=identity)
        except Exception:
            try:
                os.close(fd)
            finally:
                _unlink_reserved_evidence_path(target, root, model_id=model_id)
            raise
    finally:
        os.close(parent_fd)


def _write_reserved_evidence_path(
    reservation: QhhEvidenceReservation,
    report: dict[str, Any],
    *,
    model_id: str,
) -> None:
    content = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        _require_reserved_evidence_identity(reservation, model_id=model_id)
        os.ftruncate(reservation.fd, 0)
        os.lseek(reservation.fd, 0, os.SEEK_SET)
        view = memoryview(content)
        while view:
            written = os.write(reservation.fd, view)
            view = view[written:]
        os.fsync(reservation.fd)
        os.close(reservation.fd)
        reservation.closed = True
    except QhhProductionBootstrapError:
        _close_reserved_evidence_fd(reservation)
        raise
    except OSError as error:
        _close_reserved_evidence_fd(reservation)
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_WRITE_FAILED",
            "QHH bootstrap evidence cannot be safely written to the reserved file.",
            model_id=model_id,
            path=str(reservation.target),
        ) from error


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


def _cleanup_reserved_evidence_path(reservation: QhhEvidenceReservation, *, model_id: str) -> None:
    _close_reserved_evidence_fd(reservation)
    _unlink_reserved_evidence_path(
        reservation.target,
        reservation.root,
        model_id=model_id,
        expected_identity=reservation.identity,
    )


def _close_reserved_evidence_fd(reservation: QhhEvidenceReservation) -> None:
    if reservation.closed:
        return
    reservation.closed = True
    try:
        os.close(reservation.fd)
    except OSError:
        pass


def _unlink_reserved_evidence_path(
    target: Path,
    root: Path,
    *,
    model_id: str,
    expected_identity: tuple[int, int, int] | None = None,
) -> None:
    try:
        if expected_identity is not None:
            try:
                path_stat = stat_no_follow(target, containment_root=root)
            except FileNotFoundError:
                return
            actual_identity = (path_stat.st_dev, path_stat.st_ino, stat.S_IFMT(path_stat.st_mode))
            if actual_identity != expected_identity:
                raise QhhProductionBootstrapError(
                    "QHH_BOOTSTRAP_EVIDENCE_CLEANUP_FAILED",
                    "QHH bootstrap evidence reservation changed before cleanup.",
                    model_id=model_id,
                    path=str(target),
                )
        unlink_no_follow(target, containment_root=root, missing_ok=True)
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_CLEANUP_FAILED",
            "QHH bootstrap evidence reservation could not be safely cleaned up.",
            model_id=model_id,
            path=str(target),
            details={"reason": error.kind},
        ) from error


def _require_reserved_evidence_identity(reservation: QhhEvidenceReservation, *, model_id: str) -> None:
    try:
        path_stat = stat_no_follow(reservation.target, containment_root=reservation.root)
        fd_stat = os.fstat(reservation.fd)
    except (OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
            "QHH bootstrap evidence reservation cannot be safely rebound.",
            model_id=model_id,
            path=str(reservation.target),
        ) from error
    path_identity = (path_stat.st_dev, path_stat.st_ino, stat.S_IFMT(path_stat.st_mode))
    fd_identity = (fd_stat.st_dev, fd_stat.st_ino, stat.S_IFMT(fd_stat.st_mode))
    if path_identity != reservation.identity or fd_identity != reservation.identity:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
            "QHH bootstrap evidence target changed after reservation.",
            model_id=model_id,
            path=str(reservation.target),
        )


def _open_evidence_parent_dir(target: Path, root: Path) -> int:
    target.relative_to(root)
    ensure_directory_no_follow(target.parent, containment_root=root)
    root_fd = os.open(root, _EVIDENCE_DIR_FLAGS)
    fd = root_fd
    try:
        relative_parent = target.parent.relative_to(root)
        for part in relative_parent.parts:
            if part in {"", ".", ".."}:
                raise SafeFilesystemError("Unsafe evidence path component.")
            next_fd = os.open(part, _EVIDENCE_DIR_FLAGS, dir_fd=fd)
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        if fd == root_fd:
            return os.dup(root_fd)
        parent_fd = fd
        fd = -1
        return parent_fd
    except OSError as error:
        raise SafeFilesystemError(f"Failed to open evidence parent directory: {error}", kind="io") from error
    finally:
        if fd != -1 and fd != root_fd:
            os.close(fd)
        os.close(root_fd)


def _coerce_trusted_root(root: str | Path | TrustedBasinsRoot, *, role: str) -> TrustedBasinsRoot:
    if isinstance(root, TrustedBasinsRoot):
        return root
    try:
        return trusted_basins_root(Path(root), role=role)
    except BasinsGeometryError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH containment root is unsafe.",
            path=error.path,
            details={"role": role, **error.details, "no_mutation_expected": True},
        ) from error


def _safe_identifier(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch in {"_", "-", "."} for ch in value)


def _qhh_code(code: str) -> str:
    if code.startswith("QHH_BOOTSTRAP_"):
        return code
    return f"QHH_BOOTSTRAP_{code}"


def _from_registry_error(error: BasinsRegistryImportError, *, model_id: str) -> QhhProductionBootstrapError:
    return QhhProductionBootstrapError(
        _qhh_code(error.error_code),
        str(error),
        model_id=error.model_id or model_id,
        path=error.path,
        details={**error.details, "no_mutation_expected": True},
    )


def _from_package_error(error: BasinsPackageError, *, model_id: str) -> QhhProductionBootstrapError:
    return QhhProductionBootstrapError(
        _qhh_code(error.error_code),
        str(error),
        model_id=error.model_id or model_id,
        path=error.path,
        details={"manifest_uri": error.manifest_uri, "no_mutation_expected": True},
    )
