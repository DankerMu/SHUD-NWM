"""QHH production bootstrap contracts: schema/limit constants, the bootstrap
error and its registry/package error mapping, and the frozen path/station/
context/evidence dataclasses (#2490 split of ``qhh_production_bootstrap``).

``workers.model_registry.qhh_production_bootstrap`` stays the stable import
path and re-exports every name defined here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .basins_geometry import TrustedBasinsRoot
from .basins_package import BasinsPackageError
from .basins_registry_import import BasinsRegistryImportError, ImportSources

QHH_BOOTSTRAP_SCHEMA_VERSION = "qhh.production_bootstrap.v1"
DEFAULT_QHH_PROJECT_NAME = "qhh"
DEFAULT_QHH_MODEL_ID = "basins_qhh_shud"
DEFAULT_QHH_BASIN_SLUG = "qhh"
DEFAULT_QHH_PACKAGE_VERSION = "vbasins-qhh-production"
DEFAULT_QHH_SHUD_CODE_VERSION = "basins-shud"
MAX_QHH_BOOTSTRAP_DISCOVERY_DEPTH = 3
MAX_QHH_BOOTSTRAP_DISCOVERY_FILE_DEPTH = MAX_QHH_BOOTSTRAP_DISCOVERY_DEPTH + 3
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
        "resource_profile_id",
    }
)
QHH_RESOURCE_PROFILE_PRESERVED_OPERATIONAL_FIELDS = QHH_RESOURCE_PROFILE_OVERRIDE_ALLOWED_FIELDS
QHH_RESOURCE_PROFILE_RUN_SCOPED_FIELDS = frozenset(
    {
        "canonical_product_id",
        "published_manifest_id",
        "pipeline_job_id",
        "output_uri",
        "durable_output_uri",
        "shud_output_uri",
        "run_id",
        "hydro_run_id",
        "forcing_version_id",
        "forecast_cycle",
        "cycle_id",
        "cycle_time",
        "source_id",
        "publish_uri",
        "forcing_uri",
    }
)


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
