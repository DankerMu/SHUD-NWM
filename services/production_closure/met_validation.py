from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

from packages.common.object_store import LocalObjectStore
from packages.common.redaction import redact_payload, redact_text
from packages.common.test_netcdf4 import encode_test_netcdf4
from workers.canonical_converter.converter import (
    CanonicalConversionError,
    CanonicalConverter,
    CanonicalConverterConfig,
    ERA5CanonicalConverter,
    ERA5CanonicalConverterConfig,
    IFSCanonicalConverter,
    IFSCanonicalConverterConfig,
    format_cycle_time,
    parse_cycle_time,
)
from workers.forcing_producer.producer import (
    CanonicalProduct,
    ForcingProducer,
    ForcingProducerConfig,
    ForcingProductionError,
    ForcingTimeseriesRow,
    InterpolationWeight,
    MetStation,
)

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SENSITIVE_PREFIX_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;?#&])[^=/?#;&]*(?:token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|"
    r"session[_-]?key|signature|x-amz-signature)[^=/?#;&]*=",
    re.IGNORECASE,
)
SENSITIVE_PREFIX_SEPARATOR_RE = re.compile(r"[/;?#&]")

SOURCE_ORDER = ("GFS", "IFS", "ERA5", "CLDAS")
DETERMINISTIC_SOURCES = ("GFS", "IFS", "ERA5")
SOURCE_STORAGE_ID = {"GFS": "gfs", "IFS": "IFS", "ERA5": "ERA5", "CLDAS": "CLDAS"}
DEFAULT_CYCLE_START = "2026-05-07T00:00:00Z"
DEFAULT_CYCLE_END = "2026-05-07T03:00:00Z"
DEFAULT_FORECAST_HOURS = (0, 3)
DEFAULT_SOURCE_SUBSET = ("GFS", "IFS", "ERA5")
GFS_VARIABLES = ("tmp2m", "apcp", "rh2m", "u10m", "v10m", "pressfc", "dswrf")
IFS_VARIABLES = ("2t", "2d", "tp", "10u", "10v", "sp", "ssr", "str")
ERA5_VARIABLES = (
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "surface_pressure",
    "total_precipitation",
    "surface_net_solar_radiation",
    "surface_net_thermal_radiation",
)
REQUIRED_FORCING_VARIABLES = ("PRCP", "TEMP", "RH", "wind", "Rn", "Press")
NEGATIVE_FIXTURE_ENV = "NHMS_PRODUCTION_MET_NEGATIVE_FIXTURE"
FORCING_RANGES = {
    "PRCP": (0.0, 500.0),
    "TEMP": (-80.0, 60.0),
    "RH": (0.0, 1.0),
    "wind": (0.0, 120.0),
    "Rn": (-1000.0, 1500.0),
    "Press": (50000.0, 110000.0),
}
MAX_MANIFEST_ENTRIES = 64
MAX_PER_FILE_BYTES = 2 * 1024 * 1024
MAX_FORECAST_HOURS = 8
MAX_RETRIES = 3
MAX_TIMEOUT_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 60.0
MAX_EVIDENCE_PAYLOAD_BYTES = 512 * 1024
MAX_DETERMINISTIC_FILE_BYTES = 128 * 1024
MIN_PER_FILE_BYTES = 4096
MIN_DETERMINISTIC_RAW_FILE_BYTES = 8192


class ProductionMetValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass
class EvidenceWriter:
    evidence_root: Path
    lane_dir: Path
    force: bool = False
    max_payload_bytes: int = MAX_EVIDENCE_PAYLOAD_BYTES
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
            raise ProductionMetValidationError(
                "PRODUCTION_MET_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under evidence root.",
            ) from error
        self.lane_dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, path: Path, payload: Any) -> None:
        content = json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        if len(content) > self.max_payload_bytes:
            raise ProductionMetValidationError(
                "PRODUCTION_MET_EVIDENCE_PAYLOAD_TOO_LARGE",
                f"Evidence payload exceeds configured limit of {self.max_payload_bytes} bytes.",
            )
        self._write_bytes(path, content)

    def _write_bytes(self, path: Path, content: bytes) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ProductionMetValidationError(
                "PRODUCTION_MET_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to replace an existing run_id bundle.",
            )
        temp_path = safe_path.with_name(f".{safe_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(content)
            os.replace(temp_path, safe_path)
            self._created_paths.add(safe_path)
        except OSError as error:
            temp_path.unlink(missing_ok=True)
            raise ProductionMetValidationError(
                "PRODUCTION_MET_EVIDENCE_WRITE_FAILED",
                f"Failed to write evidence file {safe_path}: {error}",
            ) from error

    def _safe_file_path(self, path: Path) -> Path:
        if path.is_symlink():
            raise ProductionMetValidationError(
                "PRODUCTION_MET_EVIDENCE_SYMLINK",
                f"Evidence file must not be a symlink: {path}",
            )
        _refuse_symlink_components(path.parent)
        resolved_parent = path.parent.resolve(strict=False)
        try:
            resolved_parent.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionMetValidationError(
                "PRODUCTION_MET_EVIDENCE_PATH_UNSAFE",
                "Evidence file path must stay under evidence root.",
            ) from error
        path.parent.mkdir(parents=True, exist_ok=True)
        return resolved_parent / path.name


@dataclass(frozen=True)
class ProductionMetBounds:
    max_manifest_entries: int = MAX_MANIFEST_ENTRIES
    max_per_file_bytes: int = MAX_PER_FILE_BYTES
    max_forecast_hours: int = MAX_FORECAST_HOURS
    max_retries: int = MAX_RETRIES
    timeout_seconds: float = MAX_TIMEOUT_SECONDS
    max_backoff_seconds: float = MAX_BACKOFF_SECONDS
    max_evidence_payload_bytes: int = MAX_EVIDENCE_PAYLOAD_BYTES
    max_deterministic_file_bytes: int = MAX_DETERMINISTIC_FILE_BYTES

    @classmethod
    def from_env(cls) -> ProductionMetBounds:
        return cls(
            max_manifest_entries=_positive_int_env(
                "NHMS_PRODUCTION_MET_MAX_MANIFEST_ENTRIES",
                MAX_MANIFEST_ENTRIES,
                maximum=MAX_MANIFEST_ENTRIES,
            ),
            max_per_file_bytes=_positive_int_env(
                "NHMS_PRODUCTION_MET_MAX_PER_FILE_BYTES",
                MAX_PER_FILE_BYTES,
                maximum=MAX_PER_FILE_BYTES,
            ),
            max_forecast_hours=_positive_int_env(
                "NHMS_PRODUCTION_MET_MAX_FORECAST_HOURS",
                MAX_FORECAST_HOURS,
                maximum=MAX_FORECAST_HOURS,
            ),
            max_retries=_nonnegative_int_env(
                "NHMS_PRODUCTION_MET_MAX_RETRIES",
                MAX_RETRIES,
                maximum=MAX_RETRIES,
            ),
            timeout_seconds=_positive_float_env(
                "NHMS_PRODUCTION_MET_TIMEOUT_SECONDS",
                MAX_TIMEOUT_SECONDS,
                maximum=MAX_TIMEOUT_SECONDS,
            ),
            max_backoff_seconds=_positive_float_env(
                "NHMS_PRODUCTION_MET_MAX_BACKOFF_SECONDS",
                MAX_BACKOFF_SECONDS,
                maximum=MAX_BACKOFF_SECONDS,
            ),
            max_evidence_payload_bytes=_positive_int_env(
                "NHMS_PRODUCTION_MET_MAX_EVIDENCE_PAYLOAD_BYTES",
                MAX_EVIDENCE_PAYLOAD_BYTES,
                maximum=MAX_EVIDENCE_PAYLOAD_BYTES,
            ),
            max_deterministic_file_bytes=_positive_int_env(
                "NHMS_PRODUCTION_MET_MAX_DETERMINISTIC_FILE_BYTES",
                MAX_DETERMINISTIC_FILE_BYTES,
                maximum=MAX_DETERMINISTIC_FILE_BYTES,
            ),
        )


@dataclass(frozen=True)
class ProductionMetConfig:
    evidence_root: Path
    run_id: str
    enabled_sources: tuple[str, ...]
    cycle_start: datetime
    cycle_end: datetime
    forecast_hours: tuple[int, ...]
    configured_object_prefix: str
    object_prefix: str
    access_mode: str
    cached_fallback_policy: str
    model_id: str
    model_version: str
    cldas_restricted_reason: str
    bounds: ProductionMetBounds = field(default_factory=ProductionMetBounds.from_env)
    force: bool = False

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "met"

    @property
    def object_store_root(self) -> Path:
        return self.lane_dir / "local-object-store"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path,
        run_id: str | None,
        sources: str | None = None,
        cycle_start: str | None = None,
        cycle_end: str | None = None,
        forecast_hours: str | None = None,
        model_id: str | None = None,
        model_version: str | None = None,
        force: bool = False,
    ) -> ProductionMetConfig:
        resolved_evidence_root = _safe_resolved_evidence_root(evidence_root)
        resolved_run_id = _safe_run_id(run_id or datetime.now(UTC).strftime("m10-%Y%m%dT%H%M%SZ"))
        configured_prefix = (
            os.getenv("NHMS_PRODUCTION_MET_OBJECT_PREFIX")
            or os.getenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX")
            or "s3://nhms-production-like/met"
        )
        _validate_object_prefix_safe(configured_prefix)
        return cls(
            evidence_root=resolved_evidence_root,
            run_id=resolved_run_id,
            enabled_sources=_parse_sources(sources or os.getenv("NHMS_PRODUCTION_MET_SOURCES")),
            cycle_start=parse_cycle_time(
                cycle_start or os.getenv("NHMS_PRODUCTION_MET_CYCLE_START", DEFAULT_CYCLE_START)
            ),
            cycle_end=parse_cycle_time(cycle_end or os.getenv("NHMS_PRODUCTION_MET_CYCLE_END", DEFAULT_CYCLE_END)),
            forecast_hours=_parse_forecast_hours(
                forecast_hours or os.getenv("NHMS_PRODUCTION_MET_FORECAST_HOURS")
            ),
            configured_object_prefix=configured_prefix,
            object_prefix=_run_scoped_prefix(configured_prefix, resolved_run_id),
            access_mode=os.getenv("NHMS_PRODUCTION_MET_ACCESS_MODE", "public-or-deterministic-fixture"),
            cached_fallback_policy=os.getenv("NHMS_PRODUCTION_MET_CACHED_FALLBACK_POLICY", "deterministic_fixture"),
            model_id=model_id or os.getenv("NHMS_PRODUCTION_MET_MODEL_ID", "basins_qhh_shud_fixture"),
            model_version=model_version or os.getenv("NHMS_PRODUCTION_MET_MODEL_VERSION", "vproduction-met-local"),
            cldas_restricted_reason=os.getenv(
                "NHMS_PRODUCTION_MET_CLDAS_RESTRICTED_REASON",
                "CLDAS credentials/licensing are not available in the fast production-closure lane.",
            ),
            bounds=ProductionMetBounds.from_env(),
            force=force,
        )


class _MetClosureRepository:
    def __init__(self, *, model_id: str, basin_version_id: str = "basin_v1") -> None:
        self.model_id = model_id
        self.basin_version_id = basin_version_id
        self.products: dict[str, dict[str, Any]] = {}
        self.cycles: list[dict[str, Any]] = []
        self.interp_weights: list[InterpolationWeight] = []
        self.forcing_versions: dict[str, dict[str, Any]] = {}
        self.components: list[Any] = []
        self.timeseries: list[ForcingTimeseriesRow] = []

    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None:
        product = self.products.get(canonical_product_id)
        return dict(product) if product is not None else None

    def upsert_canonical_product(self, record: Mapping[str, Any]) -> dict[str, Any]:
        self.products[str(record["canonical_product_id"])] = dict(record)
        return dict(record)

    def update_forecast_cycle(self, **kwargs: Any) -> dict[str, Any]:
        self.cycles.append(dict(kwargs))
        return dict(kwargs)

    def resolve_model_basin_version(self, *, model_id: str) -> str:
        if model_id != self.model_id:
            raise ForcingProductionError(f"Unknown deterministic model fixture: {model_id}")
        return self.basin_version_id

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        if basin_version_id != self.basin_version_id:
            return ()
        return (MetStation("station_1", basin_version_id, 100.0, 30.0, 12.0, "forcing_proxy"),)

    def list_canonical_products(self, *, source_id: str, cycle_time: datetime) -> tuple[CanonicalProduct, ...]:
        products: list[CanonicalProduct] = []
        for record in self.products.values():
            if record["source_id"] != source_id or record["cycle_time"] != cycle_time:
                continue
            products.append(
                CanonicalProduct(
                    canonical_product_id=str(record["canonical_product_id"]),
                    source_id=str(record["source_id"]),
                    cycle_time=record["cycle_time"],
                    valid_time=record["valid_time"],
                    variable=str(record["variable"]),
                    unit=str(record["unit"]),
                    grid_id=str(record["grid_id"]),
                    object_uri=str(record["object_uri"]),
                    checksum=str(record["checksum"]),
                    grid_definition_uri=str(record["grid_definition_uri"]),
                    native_time_resolution=str(record["native_time_resolution"]),
                    native_spatial_resolution=str(record["native_spatial_resolution"]),
                    quality_flag=str(record.get("quality_flag", "ok")),
                    lead_time_hours=int(record["lead_time_hours"]),
                )
            )
        return tuple(sorted(products, key=lambda item: (item.variable, item.valid_time)))

    def list_fallback_canonical_products(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
        variables: Sequence[str],
    ) -> tuple[CanonicalProduct, ...]:
        return tuple(
            product
            for product in self.list_canonical_products(source_id=source_id, cycle_time=start_time)
            if start_time <= product.valid_time <= end_time and product.variable in variables
        )

    def load_interp_weights(
        self,
        *,
        source_id: str,
        grid_id: str,
        model_id: str,
    ) -> tuple[InterpolationWeight, ...]:
        return tuple(
            weight
            for weight in self.interp_weights
            if weight.source_id == source_id and weight.grid_id == grid_id and weight.model_id == model_id
        )

    def upsert_interp_weights(self, weights: Sequence[InterpolationWeight]) -> None:
        self.interp_weights.extend(weights)

    def get_forcing_version(self, *, source_id: str, cycle_time: datetime, model_id: str) -> dict[str, Any] | None:
        for record in self.forcing_versions.values():
            if (
                record["source_id"] == source_id
                and record["cycle_time"] == cycle_time
                and record["model_id"] == model_id
            ):
                return dict(record)
        return None

    def upsert_forcing_version(self, record: Mapping[str, Any]) -> dict[str, Any]:
        self.forcing_versions[str(record["forcing_version_id"])] = dict(record)
        return dict(record)

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]:
        self.forcing_versions[forcing_version_id]["checksum"] = checksum
        return dict(self.forcing_versions[forcing_version_id])

    def replace_forcing_components(self, forcing_version_id: str, components: Sequence[Any]) -> None:
        self.components = [
            component for component in self.components if component.forcing_version_id != forcing_version_id
        ]
        self.components.extend(components)

    def replace_forcing_timeseries(self, forcing_version_id: str, rows: Sequence[ForcingTimeseriesRow]) -> None:
        self.timeseries = [row for row in self.timeseries if row.forcing_version_id != forcing_version_id]
        self.timeseries.extend(rows)


def validate_met(config: ProductionMetConfig) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    _validate_config(config)
    _validate_deterministic_shape(config)

    writer = EvidenceWriter(
        config.evidence_root,
        config.lane_dir,
        force=config.force,
        max_payload_bytes=config.bounds.max_evidence_payload_bytes,
    )
    _prepare_object_bundle(config)
    writer.prepare()

    preflight = _preflight_payload(config)
    writer.write_json(config.lane_dir / "preflight.json", preflight)
    source_config = _source_config_payload(config)
    writer.write_json(config.lane_dir / "source_config.json", source_config)

    store = LocalObjectStore(config.object_store_root, config.object_prefix)
    _write_grid_definition(store, config)
    raw_evidence = _write_raw_source_evidence(config, store)
    writer.write_json(config.lane_dir / "raw_cycle_manifest.json", raw_evidence)

    repository = _MetClosureRepository(model_id=config.model_id)
    canonical_evidence = _write_canonical_evidence(config, store, repository, raw_evidence)
    writer.write_json(config.lane_dir / "canonical_products.json", canonical_evidence)

    forcing_evidence = _write_forcing_evidence(config, store, repository)
    writer.write_json(config.lane_dir / "forcing_manifest.json", forcing_evidence["manifest"])
    writer.write_json(config.lane_dir / "forcing_qc.json", forcing_evidence["qc"])

    lineage = _best_available_lineage(config, raw_evidence, canonical_evidence, forcing_evidence["qc"])
    writer.write_json(config.lane_dir / "best_available_lineage.json", lineage)

    environment = _environment_payload(config)
    writer.write_json(config.lane_dir / "environment.json", environment)

    blockers = _result_blockers(raw_evidence, canonical_evidence, forcing_evidence["qc"], lineage)
    status = "ready" if not blockers else "blocked"
    summary = _summary(
        config,
        status=status,
        blockers=blockers,
        files=[
            "preflight.json",
            "source_config.json",
            "raw_cycle_manifest.json",
            "canonical_products.json",
            "forcing_manifest.json",
            "forcing_qc.json",
            "best_available_lineage.json",
            "environment.json",
        ],
        raw_evidence=raw_evidence,
        canonical_evidence=canonical_evidence,
        forcing_manifest=forcing_evidence["manifest"],
    )
    writer.write_json(config.lane_dir / "summary.json", summary)
    return summary


def _preflight_payload(config: ProductionMetConfig) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.met.preflight.v1",
        "issue": 149,
        "run_id": config.run_id,
        "enabled_sources": list(config.enabled_sources),
        "access_mode": config.access_mode,
        "cached_fallback_policy": config.cached_fallback_policy,
        "cycle_window": {
            "start": _format_time(config.cycle_start),
            "end": _format_time(config.cycle_end),
            "forecast_hours": list(config.forecast_hours),
        },
        "object_prefix": config.object_prefix,
        "configured_object_prefix": config.configured_object_prefix,
        "selected_model": {
            "selection_mode": "deterministic_model_fixture",
            "model_id": config.model_id,
            "version": config.model_version,
            "basin_version_id": "basin_v1",
        },
        "cldas": {
            "status": "restricted",
            "restricted_reason": config.cldas_restricted_reason,
        },
        "evidence_root": str(config.evidence_root),
        "evidence_dir": str(config.lane_dir),
        "bounds": _bounds_payload(config.bounds),
    }


def _source_config_payload(config: ProductionMetConfig) -> dict[str, Any]:
    sources = []
    for source in SOURCE_ORDER:
        status = _source_status(config, source)
        configured_execution_mode = _source_configured_execution_mode(config, source, status)
        execution_mode = _source_execution_mode(config, source, status)
        endpoint = _configured_endpoint(source)
        sources.append(
            {
                "source": source,
                "source_id": SOURCE_STORAGE_ID[source],
                "status": status,
                "configured_execution_mode": configured_execution_mode,
                "execution_mode": execution_mode,
                "endpoint_identity": endpoint,
                "source_auth_reference": _source_auth_reference(source, execution_mode),
                "live_gate": {
                    "network_enabled": _truthy_env(os.getenv("NHMS_PRODUCTION_MET_ALLOW_LIVE_NETWORK")),
                    "source_enabled": _truthy_env(os.getenv(f"NHMS_PRODUCTION_MET_LIVE_{source}")),
                    "execution_status": "not_executed"
                    if execution_mode != "live_executed"
                    else "live_executed",
                },
                "reason": _source_reason(config, source, status, execution_mode),
            }
        )
    return {
        "schema": "nhms.production_closure.met.source_config.v1",
        "run_id": config.run_id,
        "sources": sources,
    }


def _write_raw_source_evidence(config: ProductionMetConfig, store: LocalObjectStore) -> dict[str, Any]:
    source_entries = []
    total_files = 0
    total_bytes = 0
    for source in SOURCE_ORDER:
        status = _source_status(config, source)
        configured_execution_mode = _source_configured_execution_mode(config, source, status)
        execution_mode = _source_execution_mode(config, source, status)
        if execution_mode != "deterministic_fixture":
            source_entries.append(
                {
                    "source": source,
                    "source_id": SOURCE_STORAGE_ID[source],
                    "status": _raw_unavailable_status(status, execution_mode),
                    "configured_execution_mode": configured_execution_mode,
                    "execution_mode": execution_mode,
                    "reason": _source_reason(config, source, status, execution_mode),
                    "cycle_time": None,
                    "selected_forecast_hours": [],
                    "file_count": 0,
                    "byte_count": 0,
                    "checksums": [],
                    "retry_count": 0,
                    "raw_uri": None,
                    "object_uri": None,
                    "canonical_lineage_required": False,
                }
            )
            continue

        manifest = _write_deterministic_source_manifest(config, store, source)
        total_files += manifest["file_count"]
        total_bytes += manifest["byte_count"]
        source_entries.append(manifest)

    raw_status, blockers = _raw_aggregate_status(source_entries)
    payload = {
        "schema": "nhms.production_closure.met.raw_cycle_manifest.v1",
        "status": raw_status,
        "run_id": config.run_id,
        "cycle_time": _format_time(config.cycle_start),
        "sources": source_entries,
        "total_file_count": total_files,
        "total_byte_count": total_bytes,
        "blockers": blockers,
        "bounds": _bounds_payload(config.bounds),
    }
    _enforce_manifest_bound(config, total_files)
    return payload


def _raw_unavailable_status(status: str, execution_mode: str) -> str:
    if status == "restricted":
        return "restricted"
    if execution_mode == "skipped":
        return "skipped"
    if execution_mode == "not_executed":
        return "not_executed"
    return "unavailable"


def _raw_aggregate_status(source_entries: Sequence[Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    blockers = []
    for entry in source_entries:
        if entry.get("source") == "CLDAS" and entry.get("status") == "restricted":
            continue
        if entry.get("execution_mode") == "skipped":
            continue
        if entry.get("status") != "available":
            blockers.append(
                {
                    "error_code": "PRODUCTION_MET_RAW_SOURCE_NOT_AVAILABLE",
                    "source": entry.get("source"),
                    "source_id": entry.get("source_id"),
                    "status": entry.get("status"),
                    "execution_mode": entry.get("execution_mode"),
                    "reason": entry.get("reason"),
                }
            )
    return ("ready" if not blockers else "blocked", blockers)


def _write_deterministic_source_manifest(
    config: ProductionMetConfig,
    store: LocalObjectStore,
    source: str,
) -> dict[str, Any]:
    source_id = SOURCE_STORAGE_ID[source]
    compact_cycle = format_cycle_time(config.cycle_start)
    endpoint_identity = _configured_endpoint(source).rstrip("/")
    entries = []
    for forecast_hour in config.forecast_hours:
        for variable in _source_variables(source):
            key = f"raw/{source_id}/{compact_cycle}/{source.lower()}.{compact_cycle}.f{forecast_hour:03d}.{variable}.nc"
            content = _deterministic_raw_content(source, variable, forecast_hour, config.cycle_start)
            _enforce_per_file_bound(config, key, content)
            object_uri = _write_object_guarded(store, key, content, force=config.force)
            entries.append(
                {
                    "source": source,
                    "source_id": source_id,
                    "cycle_time": _format_time(config.cycle_start),
                    "forecast_hour": forecast_hour,
                    "variable": variable,
                    "remote_url": f"{endpoint_identity}/{compact_cycle}/f{forecast_hour:03d}/{variable}",
                    "endpoint_identity": endpoint_identity,
                    "local_key": key,
                    "object_uri": object_uri,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "retry_count": 0,
                }
            )

    manifest_key = f"raw/{source_id}/{compact_cycle}/manifest.json"
    manifest_payload = {
        "source_id": source_id,
        "source": source,
        "cycle_time": _format_time(config.cycle_start),
        "metadata": {
            "forecast_hours": list(config.forecast_hours),
            "execution_mode": "deterministic_fixture",
            "production_like": True,
            "endpoint_identity": endpoint_identity,
        },
        "entries": [
            {
                "remote_url": entry["remote_url"],
                "local_key": entry["local_key"],
                "variable": entry["variable"],
                "forecast_hour": entry["forecast_hour"],
            }
            for entry in entries
        ],
    }
    manifest_bytes = _json_bytes(manifest_payload)
    _enforce_per_file_bound(config, manifest_key, manifest_bytes)
    manifest_uri = _write_object_guarded(store, manifest_key, manifest_bytes, force=config.force)
    byte_count = sum(int(entry["size_bytes"]) for entry in entries) + len(manifest_bytes)
    checksums = [str(entry["sha256"]) for entry in entries[: min(8, len(entries))]]
    checksums.append(hashlib.sha256(manifest_bytes).hexdigest())
    return {
        "source": source,
        "source_id": source_id,
        "status": "available",
        "execution_mode": "deterministic_fixture",
        "cycle_time": _format_time(config.cycle_start),
        "selected_forecast_hours": list(config.forecast_hours),
        "file_count": len(entries) + 1,
        "byte_count": byte_count,
        "checksums": checksums,
        "retry_count": 0,
        "raw_uri": _directory_uri(store, f"raw/{source_id}/{compact_cycle}"),
        "object_uri": manifest_uri,
        "manifest_entries": entries,
        "unavailable_status": None,
    }


def _write_canonical_evidence(
    config: ProductionMetConfig,
    store: LocalObjectStore,
    repository: _MetClosureRepository,
    raw_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    gfs_source = _source_manifest(raw_evidence, "GFS")
    if not gfs_source or gfs_source.get("execution_mode") != "deterministic_fixture":
        return {
            "schema": "nhms.production_closure.met.canonical.v1",
            "status": "blocked",
            "error_code": "PRODUCTION_MET_CANONICAL_NO_GFS_FIXTURE",
            "products": [],
            "failure_checks": [],
        }

    converter = _canonical_converter(config, store, repository, "GFS")
    manifest = _raw_manifest_for_converter(gfs_source)
    try:
        result = converter.convert_manifest(manifest)
    except CanonicalConversionError as error:
        return {
            "schema": "nhms.production_closure.met.canonical.v1",
            "status": "blocked",
            "error_code": "PRODUCTION_MET_CANONICAL_CONVERT_FAILED",
            "error_message": str(error),
            "products": [],
            "failure_checks": [],
        }

    products = _canonical_product_payloads(repository, result)

    failure_checks = _stable_negative_failure_checks(config, store, repository, manifest)
    source_statuses = [
        {
            "source": source["source"],
            "source_id": source["source_id"],
            "raw_status": source["status"],
            "raw_execution_mode": source["execution_mode"],
            "conversion_status": "canonical_ready"
            if source["source"] == "GFS" and source["status"] == "available"
            else "skipped",
            "reason": source.get("reason"),
        }
        for source in raw_evidence.get("sources", [])
        if isinstance(source, Mapping)
    ]
    return {
        "schema": "nhms.production_closure.met.canonical.v1",
        "status": "ready",
        "conversion_status": result.status,
        "source_id": "gfs",
        "cycle_time": _format_time(config.cycle_start),
        "product_count": len(products),
        "source_statuses": source_statuses,
        "products": products,
        "failure_checks": failure_checks,
    }


def _write_forcing_evidence(
    config: ProductionMetConfig,
    store: LocalObjectStore,
    repository: _MetClosureRepository,
) -> dict[str, Any]:
    producer = ForcingProducer(
        config=ForcingProducerConfig(
            source_id="gfs",
            object_store_root=config.object_store_root,
            object_store_prefix=config.object_prefix,
            idw_neighbors=1,
        ),
        repository=repository,
        object_store=store,
    )
    try:
        result = producer.produce(
            source_id="gfs",
            cycle_time=config.cycle_start,
            model_id=config.model_id,
            max_lead_hours=max(config.forecast_hours),
        )
    except ForcingProductionError as error:
        return {
            "manifest": {
                "schema": "nhms.production_closure.met.forcing_manifest.v1",
                "status": "blocked",
                "error_code": "PRODUCTION_MET_FORCING_FAILED",
                "error_message": str(error),
            },
            "qc": {
                "schema": "nhms.production_closure.met.forcing_qc.v1",
                "status": "fail",
                "error_code": "PRODUCTION_MET_FORCING_FAILED",
                "error_message": str(error),
            },
        }

    package_manifest_uri = str(result.file_uris["package_manifest"])
    package_manifest = json.loads(store.read_bytes(package_manifest_uri).decode("utf-8"))
    qc = _forcing_qc_payload(
        repository.timeseries,
        package_manifest,
        expected_valid_times=_expected_valid_times(config),
        package_uri=result.forcing_package_uri,
        package_manifest_uri=package_manifest_uri,
    )
    manifest = {
        "schema": "nhms.production_closure.met.forcing_manifest.v1",
        "status": result.status,
        "forcing_version_id": result.forcing_version_id,
        "model_id": config.model_id,
        "model_version": config.model_version,
        "source_id": "gfs",
        "cycle_time": _format_time(config.cycle_start),
        "forcing_package_uri": result.forcing_package_uri,
        "forcing_package_manifest_uri": package_manifest_uri,
        "checksum": result.checksum,
        "station_count": result.station_count,
        "timestep_count": result.timestep_count,
        "file_uris": dict(result.file_uris),
        "package_manifest": package_manifest,
    }
    return {"manifest": manifest, "qc": qc}


def _forcing_qc_payload(
    rows: Sequence[ForcingTimeseriesRow],
    package_manifest: Mapping[str, Any],
    *,
    expected_valid_times: Sequence[datetime],
    package_uri: str | None = None,
    package_manifest_uri: str | None = None,
) -> dict[str, Any]:
    variables = sorted({row.variable for row in rows})
    valid_times = sorted({row.valid_time for row in rows})
    missing_required = sorted(set(REQUIRED_FORCING_VARIABLES) - set(variables))
    continuity = _continuity_check(valid_times, expected_valid_times=expected_valid_times)
    missing_values = [
        {
            "station_id": row.station_id,
            "valid_time": _format_time(row.valid_time),
            "variable": row.variable,
        }
        for row in rows
        if not math.isfinite(row.value)
    ]
    range_checks = []
    for variable in REQUIRED_FORCING_VARIABLES:
        values = [row.value for row in rows if row.variable == variable]
        minimum, maximum = FORCING_RANGES[variable]
        out_of_range = [value for value in values if value < minimum or value > maximum]
        range_checks.append(
            {
                "variable": variable,
                "unit": package_manifest_unit(variable),
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "allowed_min": minimum,
                "allowed_max": maximum,
                "status": "pass" if values and not out_of_range else "fail",
                "out_of_range_count": len(out_of_range),
            }
        )
    status = "pass"
    if missing_required or missing_values or continuity["status"] != "pass" or any(
        check["status"] != "pass" for check in range_checks
    ):
        status = "fail"
    return {
        "schema": "nhms.production_closure.met.forcing_qc.v1",
        "status": status,
        "continuity": continuity,
        "required_variables": {
            "expected": list(REQUIRED_FORCING_VARIABLES),
            "observed": variables,
            "missing": missing_required,
            "status": "pass" if not missing_required else "fail",
        },
        "units": {variable: package_manifest_unit(variable) for variable in REQUIRED_FORCING_VARIABLES},
        "missing_values": {
            "count": len(missing_values),
            "examples": missing_values[:10],
            "status": "pass" if not missing_values else "fail",
        },
        "range_checks": range_checks,
        "package_uri": package_uri or package_manifest.get("lineage", {}).get("forcing_package_uri"),
        "package_manifest_uri": package_manifest_uri
        or package_manifest.get("lineage", {}).get("forcing_package_manifest_uri"),
    }


def package_manifest_unit(variable: str) -> str:
    return {
        "PRCP": "mm",
        "TEMP": "degC",
        "RH": "0-1",
        "wind": "m/s",
        "Rn": "W/m2",
        "Press": "Pa",
    }[variable]


def _best_available_lineage(
    config: ProductionMetConfig,
    raw_evidence: Mapping[str, Any],
    canonical_evidence: Mapping[str, Any],
    forcing_qc: Mapping[str, Any],
) -> dict[str, Any]:
    products_by_time: dict[str, list[str]] = {}
    for product in canonical_evidence.get("products", []):
        if not isinstance(product, Mapping):
            continue
        products_by_time.setdefault(str(product["valid_time"]), []).append(str(product["canonical_product_id"]))

    source_modes = {
        str(source["source"]): str(source["execution_mode"])
        for source in raw_evidence.get("sources", [])
        if isinstance(source, Mapping)
    }
    source_statuses = {
        str(source["source"]): str(source["status"])
        for source in raw_evidence.get("sources", [])
        if isinstance(source, Mapping)
    }
    per_valid_time = []
    for forecast_hour in config.forecast_hours:
        valid_time = config.cycle_start + timedelta(hours=forecast_hour)
        valid_time_text = _format_time(valid_time)
        candidates = []
        for source in SOURCE_ORDER:
            mode = source_modes.get(source, "not_executed")
            if source == "GFS" and mode == "deterministic_fixture" and forcing_qc.get("status") == "pass":
                candidates.append({"source": source, "status": "selected", "execution_mode": mode})
            elif mode in {"deterministic_fixture", "live_executed"}:
                candidates.append({"source": source, "status": "available_not_selected", "execution_mode": mode})
            elif mode == "restricted":
                candidates.append({"source": source, "status": "restricted", "execution_mode": mode})
            elif mode == "not_executed" or source_statuses.get(source) in {"unavailable", "not_executed"}:
                candidates.append({"source": source, "status": "not_executed", "execution_mode": mode})
            else:
                candidates.append({"source": source, "status": "skipped", "execution_mode": mode})
        per_valid_time.append(
            {
                "valid_time": valid_time_text,
                "selected_source": "GFS" if forcing_qc.get("status") == "pass" else None,
                "selection_reason": "deterministic_gfs_fixture_passed_forcing_qc"
                if forcing_qc.get("status") == "pass"
                else "forcing_qc_not_passed",
                "canonical_product_ids": sorted(products_by_time.get(valid_time_text, [])),
                "candidates": candidates,
            }
        )
    return {
        "schema": "nhms.production_closure.met.best_available_lineage.v1",
        "status": "ready"
        if raw_evidence.get("status") == "ready" and forcing_qc.get("status") == "pass"
        else "blocked",
        "run_id": config.run_id,
        "per_valid_time": per_valid_time,
        "skipped_or_restricted_sources": [
            {
                "source": source,
                "execution_mode": mode,
                "reason": _source_reason(config, source, _source_status(config, source), mode),
            }
            for source, mode in source_modes.items()
            if mode in {"skipped", "restricted", "not_executed"}
        ],
    }


def _summary(
    config: ProductionMetConfig,
    *,
    status: str,
    blockers: list[dict[str, Any]],
    files: list[str],
    raw_evidence: Mapping[str, Any],
    canonical_evidence: Mapping[str, Any],
    forcing_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    source_modes = [
        str(source.get("execution_mode"))
        for source in raw_evidence.get("sources", [])
        if isinstance(source, Mapping)
    ]
    live_source_count = sum(1 for mode in source_modes if mode == "live_executed")
    deterministic_fixture = live_source_count == 0
    return {
        "schema": "nhms.production_closure.met.v1",
        "issue": 149,
        "run_id": config.run_id,
        "status": status,
        "evidence_dir": str(config.lane_dir),
        "execution_mode": "live_source_ingest" if live_source_count else "deterministic_fixture",
        "deterministic_fixture": deterministic_fixture,
        "live_met_executed": live_source_count > 0,
        "live_source_count": live_source_count,
        "final_production_readiness_claimed": False,
        "object_prefix": config.object_prefix,
        "enabled_sources": list(config.enabled_sources),
        "cycle_time": _format_time(config.cycle_start),
        "forecast_hours": list(config.forecast_hours),
        "model_id": config.model_id,
        "blockers": blockers,
        "raw_total_file_count": raw_evidence.get("total_file_count"),
        "canonical_product_count": canonical_evidence.get("product_count"),
        "forcing_package_uri": forcing_manifest.get("forcing_package_uri"),
        "forcing_checksum": forcing_manifest.get("checksum"),
        "files": [*files, "summary.json"],
    }


def _environment_payload(config: ProductionMetConfig) -> dict[str, Any]:
    env_keys = [
        "NHMS_RUN_PRODUCTION_CLOSURE",
        "NHMS_PRODUCTION_MET_SOURCES",
        "NHMS_PRODUCTION_MET_ACCESS_MODE",
        "NHMS_PRODUCTION_MET_CACHED_FALLBACK_POLICY",
        "NHMS_PRODUCTION_MET_OBJECT_PREFIX",
        "NHMS_PRODUCTION_MET_GFS_ENDPOINT",
        "NHMS_PRODUCTION_MET_IFS_ENDPOINT",
        "NHMS_PRODUCTION_MET_ERA5_ENDPOINT",
        "NHMS_PRODUCTION_MET_CLDAS_ENDPOINT",
        "NHMS_PRODUCTION_MET_ALLOW_LIVE_NETWORK",
        "CDSAPI_KEY",
        "IFS_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
    ]
    return {
        "schema": "nhms.production_closure.met.environment.v1",
        "run_id": config.run_id,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": str(Path.cwd()),
        "env": {key: os.getenv(key, "") for key in env_keys if key in os.environ},
    }


def _result_blockers(*payloads: Mapping[str, Any]) -> list[dict[str, Any]]:
    blockers = []
    for payload in payloads:
        for blocker in payload.get("blockers", []):
            if isinstance(blocker, Mapping):
                blockers.append(dict(blocker))
        status = payload.get("status")
        if status not in {"ready", "pass", "canonical_ready", "forcing_ready"}:
            blockers.append(
                {
                    "error_code": "PRODUCTION_MET_VALIDATION_BLOCKED",
                    "schema": payload.get("schema"),
                    "status": status,
                }
            )
    return blockers


def _validate_config(config: ProductionMetConfig) -> None:
    if config.cycle_end < config.cycle_start:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_CYCLE_WINDOW_INVALID",
            "Cycle window end must be greater than or equal to start.",
        )
    if not config.forecast_hours:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_FORECAST_HOURS_INVALID",
            "At least one forecast hour is required.",
        )
    if len(config.forecast_hours) > config.bounds.max_forecast_hours:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_FORECAST_HOURS_EXCEED_LIMIT",
            "Forecast-hour count exceeds configured production met bound.",
        )
    if any(hour < 0 or hour > 240 for hour in config.forecast_hours):
        raise ProductionMetValidationError(
            "PRODUCTION_MET_FORECAST_HOURS_INVALID",
            "Forecast hours must be between 0 and 240.",
        )
    if int((config.cycle_end - config.cycle_start).total_seconds()) % (3 * 3600) != 0:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_CYCLE_WINDOW_INVALID",
            "Cycle window must align to the 3-hour meteorological forcing step.",
        )
    expected_forecast_hours = _expected_forecast_hours(config)
    missing_hours = sorted(set(expected_forecast_hours) - set(config.forecast_hours))
    if missing_hours:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_FORECAST_HOURS_CYCLE_WINDOW_INCOMPLETE",
            (
                "Forecast hours must cover the configured cycle window at 3-hour intervals; "
                f"missing forecast hours: {missing_hours}."
            ),
        )
    if config.cached_fallback_policy not in {"deterministic_fixture", "disabled", "cached_only"}:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_CACHED_FALLBACK_POLICY_INVALID",
            "Cached fallback policy must be deterministic_fixture, disabled, or cached_only.",
        )
    if config.bounds.max_retries > MAX_RETRIES:
        raise ProductionMetValidationError("PRODUCTION_MET_RETRY_LIMIT_INVALID", "Retry limit is too high.")
    if config.bounds.timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ProductionMetValidationError("PRODUCTION_MET_TIMEOUT_LIMIT_INVALID", "Timeout limit is too high.")
    if config.bounds.max_backoff_seconds > MAX_BACKOFF_SECONDS:
        raise ProductionMetValidationError("PRODUCTION_MET_BACKOFF_LIMIT_INVALID", "Backoff limit is too high.")
    if config.bounds.max_per_file_bytes < MIN_PER_FILE_BYTES:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_FILE_BYTE_LIMIT_TOO_SMALL",
            "Per-file byte limit is below the deterministic fixture safety floor.",
        )


def _validate_deterministic_shape(config: ProductionMetConfig) -> None:
    deterministic_enabled = [
        source
        for source in DETERMINISTIC_SOURCES
        if _source_execution_mode(config, source, _source_status(config, source)) == "deterministic_fixture"
    ]
    entry_count = sum(
        len(_source_variables(source)) * len(config.forecast_hours) + 1 for source in deterministic_enabled
    )
    _enforce_manifest_bound(config, entry_count)
    if deterministic_enabled and config.bounds.max_deterministic_file_bytes < MIN_DETERMINISTIC_RAW_FILE_BYTES:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_FILE_BYTE_LIMIT_EXCEEDED",
            "Deterministic source fixture byte limit is too small for bounded raw source files.",
        )


def _prepare_object_bundle(config: ProductionMetConfig) -> None:
    object_root = config.object_store_root
    resolved_lane = config.lane_dir.resolve(strict=False)
    resolved_object_root = object_root.resolve(strict=False)
    try:
        resolved_object_root.relative_to(resolved_lane)
    except ValueError as error:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_OBJECT_PATH_UNSAFE",
            "Met validation object bundle must stay under the current run_id evidence lane.",
        ) from error
    if object_root.exists() and not config.force:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_OBJECT_BUNDLE_EXISTS",
            "Met validation object bundle already exists. Use --force to replace this same run_id bundle.",
        )
    if object_root.exists() and config.force:
        _refuse_symlink_components(object_root)
        shutil.rmtree(object_root)


def _write_grid_definition(store: LocalObjectStore, config: ProductionMetConfig) -> str:
    key = f"models/{config.model_id}/grids/gfs_0p25.json"
    content = _json_bytes({"grid_id": "gfs_0p25", "cells": [{"grid_cell_id": "0", "lon": 100.0, "lat": 30.0}]})
    return _write_object_guarded(store, key, content, force=config.force)


def _write_object_guarded(store: LocalObjectStore, key: str, content: bytes, *, force: bool) -> str:
    if store.exists(key) and not force:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_OBJECT_EXISTS",
            f"Validation object already exists for this run: {key}. Use --force to replace it.",
        )
    return store.write_bytes_atomic(key, content)


def _deterministic_raw_content(source: str, variable: str, forecast_hour: int, cycle_time: datetime) -> bytes:
    if source == "GFS":
        return encode_test_netcdf4(variable, forecast_hour, cycle_time=cycle_time, source="gfs")
    payload = {
        "schema": "nhms.production_closure.met.raw_fixture.v1",
        "source": source,
        "variable": variable,
        "forecast_hour": forecast_hour,
        "cycle_time": _format_time(cycle_time),
        "values": _deterministic_values(source, variable, forecast_hour),
    }
    return _json_bytes(payload)


def _deterministic_values(source: str, variable: str, forecast_hour: int) -> list[float]:
    if source == "ERA5":
        base = {
            "2m_temperature": 285.0,
            "2m_dewpoint_temperature": 278.0,
            "10m_u_component_of_wind": 3.0,
            "10m_v_component_of_wind": 4.0,
            "surface_pressure": 101325.0,
            "total_precipitation": 0.00025,
            "surface_net_solar_radiation": 3600.0 * 180.0,
            "surface_net_thermal_radiation": -3600.0 * 70.0,
        }[variable]
        return [base * max(1, forecast_hour)]
    base = {
        "2t": 285.0,
        "2d": 278.0,
        "tp": 0.00025,
        "10u": 3.0,
        "10v": 4.0,
        "sp": 101325.0,
        "ssr": 3600.0 * 180.0,
        "str": -3600.0 * 70.0,
    }[variable]
    return [base * max(1, forecast_hour)]


def _raw_manifest_for_converter(source_manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source_manifest["source_id"],
        "cycle_time": source_manifest["cycle_time"],
        "metadata": {
            "forecast_hours": list(source_manifest["selected_forecast_hours"]),
            "execution_mode": source_manifest["execution_mode"],
        },
        "entries": [
            {
                "remote_url": entry["remote_url"],
                "local_key": entry["local_key"],
                "variable": entry["variable"],
                "forecast_hour": entry["forecast_hour"],
            }
            for entry in source_manifest.get("manifest_entries", [])
        ],
    }


def _canonical_converter(
    config: ProductionMetConfig,
    store: LocalObjectStore,
    repository: _MetClosureRepository,
    source: str,
) -> CanonicalConverter:
    if source == "ERA5":
        return ERA5CanonicalConverter(
            config=ERA5CanonicalConverterConfig(
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_prefix,
                grid_definition_uri=f"models/{config.model_id}/grids/era5_0p25.json",
            ),
            repository=repository,
            object_store=store,
        )
    if source == "IFS":
        return IFSCanonicalConverter(
            config=IFSCanonicalConverterConfig(
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_prefix,
                grid_definition_uri=f"models/{config.model_id}/grids/ifs_0p25.json",
            ),
            repository=repository,
            object_store=store,
        )
    return CanonicalConverter(
        config=CanonicalConverterConfig(
            source_id="gfs",
            object_store_root=config.object_store_root,
            object_store_prefix=config.object_prefix,
            grid_definition_uri=f"models/{config.model_id}/grids/gfs_0p25.json",
        ),
        repository=repository,
        object_store=store,
    )


def _canonical_product_payloads(
    repository: _MetClosureRepository,
    result: Any,
) -> list[dict[str, Any]]:
    products = []
    for product in sorted(result.products, key=lambda item: (item.valid_time, item.variable)):
        record = repository.products[product.canonical_product_id]
        products.append(
            {
                "canonical_product_id": product.canonical_product_id,
                "source_id": record["source_id"],
                "source_cycle": f"{record['source_id']}_{format_cycle_time(record['cycle_time'])}",
                "cycle_time": _format_time(record["cycle_time"]),
                "valid_time": _format_time(product.valid_time),
                "lead_time_hours": product.lead_time_hours,
                "variable": product.variable,
                "unit": record["unit"],
                "time_axis": {
                    "cycle_time": _format_time(record["cycle_time"]),
                    "valid_time": _format_time(product.valid_time),
                    "lead_time_hours": product.lead_time_hours,
                },
                "object_uri": product.object_uri,
                "checksum": product.checksum,
                "quality_flag": product.quality_flag,
                "status": product.status,
                "lineage": record["lineage_json"],
            }
        )
    return products


def _stable_negative_failure_checks(
    config: ProductionMetConfig,
    store: LocalObjectStore,
    repository: _MetClosureRepository,
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    del store, repository
    negative_store = LocalObjectStore(
        config.object_store_root / "negative-fixtures",
        f"{config.object_prefix.rstrip('/')}/negative-fixtures",
    )
    negative_repository = _MetClosureRepository(model_id=config.model_id)
    checks = [_missing_raw_failure_check(config, manifest)]
    checks.append(_malformed_raw_failure_check(config, negative_store, negative_repository, manifest))
    checks.append(_nonfinite_forcing_failure_check(config, negative_store, negative_repository, manifest))
    checks.append(_out_of_range_forcing_failure_check(config, negative_store, negative_repository, manifest))
    requested = os.getenv(NEGATIVE_FIXTURE_ENV, "").strip().lower()
    if requested:
        checks.append(_requested_negative_fixture_check(config, requested, checks))
    return checks


def _missing_raw_failure_check(config: ProductionMetConfig, manifest: Mapping[str, Any]) -> dict[str, Any]:
    malformed = dict(manifest)
    malformed["entries"] = [
        entry for entry in manifest["entries"] if not (entry["variable"] == "dswrf" and entry["forecast_hour"] == 3)
    ]
    missing = {
        "native_variable": "dswrf",
        "standard_variable": "shortwave_down",
        "forecast_hour": 3,
    }
    return {
        "status": "blocked",
        "error_code": "PRODUCTION_MET_RAW_MISSING_REQUIRED_VARIABLE",
        "downstream_forcing_ready": False,
        "source_id": "gfs",
        "cycle_time": _format_time(config.cycle_start),
        "malformed_manifest_entry_count": len(malformed["entries"]),
        "missing": missing,
    }


def _malformed_raw_failure_check(
    config: ProductionMetConfig,
    store: LocalObjectStore,
    repository: _MetClosureRepository,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    check_manifest = _copy_manifest_for_negative_check(manifest, "malformed_raw")
    target = check_manifest["entries"][0]
    bad_key = str(target["local_key"])
    _write_object_guarded(store, bad_key, b"not-a-netcdf", force=True)
    converter = _canonical_converter(config, store, repository, "GFS")
    return _conversion_failure_payload(
        config,
        check_manifest,
        converter,
        error_code="PRODUCTION_MET_RAW_MALFORMED",
        failure_type="malformed_raw",
    )


def _nonfinite_forcing_failure_check(
    config: ProductionMetConfig,
    store: LocalObjectStore,
    repository: _MetClosureRepository,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    check_manifest = _copy_manifest_for_negative_check(manifest, "nonfinite")
    _replace_gfs_fixture_value(config, store, check_manifest, variable="tmp2m", forecast_hour=0, value=float("nan"))
    converter = _canonical_converter(config, store, repository, "GFS")
    try:
        result = converter.convert_manifest(check_manifest)
        rows = _forcing_rows_for_products(repository, result, store)
        qc = _forcing_qc_payload(
            rows,
            {"lineage": {}},
            expected_valid_times=_expected_valid_times(config),
            package_uri="negative-fixture://nonfinite",
            package_manifest_uri="negative-fixture://nonfinite/forcing_package.json",
        )
    except CanonicalConversionError as error:
        return _negative_blocked_payload(
            config,
            "PRODUCTION_MET_RAW_NONFINITE",
            "nonfinite",
            str(error),
            source_id="gfs",
        )
    return _qc_failure_payload(config, "PRODUCTION_MET_QC_NONFINITE", "nonfinite", qc)


def _out_of_range_forcing_failure_check(
    config: ProductionMetConfig,
    store: LocalObjectStore,
    repository: _MetClosureRepository,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    check_manifest = _copy_manifest_for_negative_check(manifest, "out_of_range")
    _replace_gfs_fixture_value(config, store, check_manifest, variable="pressfc", forecast_hour=0, value=200000.0)
    converter = _canonical_converter(config, store, repository, "GFS")
    try:
        result = converter.convert_manifest(check_manifest)
        rows = _forcing_rows_for_products(repository, result, store)
        qc = _forcing_qc_payload(
            rows,
            {"lineage": {}},
            expected_valid_times=_expected_valid_times(config),
            package_uri="negative-fixture://out-of-range",
            package_manifest_uri="negative-fixture://out-of-range/forcing_package.json",
        )
    except CanonicalConversionError as error:
        return _negative_blocked_payload(
            config,
            "PRODUCTION_MET_RAW_OUT_OF_RANGE",
            "out_of_range",
            str(error),
            source_id="gfs",
        )
    return _qc_failure_payload(config, "PRODUCTION_MET_QC_OUT_OF_RANGE", "out_of_range", qc)


def _copy_manifest_for_negative_check(manifest: Mapping[str, Any], suffix: str) -> dict[str, Any]:
    copied = json.loads(json.dumps(manifest))
    for entry in copied["entries"]:
        local_key = str(entry["local_key"])
        if "/raw/gfs/" in f"/{local_key}":
            entry["local_key"] = local_key.replace("/raw/gfs/", f"/raw_negative/{suffix}/gfs/")
        elif local_key.startswith("raw/gfs/"):
            entry["local_key"] = local_key.replace("raw/gfs/", f"raw_negative/{suffix}/gfs/", 1)
    return copied


def _replace_gfs_fixture_value(
    config: ProductionMetConfig,
    store: LocalObjectStore,
    manifest: Mapping[str, Any],
    *,
    variable: str,
    forecast_hour: int,
    value: float,
) -> None:
    for entry in manifest["entries"]:
        if entry["variable"] == variable and int(entry["forecast_hour"]) == forecast_hour:
            content = encode_test_netcdf4(variable, forecast_hour, values=[value], cycle_time=config.cycle_start)
            _write_object_guarded(store, str(entry["local_key"]), content, force=True)
        else:
            content = _deterministic_raw_content(
                "GFS",
                str(entry["variable"]),
                int(entry["forecast_hour"]),
                config.cycle_start,
            )
            _write_object_guarded(store, str(entry["local_key"]), content, force=True)


def _conversion_failure_payload(
    config: ProductionMetConfig,
    manifest: Mapping[str, Any],
    converter: CanonicalConverter,
    *,
    error_code: str,
    failure_type: str,
) -> dict[str, Any]:
    try:
        converter.convert_manifest(manifest)
    except CanonicalConversionError as error:
        return _negative_blocked_payload(config, error_code, failure_type, str(error), source_id="gfs")
    return {
        "status": "unexpected_pass",
        "error_code": error_code,
        "failure_type": failure_type,
        "downstream_forcing_ready": True,
        "source_id": "gfs",
        "cycle_time": _format_time(config.cycle_start),
    }


def _forcing_rows_for_products(
    repository: _MetClosureRepository,
    result: Any,
    store: LocalObjectStore,
) -> list[ForcingTimeseriesRow]:
    forcing_version_id = "negative_fixture_forcing"
    canonical_to_forcing = {
        "prcp_rate_or_amount": "PRCP",
        "air_temperature_2m": "TEMP",
        "relative_humidity_2m": "RH",
        "wind_u_10m": "wind",
        "wind_speed": "wind",
        "shortwave_down": "Rn",
        "net_radiation": "Rn",
        "pressure_surface": "Press",
        "surface_pressure": "Press",
    }
    selected_by_time_variable: dict[tuple[datetime, str], ForcingTimeseriesRow] = {}
    for product in result.products:
        forcing_variable = canonical_to_forcing.get(product.variable)
        if forcing_variable is None:
            continue
        record = repository.products[product.canonical_product_id]
        values = _canonical_product_values(repository, product.canonical_product_id, store)
        selected_by_time_variable[(product.valid_time, forcing_variable)] = ForcingTimeseriesRow(
            forcing_version_id=forcing_version_id,
            basin_version_id="basin_v1",
            station_id="station_1",
            valid_time=product.valid_time,
            source_id=str(record["source_id"]),
            variable=forcing_variable,
            value=float(values[0]) if values else float("nan"),
            unit=package_manifest_unit(forcing_variable),
            native_resolution=str(record.get("native_time_resolution") or "3h"),
            quality_flag=str(record.get("quality_flag", "ok")),
        )
    return list(selected_by_time_variable.values())


def _canonical_product_values(
    repository: _MetClosureRepository,
    product_id: str,
    store: LocalObjectStore,
) -> list[float]:
    import xarray as xr

    record = repository.products[product_id]
    object_uri = str(record["object_uri"])
    file_path = store.resolve_path(object_uri)
    dataset = xr.open_dataset(file_path, engine="netcdf4")
    try:
        variable = str(record["variable"])
        return [float(value) for value in dataset[variable].values.ravel().tolist()]
    finally:
        dataset.close()


def _qc_failure_payload(
    config: ProductionMetConfig,
    error_code: str,
    failure_type: str,
    qc: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "status": "blocked" if qc.get("status") == "fail" else "unexpected_pass",
        "error_code": error_code,
        "failure_type": failure_type,
        "downstream_forcing_ready": False if qc.get("status") == "fail" else True,
        "source_id": "gfs",
        "cycle_time": _format_time(config.cycle_start),
        "qc_status": qc.get("status"),
        "missing_values": qc.get("missing_values"),
        "range_checks": qc.get("range_checks"),
    }


def _negative_blocked_payload(
    config: ProductionMetConfig,
    error_code: str,
    failure_type: str,
    error_message: str,
    *,
    source_id: str,
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "error_code": error_code,
        "failure_type": failure_type,
        "downstream_forcing_ready": False,
        "source_id": source_id,
        "cycle_time": _format_time(config.cycle_start),
        "error_message": error_message,
    }


def _requested_negative_fixture_check(
    config: ProductionMetConfig,
    requested: str,
    checks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    for check in checks:
        if check.get("failure_type") == requested:
            return {
                "status": "blocked",
                "error_code": "PRODUCTION_MET_NEGATIVE_FIXTURE_REQUESTED",
                "failure_type": requested,
                "downstream_forcing_ready": False,
                "source_id": "gfs",
                "cycle_time": _format_time(config.cycle_start),
                "affected_source_blocked": check.get("status") == "blocked",
            }
    return {
        "status": "blocked",
        "error_code": "PRODUCTION_MET_NEGATIVE_FIXTURE_UNKNOWN",
        "failure_type": requested,
        "downstream_forcing_ready": False,
        "source_id": "gfs",
        "cycle_time": _format_time(config.cycle_start),
    }


def _forcing_qc_rows_by_variable(rows: Sequence[ForcingTimeseriesRow]) -> dict[str, list[ForcingTimeseriesRow]]:
    grouped: dict[str, list[ForcingTimeseriesRow]] = {}
    for row in rows:
        grouped.setdefault(row.variable, []).append(row)
    return grouped


def _continuity_check(
    valid_times: Sequence[datetime],
    *,
    expected_valid_times: Sequence[datetime] | None = None,
) -> dict[str, Any]:
    expected = tuple(expected_valid_times or ())
    if not valid_times:
        return {
            "status": "fail",
            "reason": "no_valid_times",
            "expected_step_hours": 3,
            "expected_valid_times": [_format_time(value) for value in expected],
            "missing_valid_times": [_format_time(value) for value in expected],
        }
    deltas = [
        (later - earlier).total_seconds() / 3600.0
        for earlier, later in zip(valid_times, valid_times[1:], strict=False)
    ]
    observed = set(valid_times)
    missing = [value for value in expected if value not in observed]
    unexpected = [value for value in valid_times if expected and value not in expected]
    deltas_pass = all(delta == 3.0 for delta in deltas)
    expected_pass = not missing and not unexpected
    return {
        "status": "pass" if deltas_pass and expected_pass else "fail",
        "expected_step_hours": 3,
        "expected_valid_times": [_format_time(value) for value in expected],
        "observed_valid_times": [_format_time(value) for value in valid_times],
        "observed_deltas_hours": deltas,
        "missing_valid_times": [_format_time(value) for value in missing],
        "unexpected_valid_times": [_format_time(value) for value in unexpected],
    }


def _source_manifest(raw_evidence: Mapping[str, Any], source: str) -> Mapping[str, Any] | None:
    for entry in raw_evidence.get("sources", []):
        if isinstance(entry, Mapping) and entry.get("source") == source:
            return entry
    return None


def _source_variables(source: str) -> tuple[str, ...]:
    if source == "GFS":
        return GFS_VARIABLES
    if source == "IFS":
        return IFS_VARIABLES
    if source == "ERA5":
        return ERA5_VARIABLES
    return ()


def _source_status(config: ProductionMetConfig, source: str) -> str:
    if source == "CLDAS":
        return "restricted"
    return "enabled" if source in config.enabled_sources else "disabled"


def _source_configured_execution_mode(config: ProductionMetConfig, source: str, status: str) -> str:
    if status == "restricted":
        return "restricted"
    if status == "disabled":
        return "skipped"
    live_requested = _truthy_env(os.getenv(f"NHMS_PRODUCTION_MET_LIVE_{source}"))
    live_allowed = _truthy_env(os.getenv("NHMS_PRODUCTION_MET_ALLOW_LIVE_NETWORK"))
    if live_requested and live_allowed:
        return "not_executed"
    if config.cached_fallback_policy == "disabled":
        return "not_executed"
    return "deterministic_fixture"


def _source_execution_mode(config: ProductionMetConfig, source: str, status: str) -> str:
    configured_execution_mode = _source_configured_execution_mode(config, source, status)
    if source in {"IFS", "ERA5"} and configured_execution_mode == "deterministic_fixture":
        return "skipped"
    return configured_execution_mode


def _source_reason(config: ProductionMetConfig, source: str, status: str, execution_mode: str) -> str:
    if source == "CLDAS":
        return config.cldas_restricted_reason
    if status == "disabled":
        return "source not included in enabled source subset"
    if source in {"IFS", "ERA5"} and execution_mode == "skipped":
        return (
            f"{source} deterministic fixture is configured but skipped in this closure lane because "
            "raw-to-canonical and forcing lineage is selected for GFS only"
        )
    if execution_mode == "not_executed":
        if config.cached_fallback_policy == "disabled":
            return "cached/deterministic fallback policy is disabled and no live executor is available"
        return "live source gate was enabled, but this closure lane has no network executor; no live success is claimed"
    if config.cached_fallback_policy == "cached_only":
        return "cached deterministic production-like fixture used by cached-only validation"
    return "deterministic production-like fixture used by fast validation"


def _source_auth_reference(source: str, execution_mode: str) -> str:
    if execution_mode == "deterministic_fixture":
        return "public-path-or-none"
    if execution_mode == "skipped":
        return "not-required"
    if source == "ERA5":
        return "env:CDSAPI_KEY"
    if source == "IFS":
        return "env:IFS_API_KEY"
    if source == "GFS":
        return "public-path"
    return "restricted"


def _default_endpoint(source: str) -> str:
    return {
        "GFS": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod",
        "IFS": "https://data.ecmwf.int/forecasts",
        "ERA5": "https://cds.climate.copernicus.eu/api",
        "CLDAS": "restricted://cldas",
    }[source]


def _bounds_payload(bounds: ProductionMetBounds) -> dict[str, Any]:
    return {
        "max_manifest_entries": bounds.max_manifest_entries,
        "max_per_file_bytes": bounds.max_per_file_bytes,
        "max_forecast_hours": bounds.max_forecast_hours,
        "max_retries": bounds.max_retries,
        "timeout_seconds": bounds.timeout_seconds,
        "max_backoff_seconds": bounds.max_backoff_seconds,
        "max_evidence_payload_bytes": bounds.max_evidence_payload_bytes,
        "max_deterministic_file_bytes": bounds.max_deterministic_file_bytes,
    }


def _enforce_manifest_bound(config: ProductionMetConfig, entry_count: int) -> None:
    if entry_count > config.bounds.max_manifest_entries:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_MANIFEST_ENTRY_LIMIT_EXCEEDED",
            "Source manifest entry count exceeds configured production met bound.",
        )


def _enforce_per_file_bound(config: ProductionMetConfig, key: str, content: bytes) -> None:
    if len(content) > config.bounds.max_per_file_bytes or len(content) > config.bounds.max_deterministic_file_bytes:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_FILE_BYTE_LIMIT_EXCEEDED",
            f"Deterministic source object {key} exceeds configured byte limit.",
        )


def _parse_sources(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return DEFAULT_SOURCE_SUBSET
    sources = []
    for part in value.split(","):
        normalized = part.strip().upper()
        if not normalized:
            continue
        if normalized not in SOURCE_ORDER:
            raise ProductionMetValidationError(
                "PRODUCTION_MET_SOURCE_INVALID",
                f"Unsupported met source {part!r}; expected GFS, IFS, ERA5, or CLDAS.",
            )
        if normalized == "CLDAS":
            continue
        sources.append(normalized)
    return tuple(dict.fromkeys(sources))


def _parse_forecast_hours(value: str | None) -> tuple[int, ...]:
    if value is None or not value.strip():
        return DEFAULT_FORECAST_HOURS
    hours = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hours.append(int(part, 10))
        except ValueError as error:
            raise ProductionMetValidationError(
                "PRODUCTION_MET_FORECAST_HOURS_INVALID",
                "Forecast hours must be comma-separated integers.",
            ) from error
    return tuple(sorted(dict.fromkeys(hours)))


def _positive_int_env(env_name: str, default: int, *, maximum: int) -> int:
    raw_value = os.getenv(env_name)
    if raw_value is None or raw_value == "":
        return default
    try:
        value = int(raw_value, 10)
    except ValueError as error:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_BOUND_INVALID",
            f"{env_name} must be an integer.",
        ) from error
    if value < 1 or value > maximum:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_BOUND_INVALID",
            f"{env_name} must be between 1 and {maximum}.",
        )
    return value


def _nonnegative_int_env(env_name: str, default: int, *, maximum: int) -> int:
    raw_value = os.getenv(env_name)
    if raw_value is None or raw_value == "":
        return default
    try:
        value = int(raw_value, 10)
    except ValueError as error:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_BOUND_INVALID",
            f"{env_name} must be an integer.",
        ) from error
    if value < 0 or value > maximum:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_BOUND_INVALID",
            f"{env_name} must be between 0 and {maximum}.",
        )
    return value


def _positive_float_env(env_name: str, default: float, *, maximum: float) -> float:
    raw_value = os.getenv(env_name)
    if raw_value is None or raw_value == "":
        return default
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_BOUND_INVALID",
            f"{env_name} must be a number.",
        ) from error
    if value <= 0.0 or value > maximum:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_BOUND_INVALID",
            f"{env_name} must be greater than 0 and at most {maximum}.",
        )
    return value


def _safe_run_id(run_id: str) -> str:
    if SAFE_RUN_ID_RE.fullmatch(run_id):
        return run_id
    raise ProductionMetValidationError(
        "PRODUCTION_MET_RUN_ID_UNSAFE",
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
            raise ProductionMetValidationError(
                "PRODUCTION_MET_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )


def _validate_object_prefix_safe(prefix: str) -> None:
    if not prefix:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_OBJECT_PREFIX_INVALID",
            "Met object prefix must not be empty.",
        )
    try:
        parsed = urlsplit(prefix)
    except ValueError as error:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_OBJECT_PREFIX_UNSAFE",
            "Met object prefix must not contain credential material.",
        ) from error
    if not parsed.scheme or not parsed.netloc:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_OBJECT_PREFIX_INVALID",
            "Met object prefix must be an object URI prefix such as s3://bucket/prefix.",
        )
    if parsed.username or parsed.password:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_OBJECT_PREFIX_UNSAFE",
            "Met object prefix must not contain userinfo credentials.",
        )
    if parsed.query or parsed.fragment:
        raise ProductionMetValidationError(
            "PRODUCTION_MET_OBJECT_PREFIX_UNSAFE",
            "Met object prefix must not contain query parameters or fragments.",
        )
    for raw_segment in parsed.path.split("/"):
        segment = unquote(raw_segment)
        if "/" in segment or "\\" in segment or segment in {".", ".."}:
            raise ProductionMetValidationError(
                "PRODUCTION_MET_OBJECT_PREFIX_UNSAFE",
                "Met object prefix path segments must not contain '.', '..', or decoded path separators.",
            )
        decoded_parts = SENSITIVE_PREFIX_SEPARATOR_RE.split(segment)
        if any(SENSITIVE_PREFIX_ASSIGNMENT_RE.search(part) for part in decoded_parts):
            raise ProductionMetValidationError(
                "PRODUCTION_MET_OBJECT_PREFIX_UNSAFE",
                "Met object prefix path segments must not contain credential assignments.",
            )


def _configured_endpoint(source: str) -> str:
    return redact_text(os.getenv(f"NHMS_PRODUCTION_MET_{source}_ENDPOINT", _default_endpoint(source)))


def _expected_forecast_hours(config: ProductionMetConfig) -> tuple[int, ...]:
    window_hours = int((config.cycle_end - config.cycle_start).total_seconds() // 3600)
    return tuple(range(0, window_hours + 1, 3))


def _expected_valid_times(config: ProductionMetConfig) -> tuple[datetime, ...]:
    return tuple(config.cycle_start + timedelta(hours=hour) for hour in _expected_forecast_hours(config))


def _run_scoped_prefix(prefix: str, run_id: str) -> str:
    parsed = urlsplit(prefix.rstrip("/"))
    path = parsed.path.rstrip("/")
    run_segment = f"/runs/{run_id}/met"
    if path.endswith(run_segment):
        scoped_path = path
    else:
        scoped_path = f"{path}{run_segment}" if path else run_segment
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, scoped_path, "", ""))


def _directory_uri(store: LocalObjectStore, key: str) -> str:
    return store.uri_for_key(key.rstrip("/") + "/")


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _truthy_env(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}
