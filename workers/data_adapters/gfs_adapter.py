from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from packages.common.met_store import PsycopgMetStore
from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
from packages.common.redaction import redact_payload

from .base import (
    CycleDiscovery,
    DataSourceAdapter,
    DownloadFileResult,
    DownloadManifest,
    DownloadPlanResult,
    ManifestEntry,
    VerificationFailure,
    VerificationResult,
    cycle_id_for,
    format_cycle_time,
    generate_segmented_forecast_hours,
    parse_cycle_date,
    parse_cycle_time,
    parse_resolution_segments,
    valid_time_for,
    validate_forecast_hours,
)
from .cycle_hours import env_cycle_hours_utc, normalize_cycle_hours_utc
from .region import GeoBBox, china_buffered_bbox_from_env

LOGGER = logging.getLogger(__name__)

GFS_VARIABLES: tuple[str, ...] = ("tmp2m", "apcp", "rh2m", "u10m", "v10m", "pressfc", "dswrf")
NOMADS_QUERY_PARAMS: dict[str, dict[str, str]] = {
    "tmp2m": {"var_TMP": "on", "lev_2_m_above_ground": "on"},
    "apcp": {"var_APCP": "on", "lev_surface": "on"},
    "rh2m": {"var_RH": "on", "lev_2_m_above_ground": "on"},
    "u10m": {"var_UGRD": "on", "lev_10_m_above_ground": "on"},
    "v10m": {"var_VGRD": "on", "lev_10_m_above_ground": "on"},
    "pressfc": {"var_PRES": "on", "lev_surface": "on"},
    "dswrf": {"var_DSWRF": "on", "lev_surface": "on"},
}

# Backend dispatch chain. The cloud mirrors (s3/gcs/azure/ftpprd) serve the full
# global GRIB + .idx for a cycle and are subset locally via .idx Range GETs plus a
# cdo bbox clip; NOMADS is the grib-filter server-side-subset last-resort fallback
# (rate-limit/403 prone). The order is the default try-chain.
GFS_CLOUD_BACKENDS: tuple[str, ...] = ("s3", "gcs", "azure", "ftpprd")
GFS_NOMADS_BACKEND = "nomads"
GFS_DEFAULT_SOURCE_BACKENDS: tuple[str, ...] = (*GFS_CLOUD_BACKENDS, GFS_NOMADS_BACKEND)
GFS_DEFAULT_CYCLE_HOURS_UTC: tuple[int, ...] = (0, 6, 12, 18)

GFS_MIRROR_BASE_URL_ENV: dict[str, str] = {
    "s3": "GFS_S3_BASE_URL",
    "gcs": "GFS_GCS_BASE_URL",
    "azure": "GFS_AZURE_BASE_URL",
    "ftpprd": "GFS_FTPPRD_BASE_URL",
}
GFS_MIRROR_BASE_URL_DEFAULT: dict[str, str] = {
    "s3": "https://noaa-gfs-bdp-pds.s3.amazonaws.com",
    "gcs": "https://storage.googleapis.com/global-forecast-system",
    "azure": "https://noaagfs.blob.core.windows.net/gfs",
    "ftpprd": "https://ftpprd.ncep.noaa.gov/data/nccf/com/gfs/prod",
}

# .idx record matching: each native GFS key maps to the GRIB field name + level
# token that wgrib2 writes into the .idx (``<rec>:<start>:d=YYYYMMDDHH:VAR:level:type:``).
# These match the record's ``VAR:level:`` segment; the forecast-time segment is matched
# separately (anl for f000 instantaneous fields, ``<fh> hour fcst`` otherwise).
GFS_IDX_FIELD_LEVEL: dict[str, tuple[str, str]] = {
    "tmp2m": ("TMP", "2 m above ground"),
    "rh2m": ("RH", "2 m above ground"),
    "u10m": ("UGRD", "10 m above ground"),
    "v10m": ("VGRD", "10 m above ground"),
    "pressfc": ("PRES", "surface"),
    "apcp": ("APCP", "surface"),
    "dswrf": ("DSWRF", "surface"),
}
GFS_GRIB_SHORT_NAME: dict[str, str] = {
    "tmp2m": "2t",
    "rh2m": "2r",
    "u10m": "10u",
    "v10m": "10v",
    "pressfc": "sp",
    "apcp": "tp",
    "dswrf": "sdswrf",
}
# apcp/dswrf are accumulated/averaged fields that are undefined at the f000 analysis
# time; the cloud .idx omits them there, matching NOMADS which has no f000 APCP/DSWRF.
# Keep f000 in the manifest for instantaneous fields. The forcing producer maps
# f003 interval products back onto the cycle-start SHUD forcing row.
GFS_F000_UNAVAILABLE_VARIABLES: frozenset[str] = frozenset({"apcp", "dswrf"})
GFS_IDX_SELECTOR_SCHEMA_VERSION = "gfs-idx-selector-v3"
GFS_APCP_SELECTOR_POLICY = "prefer_cycle_cumulative_else_unique_interval_bucket"

CDO_CLIP_TIMEOUT_SECONDS = 300


class ForecastCycleRepository(Protocol):
    def ensure_data_source(
        self,
        *,
        source_id: str,
        source_name: str,
        source_type: str,
        status: str,
        native_format: str,
        adapter_name: str,
        config_json: Mapping[str, Any] | None = None,
        license_status: str | None = None,
    ) -> dict[str, Any]: ...

    def upsert_forecast_cycle(
        self,
        *,
        cycle_id: str,
        source_id: str,
        cycle_time: datetime,
        status: str,
        issue_time: datetime | None = None,
        manifest_uri: str | None = None,
        retry_count: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]: ...

    def update_forecast_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str | None = None,
        manifest_uri: str | None = None,
        retry_count: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None: ...

    def get_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any] | None: ...


class GFSAdapterError(RuntimeError):
    error_code = "GFS_ADAPTER_ERROR"

    def __init__(self, message: str, *, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


class FileUnavailableError(GFSAdapterError):
    error_code = "HTTP_404"


class ForbiddenSourceError(GFSAdapterError):
    error_code = "HTTP_403"


class NetworkDownloadError(GFSAdapterError):
    error_code = "NETWORK_ERROR"


class PollingTimeoutError(GFSAdapterError):
    error_code = "POLL_TIMEOUT"


class ChecksumMismatchError(GFSAdapterError):
    error_code = "CHECKSUM_MISMATCH"


class FileTooLargeError(GFSAdapterError):
    error_code = "FILE_TOO_LARGE"


class RateLimitedError(GFSAdapterError):
    """Mirror returned 503/429; the backend should cool down and the chain advance."""

    error_code = "RATE_LIMITED"

    def __init__(self, message: str, *, retry_after_seconds: float | None = None, attempts: int = 0) -> None:
        super().__init__(message, attempts=attempts)
        self.retry_after_seconds = retry_after_seconds


class IdxParseError(GFSAdapterError):
    error_code = "IDX_PARSE_ERROR"


class IdxRecordNotFoundError(GFSAdapterError):
    """The requested variable is absent from a cloud mirror .idx (treated as 404)."""

    error_code = "HTTP_404"


class AmbiguousIdxRecordError(GFSAdapterError):
    """Multiple .idx records remain after applying a variable-specific selector."""

    error_code = "IDX_AMBIGUOUS_RECORD"


class IdxSelectorPolicyError(GFSAdapterError):
    """A present .idx field violates the source-specific selector policy."""

    error_code = "IDX_SELECTOR_POLICY_ERROR"


class CdoMissingError(GFSAdapterError):
    error_code = "CDO_MISSING"


class CdoClipError(GFSAdapterError):
    error_code = "CDO_CLIP_FAILED"


class AllSourcesUnavailableError(GFSAdapterError):
    error_code = "GFS_ALL_SOURCES_UNAVAILABLE"


@dataclass(frozen=True)
class DownloadedPayload:
    content: bytes
    checksum: str
    bytes_written: int


@dataclass(frozen=True)
class IdxSelection:
    byte_range: tuple[int, int | None]
    step_range: str | None = None
    accumulation_type: str | None = None
    record_number: int | None = None
    selector_warning: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if self.step_range is not None:
            metadata["step_range"] = self.step_range
        if self.accumulation_type is not None:
            metadata["accumulation_type"] = self.accumulation_type
        if self.record_number is not None:
            metadata["idx_record_number"] = self.record_number
        if self.selector_warning:
            metadata["selector_warning"] = self.selector_warning
        return metadata


def _parse_source_backends(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return GFS_DEFAULT_SOURCE_BACKENDS
    backends = tuple(token.strip() for token in raw.split(",") if token.strip())
    return backends or GFS_DEFAULT_SOURCE_BACKENDS


def _mirror_base_urls_from_env() -> dict[str, str]:
    return {
        backend: os.getenv(GFS_MIRROR_BASE_URL_ENV[backend], GFS_MIRROR_BASE_URL_DEFAULT[backend])
        for backend in GFS_CLOUD_BACKENDS
    }


@dataclass(frozen=True)
class GFSAdapterConfig:
    source_id: str = "gfs"
    source_name: str = "gfs"
    source_type: str = "forecast"
    status: str = "enabled"
    native_format: str = "GRIB2"
    adapter_name: str = "gfs_adapter"
    base_url: str = field(
        default_factory=lambda: os.getenv(
            "GFS_NOMADS_BASE_URL",
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod",
        )
    )
    # Ordered backend try-chain. Cloud mirrors are tried first (idx+Range+local cdo
    # clip); NOMADS grib-filter is the server-side-subset last resort.
    source_backends: tuple[str, ...] = field(
        default_factory=lambda: _parse_source_backends(os.getenv("GFS_SOURCE_BACKENDS"))
    )
    mirror_base_urls: Mapping[str, str] = field(default_factory=_mirror_base_urls_from_env)
    # NOMADS 403 circuit-breaker cooldown: once a 403 is seen, NOMADS is skipped for
    # this long (sbatch re-runs as fresh processes, so the breaker is persisted to disk).
    nomads_cooldown_minutes: float = field(
        default_factory=lambda: float(os.getenv("GFS_NOMADS_403_COOLDOWN_MINUTES", "60"))
    )
    # Minimum spacing between NOMADS requests (politeness throttle, NOMADS only).
    nomads_min_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv("GFS_NOMADS_MIN_INTERVAL_SECONDS", "10"))
    )
    # Cloud-mirror cooldown applied on 503/429 before advancing the chain.
    mirror_cooldown_minutes: float = field(
        default_factory=lambda: float(os.getenv("GFS_MIRROR_COOLDOWN_MINUTES", "30"))
    )
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_root: Path | str = field(default_factory=lambda: os.getenv("OBJECT_STORE_ROOT", ""))
    object_store_prefix: str = field(default_factory=lambda: os.getenv("OBJECT_STORE_PREFIX", ""))
    cycle_hours_utc: tuple[int, ...] = field(
        default_factory=lambda: env_cycle_hours_utc("GFS_CYCLE_HOURS_UTC", GFS_DEFAULT_CYCLE_HOURS_UTC)
    )
    forecast_start_hour: int = field(default_factory=lambda: int(os.getenv("GFS_FORECAST_START_HOUR", "0")))
    forecast_end_hour: int = field(default_factory=lambda: int(os.getenv("GFS_FORECAST_END_HOUR", "168")))
    forecast_step_hours: int = 3
    # Optional piecewise native resolution, e.g. GFS "120:1,384:3" (hourly to 120h,
    # 3-hourly beyond). When unset, the uniform forecast_step_hours grid is used.
    forecast_resolution_segments: tuple[tuple[int, int], ...] | None = field(
        default_factory=lambda: parse_resolution_segments(os.getenv("GFS_FORECAST_RESOLUTION_SEGMENTS"))
    )
    variables: tuple[str, ...] = GFS_VARIABLES
    poll_interval_seconds: float = 300.0
    max_wait_seconds: float = 21600.0
    max_retries: int = 3
    retry_backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0)
    request_timeout_seconds: float = 30.0
    min_file_size_bytes: int = 1
    download_chunk_size_bytes: int = field(
        default_factory=lambda: int(os.getenv("GFS_DOWNLOAD_CHUNK_SIZE_BYTES", str(8 * 1024 * 1024)))
    )
    max_file_size_bytes: int = field(
        default_factory=lambda: int(os.getenv("GFS_MAX_FILE_SIZE_BYTES", str(500 * 1024 * 1024)))
    )
    bbox: GeoBBox = field(default_factory=china_buffered_bbox_from_env)

    def forecast_hours(self) -> list[int]:
        if self.forecast_resolution_segments:
            return generate_segmented_forecast_hours(
                self.forecast_start_hour,
                self.forecast_end_hour,
                self.forecast_resolution_segments,
            )
        return list(range(self.forecast_start_hour, self.forecast_end_hour + 1, self.forecast_step_hours))

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cycle_hours_utc",
            normalize_cycle_hours_utc(self.cycle_hours_utc, field_name="cycle_hours_utc"),
        )
        if not str(self.object_store_root):
            object.__setattr__(self, "object_store_root", self.workspace_root)

    def cloud_backends(self) -> tuple[str, ...]:
        return tuple(backend for backend in self.source_backends if backend != GFS_NOMADS_BACKEND)

    def uses_nomads(self) -> bool:
        return GFS_NOMADS_BACKEND in self.source_backends

    def mirror_base_url(self, backend: str) -> str:
        return self.mirror_base_urls.get(backend, GFS_MIRROR_BASE_URL_DEFAULT.get(backend, ""))

    def as_data_source_config(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "source_backends": list(self.source_backends),
            "mirror_base_urls": dict(self.mirror_base_urls),
            "nomads_cooldown_minutes": self.nomads_cooldown_minutes,
            "nomads_min_interval_seconds": self.nomads_min_interval_seconds,
            "mirror_cooldown_minutes": self.mirror_cooldown_minutes,
            "cycle_hours_utc": list(self.cycle_hours_utc),
            "forecast_hours": {
                "start": self.forecast_start_hour,
                "end": self.forecast_end_hour,
                "step": self.forecast_step_hours,
            },
            "variables": list(self.variables),
            "poll_interval_seconds": self.poll_interval_seconds,
            "max_wait_seconds": self.max_wait_seconds,
            "max_retries": self.max_retries,
            "download_chunk_size_bytes": self.download_chunk_size_bytes,
            "max_file_size_bytes": self.max_file_size_bytes,
            "bbox": self.bbox.as_dict(),
        }


class GFSAdapter(DataSourceAdapter):
    def __init__(
        self,
        *,
        config: GFSAdapterConfig | None = None,
        repository: ForecastCycleRepository | None = None,
        object_store: LocalObjectStore | None = None,
        downloader: Callable[[str], bytes | DownloadedPayload] | None = None,
        availability_checker: Callable[[str], bool] | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or GFSAdapterConfig()
        self.repository = repository
        self.object_store = object_store or LocalObjectStore(
            self.config.object_store_root,
            object_store_prefix=self.config.object_store_prefix,
        )
        self._has_injected_downloader = downloader is not None
        self.downloader = downloader or self._download_url
        self.availability_checker = availability_checker or self._url_exists
        self.sleeper = sleeper or time.sleep
        self.clock = clock or time.monotonic
        # Wall-clock is injectable so circuit-breaker cooldown windows are testable;
        # it is distinct from ``clock`` (monotonic, used for in-process deadlines)
        # because the breaker state is persisted across sbatch processes.
        self.wall_clock = wall_clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_env(cls) -> GFSAdapter:
        config = GFSAdapterConfig()
        return cls(config=config, repository=PsycopgMetStore.from_env())

    def initialize_data_source(self) -> dict[str, Any] | None:
        if self.repository is None:
            return None

        try:
            return self.repository.ensure_data_source(
                source_id=self.config.source_id,
                source_name=self.config.source_name,
                source_type=self.config.source_type,
                status=self.config.status,
                native_format=self.config.native_format,
                adapter_name=self.config.adapter_name,
                config_json=self.config.as_data_source_config(),
            )
        except Exception:
            LOGGER.exception("Failed to initialize met.data_source for %s", self.config.source_id)
            raise

    def discover_cycles(self, cycle_date: str | date | datetime) -> list[CycleDiscovery]:
        self.initialize_data_source()
        target_date = parse_cycle_date(cycle_date)
        discoveries: list[CycleDiscovery] = []

        for cycle_hour in self.config.cycle_hours_utc:
            cycle_time = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                cycle_hour,
                tzinfo=UTC,
            )
            cycle_id = cycle_id_for(self.config.source_id, cycle_time)
            probe = self._discover_cycle_availability(cycle_time)
            available = bool(probe["available"])
            status = str(probe["status"])
            reason = probe.get("reason")
            classifier = probe.get("classifier")
            retryable = probe.get("retryable")
            remote_url = str(probe["probe_uri"])

            discovery = CycleDiscovery(
                cycle_id=cycle_id,
                source_id=self.config.source_id,
                cycle_time=cycle_time,
                cycle_hour=cycle_hour,
                available=available,
                status=status,
                reason=str(reason) if reason is not None else None,
                classifier=str(classifier) if classifier is not None else None,
                retryable=bool(retryable) if retryable is not None else None,
                probe_uri=self._safe_text(remote_url),
                evidence=_mapping_value(probe.get("evidence")),
            )
            discoveries.append(discovery)

            if available and self.repository is not None:
                try:
                    self.repository.upsert_forecast_cycle(
                        cycle_id=cycle_id,
                        source_id=self.config.source_id,
                        cycle_time=cycle_time,
                        issue_time=cycle_time,
                        status="discovered",
                    )
                except Exception:
                    LOGGER.exception("Failed to upsert met.forecast_cycle for %s", cycle_id)
                    raise

        return discoveries

    def _discover_cycle_availability(self, cycle_time: datetime) -> dict[str, Any]:
        cloud_probes: list[dict[str, Any]] = []
        for backend in self.config.cloud_backends():
            idx_url = f"{self._mirror_grib_url(backend, cycle_time, forecast_hour=0)}.idx"
            try:
                available = self.availability_checker(idx_url)
            except ForbiddenSourceError:
                LOGGER.warning(
                    "GFS cloud availability check was forbidden for %s",
                    self._safe_text(idx_url),
                    exc_info=True,
                )
                cloud_probes.append({"backend": backend, "uri": self._safe_text(idx_url), "status": "forbidden"})
                continue
            except Exception:
                LOGGER.warning("Failed to check GFS cloud availability for %s", self._safe_text(idx_url), exc_info=True)
                cloud_probes.append({"backend": backend, "uri": self._safe_text(idx_url), "status": "unavailable"})
                continue
            cloud_probes.append({
                "backend": backend,
                "uri": self._safe_text(idx_url),
                "status": "available" if available else "unavailable",
            })
            if available:
                return {
                    "available": True,
                    "status": "discovered",
                    "probe_uri": self._safe_text(idx_url),
                    "evidence": {
                        "source": self.config.source_id,
                        "cycle_hours_utc": list(self.config.cycle_hours_utc),
                        "probe": {
                            "uri": self._safe_text(idx_url),
                            "backend": backend,
                            "forecast_hour": 0,
                            "type": "cloud_idx",
                        },
                        "cloud_probes": cloud_probes,
                    },
                }

        nomads_url = self.remote_url(cycle_time, forecast_hour=0, variable=self.config.variables[0])
        if self.config.uses_nomads():
            try:
                available = self.availability_checker(nomads_url)
                return {
                    "available": bool(available),
                    "status": "discovered" if available else "unavailable",
                    "reason": None if available else "source_cycle_unavailable",
                    "classifier": None if available else "unavailable",
                    "retryable": None if available else True,
                    "probe_uri": self._safe_text(nomads_url),
                    "evidence": {
                        "source": self.config.source_id,
                        "cycle_hours_utc": list(self.config.cycle_hours_utc),
                        "probe": {
                            "uri": self._safe_text(nomads_url),
                            "backend": GFS_NOMADS_BACKEND,
                            "forecast_hour": 0,
                            "variable": self.config.variables[0],
                        },
                        "cloud_probes": cloud_probes,
                    },
                }
            except ForbiddenSourceError as error:
                LOGGER.warning(
                    "GFS NOMADS availability check was forbidden for %s",
                    self._safe_text(nomads_url),
                    exc_info=True,
                )
                self._open_nomads_circuit(self._safe_text(nomads_url), error.error_code)
                return {
                    "available": False,
                    "status": "forbidden",
                    "reason": "source_cycle_forbidden",
                    "classifier": "forbidden",
                    "retryable": False,
                    "probe_uri": self._safe_text(nomads_url),
                    "evidence": {
                        "source": self.config.source_id,
                        "cycle_hours_utc": list(self.config.cycle_hours_utc),
                        "cloud_probes": cloud_probes,
                    },
                }
            except Exception:
                LOGGER.exception("Failed to check GFS NOMADS availability for %s", self._safe_text(nomads_url))

        return {
            "available": False,
            "status": "unavailable",
            "reason": "source_cycle_unavailable",
            "classifier": "unavailable",
            "retryable": True,
            "probe_uri": self._safe_text(nomads_url),
            "evidence": {
                "source": self.config.source_id,
                "cycle_hours_utc": list(self.config.cycle_hours_utc),
                "cloud_probes": cloud_probes,
            },
        }

    def build_manifest(
        self,
        cycle_time: str | datetime,
        forecast_hours: list[int] | None = None,
    ) -> DownloadManifest:
        self.initialize_data_source()
        parsed_cycle_time = parse_cycle_time(cycle_time)
        compact_cycle = format_cycle_time(parsed_cycle_time)
        hours = validate_forecast_hours(
            list(forecast_hours if forecast_hours is not None else self.config.forecast_hours()),
            source_id=self.config.source_id.upper(),
            min_hour=self.config.forecast_start_hour,
            max_hour=self.config.forecast_end_hour,
            step_hours=self.config.forecast_step_hours,
            allowed_hours=set(self.config.forecast_hours()) if self.config.forecast_resolution_segments else None,
        )
        entries: list[ManifestEntry] = []

        effective_hours = self._effective_forecast_hours(hours)
        for forecast_hour in effective_hours:
            bundle_variables = self._variables_for_forecast_hour(forecast_hour)
            bundle_filename = self.raw_bundle_filename(parsed_cycle_time, forecast_hour)
            local_key = f"raw/{self.config.source_id}/{compact_cycle}/{bundle_filename}"
            for variable in bundle_variables:
                entries.append(
                    ManifestEntry(
                        remote_url=self.remote_bundle_url(parsed_cycle_time, forecast_hour, bundle_variables),
                        local_key=local_key,
                        variable=variable,
                        forecast_hour=forecast_hour,
                        metadata={
                            "cycle_time": parsed_cycle_time.isoformat(),
                            "valid_time": valid_time_for(parsed_cycle_time, forecast_hour).isoformat(),
                            "bundle": {
                                "layout": "per_forecast_hour",
                                "variables": list(bundle_variables),
                                "physical_file_count": len(effective_hours),
                            },
                            "grib_short_name": GFS_GRIB_SHORT_NAME.get(variable, variable),
                            "cfgrib_filter_by_keys": {"shortName": GFS_GRIB_SHORT_NAME.get(variable, variable)},
                            "logical_remote_url": self.remote_url(parsed_cycle_time, forecast_hour, variable),
                        },
                    )
                )

        metadata = {
            "cycle_time": parsed_cycle_time.isoformat(),
            "first_forecast_hour": min(effective_hours) if effective_hours else None,
            "last_forecast_hour": max(effective_hours) if effective_hours else None,
            "requested_forecast_hours": list(hours),
            "forecast_hours": list(effective_hours),
            "source_policy": self.source_policy_identity(effective_hours),
            "source_object_identity": self.source_object_identity(parsed_cycle_time, effective_hours),
            "variable_count": len(self.config.variables),
            "physical_file_layout": "per_forecast_hour_bundle",
            "physical_file_count": len(effective_hours),
            "total_file_count": len(entries),
        }
        manifest_key = f"raw/{self.config.source_id}/{compact_cycle}/manifest.json"
        manifest = DownloadManifest(
            source_id=self.config.source_id,
            cycle_time=parsed_cycle_time,
            entries=tuple(entries),
            manifest_uri=self.object_store.uri_for_key(manifest_key),
            metadata=metadata,
        )

        try:
            self.object_store.write_bytes_atomic(
                manifest_key,
                json.dumps(manifest.as_dict(), indent=2, sort_keys=True).encode("utf-8"),
            )
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to persist manifest for %s", compact_cycle)
            raise

        if self.repository is not None:
            try:
                self.repository.upsert_forecast_cycle(
                    cycle_id=cycle_id_for(self.config.source_id, parsed_cycle_time),
                    source_id=self.config.source_id,
                    cycle_time=parsed_cycle_time,
                    issue_time=parsed_cycle_time,
                    status="discovered",
                    manifest_uri=manifest.manifest_uri,
                )
            except Exception:
                LOGGER.exception("Failed to update manifest_uri for GFS cycle %s", compact_cycle)
                raise

        return manifest

    def load_manifest(self, manifest_uri: str) -> DownloadManifest:
        try:
            payload = json.loads(self.object_store.read_bytes(manifest_uri).decode("utf-8"))
        except (json.JSONDecodeError, OSError, ObjectStoreError, ValueError) as error:
            raise GFSAdapterError(f"Failed to load manifest {manifest_uri}: {error}") from error
        return DownloadManifest.from_dict(payload)

    def download_plan(self, manifest: DownloadManifest) -> DownloadPlanResult:
        cycle_time = manifest.cycle_time
        existing_cycle = self._get_cycle(cycle_time)
        trusted_source_object_identity = self._trusted_prior_source_object_identity(manifest, existing_cycle)
        already_done_by_key: dict[str, DownloadFileResult] = {}
        needs_download = False

        for entry in manifest.entries:
            try:
                if entry.local_key in already_done_by_key:
                    continue
                already_done = self._already_done_result(
                    entry,
                    trusted_source_object_identity=trusted_source_object_identity,
                )
            except FileTooLargeError as error:
                self._record_download_failure(cycle_time, error.error_code, str(error), retry_count=0)
                return DownloadPlanResult(
                    status="failed_download",
                    files=(
                        DownloadFileResult(
                            local_key=entry.local_key,
                            status="failed",
                            error_code=error.error_code,
                            error_message=str(error),
                        ),
                    ),
                    total_bytes_written=0,
                    retry_count=0,
                )
            if already_done is None:
                needs_download = True
            else:
                already_done_by_key[entry.local_key] = already_done

        if not needs_download:
            results = tuple(already_done_by_key[entry.local_key] for entry in manifest.entries)
            if existing_cycle is not None and existing_cycle.get("status") == "raw_complete":
                self._refresh_manifest_source_object_identity(manifest)
                return DownloadPlanResult(
                    status="already_done",
                    files=results,
                    total_bytes_written=0,
                    retry_count=0,
                )

            verification = self.verify_manifest(manifest)
            if not verification.passed:
                message = "; ".join(failure.error_message for failure in verification.failures)
                self._record_download_failure(cycle_time, "VERIFY_FAILED", message, retry_count=0)
                return DownloadPlanResult(
                    status="failed_download",
                    files=results,
                    total_bytes_written=0,
                    retry_count=0,
                )
            self._refresh_manifest_source_object_identity(manifest)
            return DownloadPlanResult(
                status="raw_complete",
                files=results,
                total_bytes_written=0,
                retry_count=0,
            )

        self._update_cycle(cycle_time=cycle_time, status="downloading", error_code="", error_message="")

        results: list[DownloadFileResult] = []
        completed_by_key: dict[str, DownloadFileResult] = dict(already_done_by_key)
        retry_count = 0
        total_bytes_written = 0

        for entry in manifest.entries:
            try:
                if entry.local_key in completed_by_key:
                    results.append(completed_by_key[entry.local_key])
                    continue

                result, retries = self._download_entry(entry)
                retry_count += retries
                total_bytes_written += result.bytes_written
                completed_by_key[entry.local_key] = result
                results.append(result)
                self._persist_manifest_metadata(manifest)
            except GFSAdapterError as error:
                retry_count += error.attempts
                failure = DownloadFileResult(
                    local_key=entry.local_key,
                    status="failed",
                    error_code=error.error_code,
                    error_message=str(error),
                )
                results.append(failure)
                self._record_download_failure(cycle_time, error.error_code, str(error), retry_count)
                return DownloadPlanResult(
                    status="failed_download",
                    files=tuple(results),
                    total_bytes_written=total_bytes_written,
                    retry_count=retry_count,
                )
            except Exception as error:
                LOGGER.exception("Unexpected download failure for %s", entry.local_key)
                self._record_download_failure(cycle_time, "UNEXPECTED_DOWNLOAD_ERROR", str(error), retry_count)
                results.append(
                    DownloadFileResult(
                        local_key=entry.local_key,
                        status="failed",
                        error_code="UNEXPECTED_DOWNLOAD_ERROR",
                        error_message=str(error),
                    )
                )
                return DownloadPlanResult(
                    status="failed_download",
                    files=tuple(results),
                    total_bytes_written=total_bytes_written,
                    retry_count=retry_count,
                )

        verification = self.verify_manifest(manifest)
        if not verification.passed:
            message = "; ".join(failure.error_message for failure in verification.failures)
            self._record_download_failure(cycle_time, "VERIFY_FAILED", message, retry_count)
            return DownloadPlanResult(
                status="failed_download",
                files=tuple(results),
                total_bytes_written=total_bytes_written,
                retry_count=retry_count,
            )

        self._update_cycle(cycle_time=cycle_time, status="raw_complete", retry_count=0)
        self._refresh_manifest_source_object_identity(manifest)
        return DownloadPlanResult(
            status="raw_complete",
            files=tuple(results),
            total_bytes_written=total_bytes_written,
            retry_count=retry_count,
        )

    def verify_manifest(self, manifest: DownloadManifest) -> VerificationResult:
        failures: list[VerificationFailure] = []
        verified_keys: set[str] = set()
        for entry in manifest.entries:
            if entry.local_key in verified_keys:
                continue
            verified_keys.add(entry.local_key)
            try:
                if not self.object_store.exists(entry.local_key):
                    failures.append(
                        VerificationFailure(
                            local_key=entry.local_key,
                            error_code="MISSING_FILE",
                            error_message=f"Missing raw file: {entry.local_key}",
                        )
                    )
                    continue

                size = self.object_store.size(entry.local_key)
                minimum_size = entry.expected_size_bytes or self.config.min_file_size_bytes
                if size < minimum_size:
                    failures.append(
                        VerificationFailure(
                            local_key=entry.local_key,
                            error_code="SIZE_TOO_SMALL",
                            error_message=f"{entry.local_key} is {size} bytes; expected at least {minimum_size}",
                        )
                    )
                    continue

                actual_checksum = self.object_store.checksum(entry.local_key)
                if entry.expected_checksum and actual_checksum != entry.expected_checksum:
                    failures.append(
                        VerificationFailure(
                            local_key=entry.local_key,
                            error_code="CHECKSUM_MISMATCH",
                            error_message=(
                                f"{entry.local_key} checksum mismatch: expected {entry.expected_checksum}, "
                                f"actual {actual_checksum}"
                            ),
                        )
                    )
            except (OSError, ObjectStoreError, ValueError) as error:
                failures.append(
                    VerificationFailure(
                        local_key=entry.local_key,
                        error_code="VERIFY_IO_ERROR",
                        error_message=f"Failed to verify {entry.local_key}: {error}",
                    )
                )

        if failures:
            first_failure = failures[0]
            self._record_download_failure(
                manifest.cycle_time,
                first_failure.error_code,
                first_failure.error_message,
                retry_count=0,
            )
            return VerificationResult(status="partial_fail", failures=tuple(failures))

        self._update_cycle(cycle_time=manifest.cycle_time, status="raw_complete")
        return VerificationResult(status="passed")

    def _download_entry(self, entry: ManifestEntry) -> tuple[DownloadFileResult, int]:
        deadline = self.clock() + self.config.max_wait_seconds
        total_retries = 0

        while True:
            try:
                payload, retries, all_sources_404 = self._fetch_entry_payload(entry)
                total_retries += retries
                checksum = payload.checksum
                if entry.expected_checksum and checksum != entry.expected_checksum:
                    raise ChecksumMismatchError(
                        (
                            f"Downloaded checksum mismatch for {entry.local_key}: "
                            f"expected {entry.expected_checksum}, actual {checksum}"
                        ),
                        attempts=total_retries,
                    )

                self.object_store.write_bytes_atomic(entry.local_key, payload.content)
                return (
                    DownloadFileResult(
                        local_key=entry.local_key,
                        status="downloaded",
                        checksum=checksum,
                        bytes_written=payload.bytes_written,
                    ),
                    total_retries,
                )
            except FileUnavailableError as error:
                # 404 across all sources: the cycle file is not published yet; poll
                # within the wait budget (preserves the original NOMADS polling loop).
                total_retries += max(0, error.attempts - 1)
                if self.clock() >= deadline:
                    raise PollingTimeoutError(
                        f"Timed out waiting for {self._safe_text(entry.remote_url)}",
                        attempts=total_retries,
                    ) from error
                self.sleeper(self.config.poll_interval_seconds)
            except (
                ForbiddenSourceError,
                NetworkDownloadError,
                ChecksumMismatchError,
                FileTooLargeError,
                CdoMissingError,
                CdoClipError,
                AmbiguousIdxRecordError,
                IdxSelectorPolicyError,
                IdxParseError,
                AllSourcesUnavailableError,
            ):
                self._delete_partial(entry.local_key)
                raise
            except (OSError, ObjectStoreError, ValueError) as error:
                self._delete_partial(entry.local_key)
                raise GFSAdapterError(f"Failed to store {entry.local_key}: {error}", attempts=total_retries) from error

    def _fetch_entry_payload(self, entry: ManifestEntry) -> tuple[DownloadedPayload, int, bool]:
        """Try each backend in order; return the first successful payload.

        Returns ``(payload, retries, all_sources_404)``. ``FileUnavailableError`` is
        raised only when every reachable backend reported 404 (not-yet-published), so
        the caller can poll. If all sources are unreachable for non-404 reasons (and
        NOMADS is circuit-broken), ``AllSourcesUnavailableError`` is raised loud.
        """
        total_retries = 0
        any_404 = False
        any_reachable_failure = False
        any_skipped = False
        nomads_tripped = False
        last_error: GFSAdapterError | None = None
        tried: list[str] = []

        for backend in self.config.source_backends:
            if backend == GFS_NOMADS_BACKEND:
                if self._nomads_circuit_open():
                    LOGGER.warning("Skipping NOMADS for %s: 403 circuit breaker open", entry.local_key)
                    any_skipped = True
                    continue
            elif self._mirror_cooldown_active(backend):
                LOGGER.warning("Skipping GFS mirror %s for %s: cooldown active", backend, entry.local_key)
                any_skipped = True
                continue
            tried.append(backend)
            try:
                payload, retries = self._download_backend(entry, backend)
                return payload, total_retries + retries, False
            except IdxRecordNotFoundError:
                any_404 = True
                continue
            except IdxSelectorPolicyError:
                raise
            except FileUnavailableError as error:
                any_404 = True
                total_retries += max(0, error.attempts - 1)
                continue
            except ForbiddenSourceError as error:
                # 403 is a NOMADS-only condition here: trip the persisted breaker and
                # advance without any retry/backoff/availability loop.
                if backend == GFS_NOMADS_BACKEND:
                    self._open_nomads_circuit(self._safe_text(entry.remote_url), error.error_code)
                    any_reachable_failure = True
                    nomads_tripped = True
                    last_error = error
                    continue
                raise
            except RateLimitedError as error:
                self._open_mirror_cooldown(backend, error.error_code)
                any_reachable_failure = True
                last_error = error
                continue
            except NetworkDownloadError as error:
                any_reachable_failure = True
                total_retries += error.attempts
                last_error = error
                continue

        # Fail loud when no source produced the file and either a backend was skipped
        # (cooldown / pre-open 403 breaker) or NOMADS just tripped its breaker this pass
        # — i.e. cloud mirrors unreachable AND NOMADS unavailable. This precedes the
        # pollable-404 branch: a 404-only chain is only "not yet published" when every
        # backend was actually reachable (no skip/circuit gap masking a dead source).
        if (any_skipped or nomads_tripped) and (last_error is not None or any_404):
            raise AllSourcesUnavailableError(
                (
                    f"All GFS sources unavailable for {entry.local_key} "
                    f"(tried {', '.join(tried) or 'none'}; nomads_circuit_open="
                    f"{self._nomads_circuit_open()}): "
                    f"{self._safe_text(str(last_error)) if last_error else 'mirror 404'}"
                ),
                attempts=total_retries,
            ) from last_error
        if any_404 and not any_reachable_failure:
            raise FileUnavailableError(
                f"GFS file not yet published across sources {', '.join(tried)} for {entry.local_key}",
                attempts=total_retries,
            )
        if last_error is not None:
            # A reachable backend failed (network/rate-limit) with no skip-induced gap;
            # preserve the original typed error so single-backend chains keep their code.
            raise last_error
        raise AllSourcesUnavailableError(
            f"No GFS sources eligible for {entry.local_key} (all backends skipped or circuit-broken)",
            attempts=total_retries,
        )

    def _download_backend(self, entry: ManifestEntry, backend: str) -> tuple[DownloadedPayload, int]:
        if backend == GFS_NOMADS_BACKEND:
            self._throttle_nomads()
            return self._download_with_retries(entry.remote_url)
        return self._download_cloud_mirror(entry, backend)

    # ------------------------------------------------------------------ cloud mirror
    def _download_cloud_mirror(self, entry: ManifestEntry, backend: str) -> tuple[DownloadedPayload, int]:
        """Subset one forecast-hour bundle from a cloud mirror via .idx Range GET + local cdo clip.

        The mirror serves the full global GRIB and a wgrib2 .idx. We GET the .idx,
        locate the requested variables' byte ranges, Range-GET only those bytes,
        concatenate them into a small GRIB bundle, then clip to the configured bbox
        with cdo (mandatory: a missing/failed cdo fails loud).
        """
        cycle_time = self._entry_cycle_time(entry)
        variables = self._bundle_variables_for_entry(entry)
        grib_url = self._mirror_grib_url(backend, cycle_time, entry.forecast_hour)
        idx_url = f"{grib_url}.idx"
        idx_text = self._http_get_text(idx_url)
        records = _parse_gfs_idx(idx_text)
        raw_chunks: list[bytes] = []
        selectors: dict[str, Any] = {}
        for variable in variables:
            selection = _select_idx_record(
                records,
                variable,
                entry.forecast_hour,
                expected_interval_hours=self._expected_interval_hours(entry.forecast_hour),
            )
            selectors[variable] = selection.as_metadata()
            raw_chunks.append(self._http_get_range(grib_url, selection.byte_range[0], selection.byte_range[1]))
        entry.metadata.setdefault("idx_selectors", {}).update(selectors)
        if len(variables) == 1:
            entry.metadata["idx_selector"] = selectors[variables[0]]
        raw_bytes = b"".join(raw_chunks)
        clipped = self._clip_grib_to_bbox(raw_bytes, grib_url)
        if len(clipped) > self.config.max_file_size_bytes:
            raise FileTooLargeError(
                f"Clipped GFS payload from {backend} exceeds maximum size {self.config.max_file_size_bytes} bytes"
            )
        checksum = hashlib.sha256(clipped).hexdigest()
        return DownloadedPayload(content=clipped, checksum=checksum, bytes_written=len(clipped)), 0

    def _bundle_variables_for_entry(self, entry: ManifestEntry) -> tuple[str, ...]:
        bundle = entry.metadata.get("bundle") if entry.metadata else None
        raw_variables = bundle.get("variables") if isinstance(bundle, Mapping) else None
        if isinstance(raw_variables, list):
            variables = tuple(str(variable) for variable in raw_variables)
        else:
            variables = (entry.variable,)
        if entry.forecast_hour == 0:
            variables = tuple(variable for variable in variables if variable not in GFS_F000_UNAVAILABLE_VARIABLES)
        if not variables:
            raise IdxRecordNotFoundError(
                f"No GFS variables are defined for f{entry.forecast_hour:03d} in {entry.local_key}",
                attempts=1,
            )
        return variables

    def _mirror_grib_url(self, backend: str, cycle_time: datetime, forecast_hour: int) -> str:
        base = self.config.mirror_base_url(backend).rstrip("/")
        path = (
            f"/gfs.{cycle_time:%Y%m%d}/{cycle_time:%H}/atmos/"
            f"gfs.t{cycle_time:%H}z.pgrb2.0p25.f{forecast_hour:03d}"
        )
        return f"{base}{path}"

    def _entry_cycle_time(self, entry: ManifestEntry) -> datetime:
        cycle_time = entry.metadata.get("cycle_time") if entry.metadata else None
        if isinstance(cycle_time, str | datetime):
            return parse_cycle_time(cycle_time)
        # Derive from the local_key path segment ``raw/gfs/<YYYYMMDDHH>/...``.
        parts = [part for part in entry.local_key.split("/") if part]
        for part in parts:
            if len(part) == 10 and part.isdigit():
                return parse_cycle_time(part)
        raise GFSAdapterError(f"Cannot determine cycle_time for entry {entry.local_key}")

    def _expected_interval_hours(self, forecast_hour: int) -> int | None:
        if forecast_hour <= 0:
            return None
        prior_hours = [hour for hour in self.config.forecast_hours() if hour < forecast_hour]
        if not prior_hours:
            return forecast_hour
        return forecast_hour - max(prior_hours)

    def _http_get_text(self, url: str) -> str:
        payload = self.downloader(url) if self._has_injected_downloader else self._http_get_bytes(url)
        if isinstance(payload, DownloadedPayload):
            return payload.content.decode("utf-8", errors="replace")
        return bytes(payload).decode("utf-8", errors="replace")

    def _http_get_range(self, url: str, start: int, end: int | None) -> bytes:
        # end is exclusive ([start, end)); HTTP Range is inclusive, hence end-1.
        range_header = f"bytes={start}-{end - 1}" if end is not None else f"bytes={start}-"
        if self._has_injected_downloader:
            payload = self.downloader(f"{url}#range={range_header}")
            content = payload.content if isinstance(payload, DownloadedPayload) else bytes(payload)
            if end is not None and len(content) > end - start:
                raise FileTooLargeError(
                    f"Injected range payload for {self._safe_text(url)} exceeded requested byte range"
                )
            return content
        return self._http_get_bytes(url, range_header=range_header, expected_range=(start, end))

    def _http_get_bytes(
        self,
        url: str,
        *,
        range_header: str | None = None,
        expected_range: tuple[int, int | None] | None = None,
    ) -> bytes:
        headers = {"Range": range_header} if range_header else {}
        request = Request(url, method="GET", headers=headers)
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if range_header is not None:
                    self._validate_range_response(status, response, url, expected_range)
                else:
                    self._raise_for_http_status(status, url, response=response)
                content = self._read_bounded(response, url)
                if expected_range is not None and expected_range[1] is not None:
                    expected_length = expected_range[1] - expected_range[0]
                    if len(content) > expected_length:
                        raise FileTooLargeError(
                            f"Range response for {self._safe_text(url)} exceeded requested byte range"
                        )
                return content
        except HTTPError as error:
            self._raise_for_http_error(error, url)
        except (URLError, TimeoutError, OSError) as error:
            raise NetworkDownloadError(
                f"Network error while downloading {self._safe_text(url)}: {self._safe_text(str(error))}"
            ) from error
        raise NetworkDownloadError(f"Unreachable: {self._safe_text(url)}")

    def _validate_range_response(
        self,
        status: int,
        response: Any,
        url: str,
        expected_range: tuple[int, int | None] | None,
    ) -> None:
        if status != 206:
            if status in (404, 403, 429, 503) or status >= 400:
                self._raise_for_http_status(status, url, response=response)
            raise NetworkDownloadError(
                f"HTTP {status} for ranged GFS request {self._safe_text(url)}; expected 206 Partial Content",
                attempts=1,
            )
        content_range = response.headers.get("Content-Range") if response is not None else None
        if not content_range:
            raise NetworkDownloadError(
                f"Missing Content-Range for ranged GFS request {self._safe_text(url)}",
                attempts=1,
            )
        if expected_range is None:
            return
        expected_start, expected_end = expected_range
        parsed = _parse_content_range(content_range)
        if parsed is None:
            raise NetworkDownloadError(
                f"Malformed Content-Range {content_range!r} for ranged GFS request {self._safe_text(url)}",
                attempts=1,
            )
        actual_start, actual_end_inclusive = parsed
        if actual_start != expected_start:
            raise NetworkDownloadError(
                f"Content-Range start mismatch for {self._safe_text(url)}: expected {expected_start}, "
                f"observed {actual_start}",
                attempts=1,
            )
        if expected_end is not None and actual_end_inclusive != expected_end - 1:
            raise NetworkDownloadError(
                f"Content-Range end mismatch for {self._safe_text(url)}: expected {expected_end - 1}, "
                f"observed {actual_end_inclusive}",
                attempts=1,
            )

    def _raise_for_http_status(self, status: int, url: str, *, response: Any = None) -> None:
        if status in (200, 206):
            return
        if status == 404:
            raise IdxRecordNotFoundError(f"Remote file is unavailable: {self._safe_text(url)}", attempts=1)
        if status == 403:
            raise ForbiddenSourceError(f"Remote file is forbidden: {self._safe_text(url)}", attempts=1)
        if status in (429, 503):
            retry_after = None
            if response is not None:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            raise RateLimitedError(
                f"Mirror rate limited (HTTP {status}) for {self._safe_text(url)}",
                retry_after_seconds=retry_after,
                attempts=1,
            )
        if status >= 400:
            raise NetworkDownloadError(f"HTTP {status} while downloading {self._safe_text(url)}", attempts=1)

    def _raise_for_http_error(self, error: HTTPError, url: str) -> None:
        if error.code == 404:
            raise IdxRecordNotFoundError(
                f"Remote file is unavailable: {self._safe_text(url)}", attempts=1
            ) from error
        if error.code == 403:
            raise ForbiddenSourceError(
                f"Remote file is forbidden: {self._safe_text(url)}", attempts=1
            ) from error
        if error.code in (429, 503):
            raise RateLimitedError(
                f"Mirror rate limited (HTTP {error.code}) for {self._safe_text(url)}",
                retry_after_seconds=_retry_after_seconds(error.headers.get("Retry-After")),
                attempts=1,
            ) from error
        raise NetworkDownloadError(
            f"HTTP {error.code} while downloading {self._safe_text(url)}: {self._safe_text(str(error))}"
        ) from error

    def _read_bounded(self, response: Any, url: str) -> bytes:
        content = bytearray()
        chunk_size = max(1, self.config.download_chunk_size_bytes)
        total = 0
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > self.config.max_file_size_bytes:
                raise FileTooLargeError(
                    f"Remote file {self._safe_text(url)} exceeded maximum size {self.config.max_file_size_bytes} bytes"
                )
            content.extend(chunk)
        return bytes(content)

    def _clip_grib_to_bbox(self, global_bytes: bytes, source_url: str) -> bytes:
        """Clip a global GRIB2 payload to the configured bbox via cdo, keeping GRIB2.

        Cloud mirrors serve the global field (no server-side region subset), so the
        bytes are clipped locally to stay identity-compatible with the NOMADS
        server-side-subset path. cdo is mandatory: a missing binary or non-zero exit
        fails loud rather than degrading to the global payload.
        """
        cdo_binary = shutil.which("cdo")
        if cdo_binary is None:
            raise CdoMissingError(
                f"cdo is required to clip GFS mirror downloads to bbox but was not found "
                f"while processing {self._safe_text(source_url)}"
            )
        bbox = self.config.bbox
        sellonlatbox = f"sellonlatbox,{bbox.west:g},{bbox.east:g},{bbox.south:g},{bbox.north:g}"
        with (
            tempfile.NamedTemporaryFile(suffix=".grib2") as raw,
            tempfile.NamedTemporaryFile(suffix=".grib2") as clipped,
        ):
            raw.write(global_bytes)
            raw.flush()
            try:
                result = subprocess.run(  # noqa: S603 - argv list, no shell
                    [cdo_binary, "-f", "grb2", sellonlatbox, raw.name, clipped.name],
                    capture_output=True,
                    timeout=CDO_CLIP_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as error:
                stderr = error.stderr.decode("utf-8", errors="replace") if error.stderr else ""
                raise CdoClipError(
                    f"cdo timed out after {CDO_CLIP_TIMEOUT_SECONDS}s clipping GFS file "
                    f"{self._safe_text(source_url)}: {self._safe_text(stderr)}"
                ) from error
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
                raise CdoClipError(
                    f"cdo failed to clip GFS file {self._safe_text(source_url)} "
                    f"(exit {result.returncode}): {self._safe_text(stderr)}"
                )
            clipped_size = Path(clipped.name).stat().st_size
            if clipped_size > self.config.max_file_size_bytes:
                raise FileTooLargeError(
                    f"Clipped GFS payload from {self._safe_text(source_url)} exceeds maximum size "
                    f"{self.config.max_file_size_bytes} bytes"
                )
            return Path(clipped.name).read_bytes()

    # ------------------------------------------------------------------ circuit breaker
    def _circuit_state_path(self, source: str) -> Path:
        root = Path(str(self.config.object_store_root)).expanduser()
        return root / "state" / "source_circuit" / f"gfs_{source}.json"

    def _read_circuit_state(self, source: str) -> dict[str, Any]:
        path = self._circuit_state_path(source)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def _write_circuit_state(self, source: str, state: Mapping[str, Any]) -> None:
        path = self._circuit_state_path(source)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.part")
            tmp.write_text(json.dumps(dict(state), sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            LOGGER.warning("Failed to persist GFS circuit state for %s", source, exc_info=True)

    def _nomads_circuit_open(self) -> bool:
        cooldown_until = self._read_circuit_state(GFS_NOMADS_BACKEND).get("cooldown_until")
        return self._cooldown_active(cooldown_until)

    def _open_nomads_circuit(self, last_url: str, last_status: str) -> None:
        now = self.wall_clock()
        cooldown_until = now + _minutes(self.config.nomads_cooldown_minutes)
        self._write_circuit_state(
            GFS_NOMADS_BACKEND,
            {
                "opened_at": now.isoformat(),
                "cooldown_until": cooldown_until.isoformat(),
                "last_status": last_status,
                "last_url": last_url,
            },
        )
        LOGGER.warning("NOMADS 403 circuit breaker opened until %s", cooldown_until.isoformat())

    def _mirror_cooldown_active(self, backend: str) -> bool:
        cooldown_until = self._read_circuit_state(backend).get("cooldown_until")
        return self._cooldown_active(cooldown_until)

    def _open_mirror_cooldown(self, backend: str, last_status: str) -> None:
        now = self.wall_clock()
        cooldown_until = now + _minutes(self.config.mirror_cooldown_minutes)
        self._write_circuit_state(
            backend,
            {
                "opened_at": now.isoformat(),
                "cooldown_until": cooldown_until.isoformat(),
                "last_status": last_status,
            },
        )
        LOGGER.warning("GFS mirror %s cooldown opened until %s", backend, cooldown_until.isoformat())

    def _cooldown_active(self, cooldown_until: Any) -> bool:
        if not isinstance(cooldown_until, str):
            return False
        try:
            until = datetime.fromisoformat(cooldown_until)
        except ValueError:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return self.wall_clock() < until

    def _throttle_nomads(self) -> None:
        if self.config.nomads_min_interval_seconds <= 0:
            return
        state = self._read_circuit_state(GFS_NOMADS_BACKEND)
        last_request = state.get("last_request_at")
        now = self.wall_clock()
        if isinstance(last_request, str):
            try:
                previous = datetime.fromisoformat(last_request)
                if previous.tzinfo is None:
                    previous = previous.replace(tzinfo=UTC)
                wait = self.config.nomads_min_interval_seconds - (now - previous).total_seconds()
                if wait > 0:
                    self.sleeper(wait)
            except ValueError:
                pass
        merged = dict(state)
        merged["last_request_at"] = self.wall_clock().isoformat()
        self._write_circuit_state(GFS_NOMADS_BACKEND, merged)

    def _download_with_retries(self, remote_url: str) -> tuple[DownloadedPayload, int]:
        last_error: Exception | None = None
        max_attempts = max(1, self.config.max_retries)
        for attempt in range(1, max_attempts + 1):
            try:
                return self._normalize_payload(self.downloader(remote_url)), attempt - 1
            except (FileUnavailableError, ForbiddenSourceError, FileTooLargeError):
                raise
            except Exception as error:
                last_error = error
                if attempt >= max_attempts:
                    break
                backoff = self._backoff_for(attempt)
                self.sleeper(backoff)

        raise NetworkDownloadError(
            f"Failed to download {self._safe_text(remote_url)} after {max_attempts} attempts: "
            f"{self._safe_text(str(last_error))}",
            attempts=max_attempts - 1,
        )

    def _normalize_payload(self, payload: bytes | DownloadedPayload) -> DownloadedPayload:
        if isinstance(payload, DownloadedPayload):
            if (
                payload.bytes_written > self.config.max_file_size_bytes
                or len(payload.content) > self.config.max_file_size_bytes
            ):
                raise FileTooLargeError(
                    f"Downloaded GFS payload exceeds maximum size {self.config.max_file_size_bytes} bytes"
                )
            return payload
        content = bytes(payload)
        if len(content) > self.config.max_file_size_bytes:
            raise FileTooLargeError(
                f"Downloaded GFS payload exceeds maximum size {self.config.max_file_size_bytes} bytes"
            )
        return DownloadedPayload(content=content, checksum=sha256_bytes(content), bytes_written=len(content))

    def _entry_already_done(
        self,
        entry: ManifestEntry,
        *,
        trusted_source_object_identity: Mapping[str, Any] | None,
    ) -> bool:
        try:
            if not self.object_store.exists(entry.local_key):
                return False
            minimum_size = entry.expected_size_bytes or self.config.min_file_size_bytes
            size_bytes = self.object_store.size(entry.local_key)
            if size_bytes < minimum_size:
                return False
            if size_bytes > self.config.max_file_size_bytes:
                raise FileTooLargeError(
                    f"Existing GFS raw object {entry.local_key} exceeds maximum size "
                    f"{self.config.max_file_size_bytes} bytes"
                )
            if entry.expected_checksum is None:
                return self._trusted_raw_observation_matches(
                    entry.local_key,
                    trusted_source_object_identity=trusted_source_object_identity,
                    size_bytes=size_bytes,
                )
            return self.object_store.checksum(entry.local_key) == entry.expected_checksum
        except FileTooLargeError:
            raise
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to check idempotency for %s", entry.local_key)
            return False

    def _already_done_result(
        self,
        entry: ManifestEntry,
        *,
        trusted_source_object_identity: Mapping[str, Any] | None,
    ) -> DownloadFileResult | None:
        if not self._entry_already_done(entry, trusted_source_object_identity=trusted_source_object_identity):
            return None
        try:
            checksum = self.object_store.checksum(entry.local_key)
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to checksum existing raw object %s", entry.local_key)
            return None
        return DownloadFileResult(local_key=entry.local_key, status="already_done", checksum=checksum)

    def _download_url(self, remote_url: str) -> DownloadedPayload:
        request = Request(remote_url, method="GET")
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if status == 404:
                    raise FileUnavailableError(f"Remote file is unavailable: {self._safe_text(remote_url)}", attempts=1)
                if status == 403:
                    raise ForbiddenSourceError(f"Remote file is forbidden: {self._safe_text(remote_url)}", attempts=1)
                if status >= 400:
                    raise NetworkDownloadError(
                        f"HTTP {status} while downloading {self._safe_text(remote_url)}",
                        attempts=1,
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > self.config.max_file_size_bytes:
                    raise FileTooLargeError(
                        (
                            f"Remote file {self._safe_text(remote_url)} is {content_length} bytes; "
                            f"maximum allowed is {self.config.max_file_size_bytes}"
                        )
                    )

                content = bytearray()
                checksum = hashlib.sha256()
                chunk_size = max(1, self.config.download_chunk_size_bytes)
                total_bytes = 0
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > self.config.max_file_size_bytes:
                        raise FileTooLargeError(
                            (
                                f"Remote file {self._safe_text(remote_url)} exceeded maximum size "
                                f"{self.config.max_file_size_bytes} bytes"
                            )
                        )
                    checksum.update(chunk)
                    content.extend(chunk)

                return DownloadedPayload(
                    content=bytes(content),
                    checksum=checksum.hexdigest(),
                    bytes_written=total_bytes,
                )
        except HTTPError as error:
            if error.code == 404:
                raise FileUnavailableError(
                    f"Remote file is unavailable: {self._safe_text(remote_url)}",
                    attempts=1,
                ) from error
            if error.code == 403:
                raise ForbiddenSourceError(
                    f"Remote file is forbidden: {self._safe_text(remote_url)}",
                    attempts=1,
                ) from error
            raise NetworkDownloadError(
                f"HTTP {error.code} while downloading {self._safe_text(remote_url)}: {self._safe_text(str(error))}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise NetworkDownloadError(
                f"Network error while downloading {self._safe_text(remote_url)}: {self._safe_text(str(error))}"
            ) from error

    def _url_exists(self, remote_url: str) -> bool:
        request = Request(remote_url, method="HEAD")
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                status = getattr(response, "status", 200)
                return 200 <= status < 400
        except HTTPError as error:
            if error.code == 404:
                return False
            if error.code == 403:
                raise ForbiddenSourceError(
                    f"Remote file is forbidden: {self._safe_text(remote_url)}",
                    attempts=1,
                ) from error
            LOGGER.warning("HEAD %s returned HTTP %s", self._safe_text(remote_url), error.code)
            return False
        except (URLError, TimeoutError, OSError):
            LOGGER.warning("HEAD %s failed", self._safe_text(remote_url), exc_info=True)
            return False

    def _record_download_failure(
        self,
        cycle_time: datetime,
        error_code: str,
        error_message: str,
        retry_count: int,
    ) -> None:
        self._update_cycle(
            cycle_time=cycle_time,
            status="failed_download",
            retry_count=retry_count,
            error_code=error_code,
            error_message=self._safe_text(error_message),
        )

    def _refresh_manifest_source_object_identity(self, manifest: DownloadManifest) -> None:
        hours = self._manifest_forecast_hours(manifest)
        manifest.metadata["source_object_identity"] = self.source_object_identity(manifest.cycle_time, hours)
        self._persist_manifest_metadata(manifest)

    def _persist_manifest_metadata(self, manifest: DownloadManifest) -> None:
        if manifest.manifest_uri is None:
            return
        try:
            self.object_store.write_bytes_atomic(
                manifest.manifest_uri,
                json.dumps(manifest.as_dict(), indent=2, sort_keys=True).encode("utf-8"),
            )
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to persist refreshed source object identity for %s", manifest.manifest_uri)
            raise

    def _manifest_forecast_hours(self, manifest: DownloadManifest) -> list[int]:
        forecast_hours = manifest.metadata.get("forecast_hours")
        if isinstance(forecast_hours, list):
            return [int(forecast_hour) for forecast_hour in forecast_hours]
        return sorted({int(entry.forecast_hour) for entry in manifest.entries})

    def _variables_for_forecast_hour(self, forecast_hour: int) -> tuple[str, ...]:
        return tuple(
            variable
            for variable in self.config.variables
            if not (forecast_hour == 0 and variable in GFS_F000_UNAVAILABLE_VARIABLES)
        )

    def _effective_forecast_hours(self, forecast_hours: list[int]) -> list[int]:
        return list(forecast_hours)

    def source_policy_identity(self, forecast_hours: list[int] | None = None) -> dict[str, Any]:
        requested_hours = list(forecast_hours if forecast_hours is not None else self.config.forecast_hours())
        hours = self._effective_forecast_hours(requested_hours)
        return {
            "source": self.config.source_id,
            "policy_schema_version": "nhms.gfs.source_policy.v3",
            "idx_selector_schema_version": GFS_IDX_SELECTOR_SCHEMA_VERSION,
            "apcp_selector_policy": GFS_APCP_SELECTOR_POLICY,
            "cycle_hours_utc": list(self.config.cycle_hours_utc),
            "forecast_start_hour": self.config.forecast_start_hour,
            "forecast_end_hour": self.config.forecast_end_hour,
            "forecast_step_hours": self.config.forecast_step_hours,
            "forecast_resolution_segments": (
                [list(segment) for segment in self.config.forecast_resolution_segments]
                if self.config.forecast_resolution_segments
                else None
            ),
            "requested_forecast_hours": requested_hours,
            "forecast_hours": hours,
            "variables": list(self.config.variables),
            "grib_short_names": self._grib_short_names_identity(),
            "variable_availability": {
                "f000_unavailable_variables": sorted(GFS_F000_UNAVAILABLE_VARIABLES),
                "f000_keeps_available_instantaneous_variables": True,
            },
            "max_retries": self.config.max_retries,
            "max_wait_seconds": self.config.max_wait_seconds,
            "bbox": self.config.bbox.as_dict(),
            "source_backends": list(self.config.source_backends),
            "mirror_base_urls": dict(self.config.mirror_base_urls),
            "nomads_cooldown_minutes": self.config.nomads_cooldown_minutes,
            "mirror_cooldown_minutes": self.config.mirror_cooldown_minutes,
        }

    def source_object_identity(
        self,
        cycle_time: str | datetime,
        forecast_hours: list[int] | None = None,
    ) -> dict[str, Any]:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        requested_hours = list(forecast_hours if forecast_hours is not None else self.config.forecast_hours())
        hours = self._effective_forecast_hours(requested_hours)
        entries = self._source_object_entry_identities(parsed_cycle_time, hours)
        entry_digest = _stable_digest(entries)
        return {
            "identity_schema_version": "nhms.source_object_identity.v3",
            "source": self.config.source_id,
            "cycle_time": parsed_cycle_time.isoformat(),
            "base_url": self._safe_text(self.config.base_url),
            "idx_selector_schema_version": GFS_IDX_SELECTOR_SCHEMA_VERSION,
            "apcp_selector_policy": GFS_APCP_SELECTOR_POLICY,
            "bbox": self.config.bbox.as_dict(),
            "requested_forecast_hours": requested_hours,
            "first_forecast_hour": min(hours) if hours else None,
            "last_forecast_hour": max(hours) if hours else None,
            "forecast_hour_count": len(hours),
            "variable_count": len(self.config.variables),
            "grib_short_names": self._grib_short_names_identity(),
            "manifest_object_key": f"raw/{self.config.source_id}/{format_cycle_time(parsed_cycle_time)}/manifest.json",
            "manifest_digest": _stable_digest(
                {
                    "source_id": self.config.source_id,
                    "cycle_time": parsed_cycle_time.isoformat(),
                    "entries": entries,
                    "source_policy": self.source_policy_identity(hours),
                }
            ),
            "raw_entry_count": len(entries),
            "raw_entry_digest": entry_digest,
            "raw_entry_observation_digest_by_key": _entry_observation_digest_by_key(entries),
            "remote_identity_digest": _stable_digest([entry["remote_identity"] for entry in entries]),
            "raw_entry_samples": _entry_samples(entries),
        }

    def _safe_text(self, value: object) -> str:
        return str(redact_payload(str(value)))

    def _source_object_entry_identities(
        self,
        cycle_time: datetime,
        forecast_hours: list[int],
    ) -> list[dict[str, Any]]:
        compact_cycle = format_cycle_time(cycle_time)
        entries: list[dict[str, Any]] = []
        for forecast_hour in forecast_hours:
            bundle_variables = self._variables_for_forecast_hour(forecast_hour)
            local_key = (
                f"raw/{self.config.source_id}/{compact_cycle}/"
                f"{self.raw_bundle_filename(cycle_time, forecast_hour)}"
            )
            for variable in bundle_variables:
                entries.append(
                    {
                        "local_key": local_key,
                        "remote_identity": self._safe_text(
                            self.remote_bundle_url(cycle_time, forecast_hour, bundle_variables)
                        ),
                        "variable": variable,
                        "forecast_hour": forecast_hour,
                        "selector_policy": self._entry_selector_policy(variable, forecast_hour),
                        "bundle_variables": list(bundle_variables),
                        "expected_checksum": None,
                        "expected_size_bytes": None,
                        "observed_raw_object": self._raw_object_observation(local_key),
                    }
                )
        return entries

    def _entry_selector_policy(self, variable: str, forecast_hour: int) -> dict[str, Any]:
        if variable == "apcp":
            return {
                "idx_selector_schema_version": GFS_IDX_SELECTOR_SCHEMA_VERSION,
                "accumulation_policy": "prefer_cumulative_since_cycle_else_unique_interval_bucket",
                "preferred_step_range": f"0-{forecast_hour}",
                "allow_unique_interval_bucket_fallback": True,
                "allow_duplicate_identical_step_range": True,
            }
        return {
            "idx_selector_schema_version": GFS_IDX_SELECTOR_SCHEMA_VERSION,
            "accumulation_policy": "default_unique_record",
        }

    def _raw_object_observation(self, local_key: str) -> dict[str, Any]:
        try:
            if not self.object_store.exists(local_key):
                return {"status": "missing", "checksum": None, "size_bytes": None}
            size_bytes = self.object_store.size(local_key)
            if size_bytes > self.config.max_file_size_bytes:
                return {
                    "status": "oversized",
                    "checksum": None,
                    "size_bytes": size_bytes,
                    "max_size_bytes": self.config.max_file_size_bytes,
                }
            return {
                "status": "present",
                "checksum": self.object_store.checksum(local_key),
                "size_bytes": size_bytes,
            }
        except (OSError, ObjectStoreError, ValueError) as error:
            return {
                "status": "unavailable",
                "checksum": None,
                "size_bytes": None,
                "error_type": type(error).__name__,
            }

    def _trusted_raw_observation_matches(
        self,
        local_key: str,
        *,
        trusted_source_object_identity: Mapping[str, Any] | None,
        size_bytes: int,
        expected_checksum: str | None = None,
    ) -> bool:
        trusted_observation = _trusted_observed_raw_object(
            trusted_source_object_identity,
            local_key=local_key,
        )
        if trusted_observation is None or trusted_observation.get("status") != "present":
            return False
        trusted_size = trusted_observation.get("size_bytes")
        if trusted_size is None or int(trusted_size) != int(size_bytes):
            return False
        trusted_checksum = trusted_observation.get("checksum")
        if not trusted_checksum:
            return False
        if expected_checksum is not None and trusted_checksum != expected_checksum:
            return False
        return self.object_store.checksum(local_key) == trusted_checksum

    def _trusted_prior_source_object_identity(
        self,
        manifest: DownloadManifest,
        existing_cycle: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if existing_cycle is None or existing_cycle.get("status") != "raw_complete":
            return None
        manifest_uri = existing_cycle.get("manifest_uri")
        if not isinstance(manifest_uri, str) or not manifest_uri:
            return None
        try:
            prior_manifest = self.load_manifest(manifest_uri)
        except GFSAdapterError:
            LOGGER.warning("Raw-complete GFS cycle has unreadable prior manifest %s", manifest_uri, exc_info=True)
            return None
        current_policy = manifest.metadata.get("source_policy")
        prior_policy = prior_manifest.metadata.get("source_policy")
        if _stable_digest(current_policy) != _stable_digest(prior_policy):
            return None
        prior_identity = prior_manifest.metadata.get("source_object_identity")
        if not isinstance(prior_identity, Mapping):
            return None
        if prior_identity.get("source") != manifest.source_id:
            return None
        if str(prior_identity.get("cycle_time") or "") != manifest.cycle_time.isoformat():
            return None
        return dict(prior_identity)

    def _get_cycle(self, cycle_time: datetime) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        try:
            return self.repository.get_forecast_cycle(source_id=self.config.source_id, cycle_time=cycle_time)
        except Exception:
            LOGGER.exception("Failed to read forecast cycle %s", format_cycle_time(cycle_time))
            raise

    def _update_cycle(
        self,
        *,
        cycle_time: datetime,
        status: str | None = None,
        manifest_uri: str | None = None,
        retry_count: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self.repository is None:
            return
        try:
            self.repository.update_forecast_cycle(
                source_id=self.config.source_id,
                cycle_time=cycle_time,
                status=status,
                manifest_uri=manifest_uri,
                retry_count=retry_count,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            LOGGER.exception("Failed to update forecast cycle %s", format_cycle_time(cycle_time))
            raise

    def _delete_partial(self, local_key: str) -> None:
        try:
            self.object_store.delete(local_key)
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.warning("Failed to clean partial raw object %s", local_key, exc_info=True)

    def _backoff_for(self, attempt: int) -> float:
        index = max(0, min(attempt - 1, len(self.config.retry_backoff_seconds) - 1))
        return self.config.retry_backoff_seconds[index]

    def raw_filename(self, cycle_time: str | datetime, forecast_hour: int, variable: str) -> str:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        return f"gfs.t{parsed_cycle_time:%H}z.pgrb2.0p25.f{forecast_hour:03d}.{variable}.grib2"

    def _grib_short_names_identity(self) -> dict[str, str]:
        return {variable: GFS_GRIB_SHORT_NAME.get(variable, variable) for variable in self.config.variables}

    def raw_bundle_filename(self, cycle_time: str | datetime, forecast_hour: int) -> str:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        return f"gfs.t{parsed_cycle_time:%H}z.pgrb2.0p25.f{forecast_hour:03d}.bundle.grib2"

    def remote_url(self, cycle_time: str | datetime, forecast_hour: int, variable: str) -> str:
        return self.remote_bundle_url(cycle_time, forecast_hour, (variable,))

    def remote_bundle_url(
        self,
        cycle_time: str | datetime,
        forecast_hour: int,
        variables: tuple[str, ...],
    ) -> str:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        for variable in variables:
            if variable not in NOMADS_QUERY_PARAMS:
                raise ValueError(f"Unsupported GFS variable: {variable}")

        file_name = f"gfs.t{parsed_cycle_time:%H}z.pgrb2.0p25.f{forecast_hour:03d}"
        directory = f"/gfs.{parsed_cycle_time:%Y%m%d}/{parsed_cycle_time:%H}/atmos"
        bbox = self.config.bbox
        variable_params: dict[str, str] = {}
        for variable in variables:
            variable_params.update(NOMADS_QUERY_PARAMS[variable])
        query = {
            "dir": directory,
            "file": file_name,
            **variable_params,
            "subregion": "on",
            "leftlon": f"{bbox.west:g}",
            "rightlon": f"{bbox.east:g}",
            "toplat": f"{bbox.north:g}",
            "bottomlat": f"{bbox.south:g}",
        }
        return f"{self._filter_endpoint()}?{urlencode(query, quote_via=quote)}"

    def _filter_endpoint(self) -> str:
        parsed = urlparse(self.config.base_url)
        if "/cgi-bin/" in parsed.path:
            return self.config.base_url.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}/cgi-bin/filter_gfs_0p25.pl"


def _minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _parse_content_range(value: str) -> tuple[int, int] | None:
    prefix = "bytes "
    if not value.startswith(prefix):
        return None
    range_part = value[len(prefix) :].split("/", 1)[0]
    start_text, separator, end_text = range_part.partition("-")
    if not separator:
        return None
    try:
        return int(start_text), int(end_text)
    except ValueError:
        return None


@dataclass(frozen=True)
class IdxRecord:
    record_number: int
    start_byte: int
    field: str
    level: str
    forecast: str  # e.g. "anl", "3 hour fcst", "0-6 hour acc fcst"


def _parse_gfs_idx(idx_text: str) -> list[IdxRecord]:
    """Parse a wgrib2 .idx into ordered records.

    Each line is ``<rec>:<start>:d=YYYYMMDDHH:<FIELD>:<level>:<forecast>:``. The byte
    range of record N is ``[start_N, start_{N+1})``; the last record runs to EOF.
    """
    records: list[IdxRecord] = []
    for raw_line in idx_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 6:
            raise IdxParseError(f"Malformed GFS .idx line: {raw_line!r}")
        try:
            record_number = int(parts[0])
            start_byte = int(parts[1])
        except ValueError as error:
            raise IdxParseError(f"Malformed GFS .idx line: {raw_line!r}") from error
        records.append(
            IdxRecord(
                record_number=record_number,
                start_byte=start_byte,
                field=parts[3],
                level=parts[4],
                forecast=parts[5],
            )
        )
    if not records:
        raise IdxParseError("GFS .idx is empty")
    return records


def _idx_forecast_matches(record: IdxRecord, variable: str, forecast_hour: int) -> bool:
    forecast = record.forecast
    if variable in {"apcp", "dswrf"}:
        # Accumulated/averaged fields carry a window token (e.g. "0-6 hour acc fcst",
        # "0-6 hour ave fcst"); the window must end at the requested forecast hour.
        keyword = "acc fcst" if variable == "apcp" else "ave fcst"
        if keyword not in forecast:
            return False
        bounds = _idx_forecast_window_bounds(record)
        return bounds is not None and bounds[1] == forecast_hour
    if forecast_hour == 0:
        return forecast == "anl"
    return forecast == f"{forecast_hour} hour fcst"


def _idx_step_range(record: IdxRecord) -> str | None:
    bounds = _idx_forecast_window_bounds(record)
    if bounds is None:
        return None
    return f"{bounds[0]}-{bounds[1]}"


def _idx_forecast_window_bounds(record: IdxRecord) -> tuple[int, int] | None:
    forecast = record.forecast.strip()
    match = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s+(hour|hours|day|days)\b", forecast)
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    unit = match.group(3)
    multiplier = 24 if unit.startswith("day") else 1
    return start * multiplier, end * multiplier


def _idx_byte_range_for_match(records: list[IdxRecord], index: int) -> tuple[int, int | None]:
    start = records[index].start_byte
    end = records[index + 1].start_byte if index + 1 < len(records) else None
    return start, end


def _idx_step_range_bounds(step_range: str | None) -> tuple[int, int] | None:
    if step_range is None:
        return None
    start, separator, end = step_range.partition("-")
    if not separator:
        return None
    try:
        return int(start), int(end)
    except ValueError:
        return None


def _apcp_accumulation_type(step_range: str | None, forecast_hour: int) -> str | None:
    bounds = _idx_step_range_bounds(step_range)
    if bounds is None:
        return None
    start, end = bounds
    if end != forecast_hour:
        return None
    return "cumulative_since_cycle" if start == 0 else "interval_bucket"


def _select_apcp_idx_match(
    records: list[IdxRecord],
    matches: list[int],
    forecast_hour: int,
    *,
    expected_interval_hours: int | None = None,
) -> IdxSelection:
    preferred_step_range = f"0-{forecast_hour}"
    preferred = [index for index in matches if _idx_step_range(records[index]) == preferred_step_range]
    if len(preferred) == 1:
        index = preferred[0]
        return IdxSelection(
            byte_range=_idx_byte_range_for_match(records, index),
            step_range=preferred_step_range,
            accumulation_type="cumulative_since_cycle",
            record_number=records[index].record_number,
        )

    # FV3-GFS can carry two APCP records with the same 0-fhr text in early lead
    # times. They are equivalent for the cumulative-since-cycle policy; keep the
    # selection deterministic and visible instead of failing the whole cycle.
    if len(preferred) > 1:
        descriptions = "; ".join(
            f"record={records[i].record_number}:{records[i].field}:{records[i].level}:{records[i].forecast}"
            for i in preferred
        )
        warning = (
            f"duplicate_identical_cumulative_step_range:{preferred_step_range}:"
            f"chosen_record={records[preferred[0]].record_number}"
        )
        LOGGER.warning(
            "Duplicate GFS APCP cumulative .idx records at f%03d with stepRange=%s; choosing record %s (%s)",
            forecast_hour,
            preferred_step_range,
            records[preferred[0]].record_number,
            descriptions,
        )
        return IdxSelection(
            byte_range=_idx_byte_range_for_match(records, preferred[0]),
            step_range=preferred_step_range,
            accumulation_type="cumulative_since_cycle",
            record_number=records[preferred[0]].record_number,
            selector_warning=warning,
        )

    bucket_matches = [
        index
        for index in matches
        if _apcp_accumulation_type(_idx_step_range(records[index]), forecast_hour) == "interval_bucket"
        and (
            expected_interval_hours is None
            or (
                (bounds := _idx_step_range_bounds(_idx_step_range(records[index]))) is not None
                and bounds[1] - bounds[0] == expected_interval_hours
            )
        )
    ]
    if len(bucket_matches) == 1:
        index = bucket_matches[0]
        step_range = _idx_step_range(records[index])
        LOGGER.warning(
            "GFS APCP f%03d has no %s cumulative .idx record; using unique interval bucket %s record %s",
            forecast_hour,
            preferred_step_range,
            step_range,
            records[index].record_number,
        )
        return IdxSelection(
            byte_range=_idx_byte_range_for_match(records, index),
            step_range=step_range,
            accumulation_type="interval_bucket",
            record_number=records[index].record_number,
            selector_warning=f"cumulative_absent_used_interval_bucket:{step_range}",
        )

    descriptions = "; ".join(f"{records[i].field}:{records[i].level}:{records[i].forecast}" for i in matches)
    if bucket_matches:
        raise IdxSelectorPolicyError(
            (
                f"No GFS APCP cumulative .idx record with stepRange={preferred_step_range} "
                f"and multiple compatible interval bucket records at f{forecast_hour:03d}; "
                f"expected_interval_hours={expected_interval_hours}; matched records: {descriptions}"
            )
        )
    raise IdxSelectorPolicyError(
        (
            f"No GFS APCP cumulative .idx record with stepRange={preferred_step_range} "
            f"or unique compatible interval bucket at f{forecast_hour:03d}; "
            f"expected_interval_hours={expected_interval_hours}; matched records: {descriptions}"
        )
    )


def _select_idx_record(
    records: list[IdxRecord],
    variable: str,
    forecast_hour: int,
    *,
    expected_interval_hours: int | None = None,
) -> IdxSelection:
    if variable not in GFS_IDX_FIELD_LEVEL:
        raise ValueError(f"Unsupported GFS variable for idx selection: {variable}")
    field, level = GFS_IDX_FIELD_LEVEL[variable]
    matches = [
        index
        for index, record in enumerate(records)
        if record.field == field
        and record.level == level
        and _idx_forecast_matches(record, variable, forecast_hour)
    ]
    if not matches:
        raise IdxRecordNotFoundError(
            f"No GFS .idx record for {variable} ({field}:{level}) at f{forecast_hour:03d}",
            attempts=1,
        )
    if variable == "apcp":
        return _select_apcp_idx_match(
            records,
            matches,
            forecast_hour,
            expected_interval_hours=expected_interval_hours,
        )
    if len(matches) > 1:
        descriptions = "; ".join(f"{records[i].field}:{records[i].level}:{records[i].forecast}" for i in matches)
        raise AmbiguousIdxRecordError(
            f"Ambiguous GFS .idx selection for {variable} at f{forecast_hour:03d}: {descriptions}"
        )
    return IdxSelection(
        byte_range=_idx_byte_range_for_match(records, matches[0]),
        step_range=_idx_step_range(records[matches[0]]),
        record_number=records[matches[0]].record_number,
    )


def _select_idx_byte_range(
    records: list[IdxRecord],
    variable: str,
    forecast_hour: int,
) -> tuple[int, int | None]:
    return _select_idx_record(records, variable, forecast_hour).byte_range


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _mapping_value(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _entry_samples(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(entries) <= 2:
        return [dict(entry) for entry in entries]
    return [dict(entries[0]), dict(entries[-1])]


def _entry_observation_digest_by_key(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    observations: dict[str, dict[str, Any]] = {}
    for entry in entries:
        local_key = entry.get("local_key")
        observed = entry.get("observed_raw_object")
        if not isinstance(local_key, str) or not isinstance(observed, Mapping):
            continue
        observations[local_key] = {
            "status": observed.get("status"),
            "checksum": observed.get("checksum"),
            "size_bytes": observed.get("size_bytes"),
            "digest": _stable_digest(observed),
        }
    return observations


def _trusted_observed_raw_object(
    source_object_identity: Mapping[str, Any] | None,
    *,
    local_key: str,
) -> Mapping[str, Any] | None:
    if not isinstance(source_object_identity, Mapping):
        return None
    by_key = source_object_identity.get("raw_entry_observation_digest_by_key")
    if isinstance(by_key, Mapping):
        observed = by_key.get(local_key)
        if isinstance(observed, Mapping):
            return observed
    samples = source_object_identity.get("raw_entry_samples")
    if not isinstance(samples, list):
        return None
    for sample in samples:
        if not isinstance(sample, Mapping) or sample.get("local_key") != local_key:
            continue
        observed = sample.get("observed_raw_object")
        if isinstance(observed, Mapping):
            return observed
    return None
