from __future__ import annotations

import json
import logging
import math
import os
import queue
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from packages.common.met_store import PsycopgMetStore
from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes

ERA5_VARIABLES: tuple[str, ...] = (
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "surface_pressure",
    "total_precipitation",
    "surface_net_solar_radiation",
    "surface_net_thermal_radiation",
)

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
    parse_cycle_date,
)
from .gfs_adapter import ForecastCycleRepository

LOGGER = logging.getLogger(__name__)

ERA5_DATASET_NAME = "reanalysis-era5-single-levels"
ERA5_FORECAST_HOURS = tuple(range(24))
ERA5_DEFAULT_AREA = (55.0, 70.0, 15.0, 140.0)
ERA5_MAX_DISCOVERY_RANGE_DAYS = 365
ERA5_ACCUMULATED_VARIABLES = {
    "total_precipitation",
    "surface_net_solar_radiation",
    "surface_net_thermal_radiation",
}


class CDSClient(Protocol):
    def is_available(self, request: Mapping[str, Any]) -> bool: ...

    def retrieve(
        self,
        dataset: str,
        request: Mapping[str, Any],
        target: Path,
        *,
        timeout_seconds: float,
    ) -> None: ...


class CDSAPIClient:
    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            try:
                import cdsapi
            except ImportError as error:
                raise ERA5AdapterError("cdsapi is required for real ERA5 downloads.") from error
            client = cdsapi.Client()
        self.client = client

    def is_available(self, request: Mapping[str, Any]) -> bool:
        return True

    def retrieve(
        self,
        dataset: str,
        request: Mapping[str, Any],
        target: Path,
        *,
        timeout_seconds: float,
    ) -> None:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError(f"CDS retrieve timed out before starting; timeout_seconds={timeout_seconds!r}")

        outcome: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

        def retrieve_in_thread() -> None:
            try:
                self.client.retrieve(dataset, dict(request), str(target))
            except BaseException as error:
                outcome.put(error)
            else:
                outcome.put(None)

        thread = threading.Thread(target=retrieve_in_thread, name="era5-cds-retrieve", daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(f"CDS retrieve timed out after {timeout:g} seconds.")

        try:
            error = outcome.get(timeout=1)
        except queue.Empty:
            return
        if error is not None:
            raise error


ERA5_GCS_DEFAULT_STORE = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"

CDS_TO_ZARR_VARIABLE: dict[str, str] = {
    "2m_temperature": "2m_temperature",
    "2m_dewpoint_temperature": "2m_dewpoint_temperature",
    "10m_u_component_of_wind": "10m_u_component_of_wind",
    "10m_v_component_of_wind": "10m_v_component_of_wind",
    "surface_pressure": "surface_pressure",
    "total_precipitation": "total_precipitation",
    "surface_net_solar_radiation": "surface_net_solar_radiation",
    "surface_net_thermal_radiation": "surface_net_thermal_radiation",
}


class GCSZarrClient:
    """ERA5 data client using ARCO-ERA5 on Google Cloud Storage (anonymous, no credentials needed)."""

    def __init__(self, store_url: str | None = None) -> None:
        self._store_url = store_url or os.getenv("ERA5_GCS_ZARR_STORE", ERA5_GCS_DEFAULT_STORE)
        self._dataset: Any = None
        self._lock = threading.Lock()

    def _open_dataset(self) -> Any:
        if self._dataset is not None:
            return self._dataset
        with self._lock:
            if self._dataset is not None:
                return self._dataset
            try:
                import gcsfs
                import xarray as xr
            except ImportError as error:
                raise ERA5AdapterError(
                    "gcsfs and xarray are required for GCS ERA5 downloads. "
                    "Install with: pip install gcsfs zarr"
                ) from error
            fs = gcsfs.GCSFileSystem(token="anon")
            store = fs.get_mapper(self._store_url)
            self._dataset = xr.open_zarr(store, consolidated=True)
            return self._dataset

    def is_available(self, request: Mapping[str, Any]) -> bool:
        import numpy as np

        ds = self._open_dataset()
        try:
            target_date = _date_from_request(request)
            target_np = np.datetime64(f"{target_date.isoformat()}T00:00:00")
            time_vals = ds.time.values
            return bool(time_vals[0] <= target_np <= time_vals[-1])
        except (KeyError, IndexError, ValueError):
            return False

    def retrieve(
        self,
        dataset: str,
        request: Mapping[str, Any],
        target: Path,
        *,
        timeout_seconds: float,
    ) -> None:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError(f"GCS retrieve timed out before starting; timeout_seconds={timeout_seconds!r}")

        outcome: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

        def _do_retrieve() -> None:
            try:
                self._retrieve_inner(request, target)
            except BaseException as error:
                outcome.put(error)
            else:
                outcome.put(None)

        thread = threading.Thread(target=_do_retrieve, name="era5-gcs-retrieve", daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(f"GCS Zarr retrieve timed out after {timeout:g} seconds.")

        try:
            error = outcome.get(timeout=1)
        except queue.Empty:
            return
        if error is not None:
            raise error

    def _retrieve_inner(self, request: Mapping[str, Any], target: Path) -> None:
        import numpy as np

        ds = self._open_dataset()
        variable = str(request["variable"])
        zarr_var = CDS_TO_ZARR_VARIABLE.get(variable)
        if zarr_var is None:
            raise ERA5AdapterError(f"No GCS Zarr mapping for ERA5 variable: {variable}")
        if zarr_var not in ds.data_vars:
            available = sorted(str(v) for v in ds.data_vars)
            raise ERA5AdapterError(
                f"Variable '{zarr_var}' not found in Zarr store. Available: {available[:20]}"
            )

        cycle_date = _date_from_request(request)
        hour = int(str(request["time"]).split(":", maxsplit=1)[0])
        target_time = np.datetime64(f"{cycle_date.isoformat()}T{hour:02d}:00:00")

        area = request.get("area", list(ERA5_DEFAULT_AREA))
        north, west, south, east = (float(area[0]), float(area[1]), float(area[2]), float(area[3]))

        da = ds[zarr_var].sel(time=target_time, method="nearest")
        if "time" in da.coords:
            actual_time = da.time.values
            if abs(actual_time - target_time) > np.timedelta64(1, "h"):
                raise ERA5AdapterError(
                    f"GCS Zarr exact time {target_time} not found; nearest is {actual_time}"
                )

        lat_dim = "latitude" if "latitude" in da.dims else "lat"
        lon_dim = "longitude" if "longitude" in da.dims else "lon"

        lat_vals = da[lat_dim].values
        if lat_vals[0] > lat_vals[-1]:
            da = da.sel({lat_dim: slice(north, south)})
        else:
            da = da.sel({lat_dim: slice(south, north)})

        lon_vals = da[lon_dim].values
        lon_is_360 = float(lon_vals.max()) > 180.0
        if lon_is_360:
            west_adj = west % 360.0 if west < 0 else west
            east_adj = east % 360.0 if east < 0 else east
            if abs(east - west) >= 360.0:
                pass
            elif west_adj > east_adj:
                part_a = da.sel({lon_dim: slice(west_adj, 360.0)})
                part_b = da.sel({lon_dim: slice(0.0, east_adj)})
                import xarray as xr
                da = xr.concat([part_a, part_b], dim=lon_dim)
            else:
                da = da.sel({lon_dim: slice(west_adj, east_adj)})
        else:
            da = da.sel({lon_dim: slice(west, east)})

        if da.size == 0:
            raise ERA5AdapterError(
                f"GCS Zarr selection returned empty data for {variable} "
                f"area=[{north},{west},{south},{east}]"
            )

        slice_ds = da.to_dataset(name=zarr_var)
        slice_ds.attrs["source"] = "ARCO-ERA5-GCS"
        slice_ds.attrs["variable_cds_name"] = variable
        slice_ds.attrs["forecast_hour"] = hour
        cycle_time_dt = datetime(cycle_date.year, cycle_date.month, cycle_date.day, hour, tzinfo=UTC)
        slice_ds.attrs["cycle_time"] = cycle_time_dt.isoformat()
        slice_ds.to_netcdf(target, engine="netcdf4")
        slice_ds.close()


class MockCDSClient:
    def __init__(
        self,
        *,
        available_dates: set[date] | None = None,
        unavailable_dates: set[date] | None = None,
        failures_before_success: int = 0,
        failure_factory: Callable[[], Exception] | None = None,
    ) -> None:
        self.available_dates = available_dates
        self.unavailable_dates = unavailable_dates or set()
        self.failures_before_success = failures_before_success
        self.failure_factory = failure_factory or (lambda: TimeoutError("mock CDS timeout"))
        self.availability_requests: list[dict[str, Any]] = []
        self.retrieve_requests: list[dict[str, Any]] = []

    def is_available(self, request: Mapping[str, Any]) -> bool:
        normalized = dict(request)
        self.availability_requests.append(normalized)
        request_date = _date_from_request(normalized)
        if request_date in self.unavailable_dates:
            return False
        if self.available_dates is None:
            return True
        return request_date in self.available_dates

    def retrieve(
        self,
        dataset: str,
        request: Mapping[str, Any],
        target: Path,
        *,
        timeout_seconds: float,
    ) -> None:
        normalized = dict(request)
        normalized["dataset"] = dataset
        normalized["timeout_seconds"] = timeout_seconds
        self.retrieve_requests.append(normalized)

        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise self.failure_factory()

        from packages.common.test_netcdf4 import encode_test_netcdf4

        cycle_time = _cycle_time_from_request(request)
        variable = str(request["variable"])
        forecast_hour = int(str(request["time"]).split(":", maxsplit=1)[0])
        target.write_bytes(encode_test_netcdf4(variable, forecast_hour, cycle_time=cycle_time, source="ERA5"))


class ERA5AdapterError(RuntimeError):
    error_code = "ERA5_ADAPTER_ERROR"

    def __init__(self, message: str, *, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


class CDSDownloadError(ERA5AdapterError):
    error_code = "CDS_DOWNLOAD_ERROR"


class CDSRequestTimeoutError(ERA5AdapterError):
    error_code = "CDS_TIMEOUT"


class ChecksumMismatchError(ERA5AdapterError):
    error_code = "CHECKSUM_MISMATCH"


class RetrievedFileTooSmallError(ERA5AdapterError):
    error_code = "SIZE_TOO_SMALL"


@dataclass(frozen=True)
class RetrievedPayload:
    content: bytes
    checksum: str
    bytes_written: int


@dataclass(frozen=True)
class ERA5AdapterConfig:
    source_id: str = "ERA5"
    source_name: str = "ERA5 Reanalysis"
    source_type: str = "reanalysis"
    status: str = "enabled"
    native_format: str = "GRIB"
    adapter_name: str = "era5"
    dataset_name: str = ERA5_DATASET_NAME
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_prefix: str = field(default_factory=lambda: os.getenv("OBJECT_STORE_PREFIX", ""))
    variables: tuple[str, ...] = ERA5_VARIABLES
    cycle_hours_utc: tuple[int, ...] = (0,)
    area: tuple[float, float, float, float] = ERA5_DEFAULT_AREA
    cds_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("ERA5_CDS_TIMEOUT_SECONDS", "7200")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("ERA5_MAX_RETRIES", "3")))
    retry_backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0)
    min_file_size_bytes: int = 1
    availability_lag_days: int = 5
    backend: str = field(default_factory=lambda: os.getenv("ERA5_BACKEND", "gcs"))

    def forecast_hours(self) -> list[int]:
        return list(ERA5_FORECAST_HOURS)

    def as_data_source_config(self) -> dict[str, Any]:
        return {
            "dataset_name": self.dataset_name,
            "cycle_hours_utc": list(self.cycle_hours_utc),
            "forecast_hours": self.forecast_hours(),
            "variables": list(self.variables),
            "area": list(self.area),
            "cds_timeout_seconds": self.cds_timeout_seconds,
            "max_retries": self.max_retries,
            "availability_lag_days": self.availability_lag_days,
        }


class ERA5Adapter(DataSourceAdapter):
    def __init__(
        self,
        *,
        config: ERA5AdapterConfig | None = None,
        repository: ForecastCycleRepository | None = None,
        object_store: LocalObjectStore | None = None,
        cds_client: CDSClient | None = None,
        sleeper: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or ERA5AdapterConfig()
        self.repository = repository
        self.object_store = object_store or LocalObjectStore(
            self.config.workspace_root,
            object_store_prefix=self.config.object_store_prefix,
        )
        self.cds_client = cds_client or self._default_client(self.config.backend)
        self.sleeper = sleeper or time.sleep
        self.now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _default_client(backend: str) -> CDSClient:
        if backend == "gcs":
            return GCSZarrClient()
        return CDSAPIClient()

    @classmethod
    def from_env(cls, *, area: tuple[float, float, float, float] | None = None) -> ERA5Adapter:
        config = ERA5AdapterConfig(area=area or _area_from_env())
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

    def discover_cycles(
        self,
        cycle_date: str | date | datetime,
        end_date: str | date | datetime | None = None,
    ) -> list[CycleDiscovery]:
        self.initialize_data_source()
        start = parse_cycle_date(cycle_date)
        end = parse_cycle_date(end_date) if end_date is not None else start
        if (end - start).days > ERA5_MAX_DISCOVERY_RANGE_DAYS:
            raise ERA5AdapterError(
                f"ERA5 discovery date range cannot exceed {ERA5_MAX_DISCOVERY_RANGE_DAYS} days."
            )
        discoveries: list[CycleDiscovery] = []

        for target_date in _date_range(start, end):
            for cycle_hour in self.config.cycle_hours_utc:
                cycle_time = datetime(
                    target_date.year,
                    target_date.month,
                    target_date.day,
                    cycle_hour,
                    tzinfo=UTC,
                )
                cycle_id = cycle_id_for(self.config.source_id, cycle_time)
                status = "discovered"
                if self._within_latency_window(target_date):
                    available = False
                    status = "not_yet_available"
                else:
                    try:
                        available = self.cds_client.is_available(self.availability_request(target_date))
                    except Exception:
                        LOGGER.exception("Failed to check ERA5 availability for %s", target_date.isoformat())
                        available = False
                        status = "availability_check_failed"
                    if not available and status == "discovered":
                        status = "not_available"

                discoveries.append(
                    CycleDiscovery(
                        cycle_id=cycle_id,
                        source_id=self.config.source_id,
                        cycle_time=cycle_time,
                        cycle_hour=cycle_hour,
                        available=available,
                        status=status,
                    )
                )

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

    def build_manifest(
        self,
        cycle_time: str | datetime,
        forecast_hours: list[int] | None = None,
    ) -> DownloadManifest:
        self.initialize_data_source()
        parsed_cycle_time = parse_era5_cycle_time(cycle_time)
        date_key = _date_key(parsed_cycle_time)
        variables = tuple(self.config.variables)
        if not variables:
            raise ERA5AdapterError("ERA5 manifest requires at least one variable.")
        hours = list(forecast_hours if forecast_hours is not None else self.config.forecast_hours())
        if not hours:
            raise ERA5AdapterError("ERA5 manifest requires at least one forecast hour.")
        entries: list[ManifestEntry] = []

        for forecast_hour in hours:
            for variable in variables:
                local_key = f"raw/{self.config.source_id}/{date_key}/{variable}_{forecast_hour:02d}.grib"
                entries.append(
                    ManifestEntry(
                        remote_url=f"cds://{self.config.dataset_name}/{date_key}/{variable}/{forecast_hour:02d}",
                        local_key=local_key,
                        variable=variable,
                        forecast_hour=forecast_hour,
                        expected_size_bytes=self.config.min_file_size_bytes,
                        metadata={
                            "cycle_time": parsed_cycle_time.isoformat(),
                            "valid_time": (parsed_cycle_time + timedelta(hours=forecast_hour)).isoformat(),
                            "area": list(self.config.area),
                            "time": f"{forecast_hour:02d}:00",
                            "accumulation_type": "since_midnight"
                            if variable in ERA5_ACCUMULATED_VARIABLES
                            else "instantaneous",
                        },
                    )
                )

        metadata = {
            "cycle_time": parsed_cycle_time.isoformat(),
            "date": date_key,
            "forecast_hours": hours,
            "first_forecast_hour": min(hours) if hours else None,
            "last_forecast_hour": max(hours) if hours else None,
            "variable_count": len(self.config.variables),
            "total_file_count": len(entries),
            "area": list(self.config.area),
            "dataset_name": self.config.dataset_name,
        }
        manifest_key = f"raw/{self.config.source_id}/{date_key}/manifest.json"
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
            LOGGER.exception("Failed to persist ERA5 manifest for %s", date_key)
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
                LOGGER.exception("Failed to update manifest_uri for ERA5 cycle %s", date_key)
                raise

        return manifest

    def load_manifest(self, manifest_uri: str) -> DownloadManifest:
        try:
            payload = json.loads(self.object_store.read_bytes(manifest_uri).decode("utf-8"))
        except (json.JSONDecodeError, OSError, ObjectStoreError, ValueError) as error:
            raise ERA5AdapterError(f"Failed to load manifest {manifest_uri}: {error}") from error
        return DownloadManifest.from_dict(payload)

    def download_plan(self, manifest: DownloadManifest) -> DownloadPlanResult:
        manifest = self._manifest_with_persisted_checksums(manifest)
        self._validate_download_manifest(manifest)
        cycle_time = manifest.cycle_time
        existing_cycle = self._get_cycle(cycle_time)
        already_done_by_key: dict[str, DownloadFileResult] = {}
        needs_download = False

        for entry in manifest.entries:
            already_done = self._already_done_result(entry)
            if already_done is None:
                needs_download = True
            else:
                already_done_by_key[entry.local_key] = already_done

        if not needs_download:
            results = tuple(already_done_by_key[entry.local_key] for entry in manifest.entries)
            if existing_cycle is not None and existing_cycle.get("status") == "raw_complete":
                return DownloadPlanResult(status="already_done", files=results, total_bytes_written=0, retry_count=0)

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
            return DownloadPlanResult(status="raw_complete", files=results, total_bytes_written=0, retry_count=0)

        self._update_cycle(cycle_time=cycle_time, status="downloading", error_code="", error_message="")

        results: list[DownloadFileResult] = []
        retry_count = 0
        total_bytes_written = 0

        for entry in manifest.entries:
            try:
                if entry.local_key in already_done_by_key:
                    results.append(already_done_by_key[entry.local_key])
                    continue

                result, retries = self._download_entry(entry, cycle_time)
                retry_count += retries
                total_bytes_written += result.bytes_written
                results.append(result)
            except ERA5AdapterError as error:
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
                LOGGER.exception("Unexpected ERA5 download failure for %s", entry.local_key)
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

        self._update_manifest_checksums(manifest)
        self._update_cycle(cycle_time=cycle_time, status="raw_complete", retry_count=0)
        return DownloadPlanResult(
            status="raw_complete",
            files=tuple(results),
            total_bytes_written=total_bytes_written,
            retry_count=retry_count,
        )

    def verify_manifest(self, manifest: DownloadManifest) -> VerificationResult:
        failures: list[VerificationFailure] = []
        for entry in manifest.entries:
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

    def availability_request(self, target_date: date) -> dict[str, Any]:
        return {
            "product_type": "reanalysis",
            "format": "grib",
            "variable": list(self.config.variables),
            "year": f"{target_date.year:04d}",
            "month": f"{target_date.month:02d}",
            "day": f"{target_date.day:02d}",
            "time": [f"{hour:02d}:00" for hour in self.config.forecast_hours()],
            "area": list(self.config.area),
        }

    def retrieve_request(self, entry: ManifestEntry, cycle_time: datetime) -> dict[str, Any]:
        return {
            "product_type": "reanalysis",
            "format": "grib",
            "variable": entry.variable,
            "year": f"{cycle_time.year:04d}",
            "month": f"{cycle_time.month:02d}",
            "day": f"{cycle_time.day:02d}",
            "time": f"{entry.forecast_hour:02d}:00",
            "area": list(self.config.area),
        }

    def _download_entry(self, entry: ManifestEntry, cycle_time: datetime) -> tuple[DownloadFileResult, int]:
        payload, retries = self._retrieve_with_retries(entry, cycle_time)
        if payload.bytes_written < (entry.expected_size_bytes or self.config.min_file_size_bytes):
            raise RetrievedFileTooSmallError(
                (
                    f"Retrieved {entry.local_key} is {payload.bytes_written} bytes; expected at least "
                    f"{entry.expected_size_bytes or self.config.min_file_size_bytes}"
                ),
                attempts=retries,
            )
        if entry.expected_checksum and payload.checksum != entry.expected_checksum:
            raise ChecksumMismatchError(
                (
                    f"Downloaded checksum mismatch for {entry.local_key}: expected {entry.expected_checksum}, "
                    f"actual {payload.checksum}"
                ),
                attempts=retries,
            )

        try:
            self.object_store.write_bytes_atomic(entry.local_key, payload.content)
        except (OSError, ObjectStoreError, ValueError) as error:
            self._delete_partial(entry.local_key)
            raise ERA5AdapterError(f"Failed to store {entry.local_key}: {error}", attempts=retries) from error

        return (
            DownloadFileResult(
                local_key=entry.local_key,
                status="downloaded",
                checksum=payload.checksum,
                bytes_written=payload.bytes_written,
            ),
            retries,
        )

    def _retrieve_with_retries(self, entry: ManifestEntry, cycle_time: datetime) -> tuple[RetrievedPayload, int]:
        last_error: Exception | None = None
        max_attempts = max(1, self.config.max_retries)
        request = self.retrieve_request(entry, cycle_time)
        for attempt in range(1, max_attempts + 1):
            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    target = Path(temp_dir) / Path(entry.local_key).name
                    self.cds_client.retrieve(
                        self.config.dataset_name,
                        request,
                        target,
                        timeout_seconds=self.config.cds_timeout_seconds,
                    )
                    content = target.read_bytes()
                return RetrievedPayload(content, sha256_bytes(content), len(content)), attempt - 1
            except TimeoutError as error:
                last_error = error
                if attempt >= max_attempts:
                    raise CDSRequestTimeoutError(
                        f"CDS retrieve timed out for {entry.local_key} after {max_attempts} attempts: {error}",
                        attempts=max_attempts - 1,
                    ) from error
                self.sleeper(self._backoff_for(attempt))
            except Exception as error:
                last_error = error
                if attempt >= max_attempts:
                    break
                self.sleeper(self._backoff_for(attempt))

        raise CDSDownloadError(
            f"Failed to retrieve {entry.local_key} after {max_attempts} attempts: {last_error}",
            attempts=max_attempts - 1,
        )

    def _entry_already_done(self, entry: ManifestEntry) -> bool:
        try:
            if not self.object_store.exists(entry.local_key):
                return False
            minimum_size = entry.expected_size_bytes or self.config.min_file_size_bytes
            if self.object_store.size(entry.local_key) < minimum_size:
                return False
            if entry.expected_checksum is None:
                return False
            return self.object_store.checksum(entry.local_key) == entry.expected_checksum
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to check ERA5 idempotency for %s", entry.local_key)
            return False

    def _already_done_result(self, entry: ManifestEntry) -> DownloadFileResult | None:
        if not self._entry_already_done(entry):
            return None
        try:
            checksum = self.object_store.checksum(entry.local_key)
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to checksum existing ERA5 raw object %s", entry.local_key)
            return None
        return DownloadFileResult(local_key=entry.local_key, status="already_done", checksum=checksum)

    def _within_latency_window(self, target_date: date) -> bool:
        current_date = self.now().astimezone(UTC).date()
        return (current_date - target_date).days < self.config.availability_lag_days

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
            error_message=error_message,
        )

    def _get_cycle(self, cycle_time: datetime) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        try:
            return self.repository.get_forecast_cycle(source_id=self.config.source_id, cycle_time=cycle_time)
        except Exception:
            LOGGER.exception("Failed to read ERA5 forecast cycle %s", cycle_time.isoformat())
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
            LOGGER.exception("Failed to update ERA5 forecast cycle %s", cycle_time.isoformat())
            raise

    def _delete_partial(self, local_key: str) -> None:
        try:
            self.object_store.delete(local_key)
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.warning("Failed to clean partial ERA5 raw object %s", local_key, exc_info=True)

    def _backoff_for(self, attempt: int) -> float:
        index = max(0, min(attempt - 1, len(self.config.retry_backoff_seconds) - 1))
        return self.config.retry_backoff_seconds[index]

    def _validate_download_manifest(self, manifest: DownloadManifest) -> None:
        if not manifest.entries:
            raise ERA5AdapterError("ERA5 download manifest contains no entries.")

    def _manifest_with_persisted_checksums(self, manifest: DownloadManifest) -> DownloadManifest:
        if manifest.manifest_uri is None or all(entry.expected_checksum for entry in manifest.entries):
            return manifest

        try:
            persisted = self.load_manifest(manifest.manifest_uri)
        except ERA5AdapterError:
            LOGGER.debug(
                "Unable to load persisted ERA5 manifest checksums from %s",
                manifest.manifest_uri,
                exc_info=True,
            )
            return manifest

        if not self._same_manifest_entries(manifest, persisted):
            LOGGER.warning("Persisted ERA5 manifest %s does not match the supplied manifest.", manifest.manifest_uri)
            return manifest
        return persisted

    def _same_manifest_entries(self, left: DownloadManifest, right: DownloadManifest) -> bool:
        left_identity = tuple((entry.local_key, entry.variable, entry.forecast_hour) for entry in left.entries)
        right_identity = tuple((entry.local_key, entry.variable, entry.forecast_hour) for entry in right.entries)
        return (
            left.source_id == right.source_id
            and left.cycle_time == right.cycle_time
            and left_identity == right_identity
        )

    def _update_manifest_checksums(self, manifest: DownloadManifest) -> DownloadManifest:
        if manifest.manifest_uri is None:
            return manifest

        updated_entries: list[ManifestEntry] = []
        for entry in manifest.entries:
            try:
                checksum = self.object_store.checksum(entry.local_key)
            except (OSError, ObjectStoreError, ValueError) as error:
                raise ERA5AdapterError(f"Failed to checksum downloaded ERA5 file {entry.local_key}: {error}") from error
            updated_entries.append(replace(entry, expected_checksum=checksum))

        updated_manifest = replace(manifest, entries=tuple(updated_entries))
        try:
            self.object_store.write_bytes_atomic(
                manifest.manifest_uri,
                json.dumps(updated_manifest.as_dict(), indent=2, sort_keys=True).encode("utf-8"),
            )
        except (OSError, ObjectStoreError, ValueError) as error:
            raise ERA5AdapterError(f"Failed to persist ERA5 manifest checksums: {error}") from error

        return updated_manifest


def parse_era5_cycle_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)

    candidate = value.strip()
    if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
        parsed_date = parse_cycle_date(candidate)
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
    from .base import parse_cycle_time

    parsed = parse_cycle_time(candidate)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end_date must be greater than or equal to cycle_date.")
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _area_from_env() -> tuple[float, float, float, float]:
    value = os.getenv("ERA5_AREA", "").strip()
    if not value:
        return ERA5_DEFAULT_AREA
    return parse_area(value)


def parse_area(value: str | tuple[float, float, float, float] | list[float]) -> tuple[float, float, float, float]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            raise ValueError("ERA5 area must be four comma-separated values: N,W,S,E.")
        try:
            parsed = tuple(float(part) for part in parts)
        except ValueError as error:
            raise ValueError("ERA5 area values must be numeric.") from error
    else:
        parsed = tuple(float(part) for part in value)

    if len(parsed) != 4:
        raise ValueError("ERA5 area must contain exactly four values: N,W,S,E.")
    north, west, south, east = parsed
    if not all(math.isfinite(coordinate) for coordinate in parsed):
        raise ValueError("ERA5 area coordinates must be finite numbers.")
    if not (-90.0 <= north <= 90.0 and -90.0 <= south <= 90.0):
        raise ValueError("ERA5 area latitude bounds must be between -90 and 90 degrees.")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise ValueError("ERA5 area longitude bounds must be between -180 and 180 degrees.")
    if north <= south:
        raise ValueError("ERA5 area north latitude must be greater than south latitude.")
    if east <= west:
        raise ValueError("ERA5 area east longitude must be greater than west longitude.")
    if (north - south) >= 180.0 and (east - west) >= 360.0:
        raise ValueError("ERA5 area must be smaller than the full globe.")
    return north, west, south, east


def _date_key(cycle_time: datetime) -> str:
    return cycle_time.astimezone(UTC).strftime("%Y-%m-%d")


def _date_from_request(request: Mapping[str, Any]) -> date:
    return date(int(str(request["year"])), int(str(request["month"])), int(str(request["day"])))


def _cycle_time_from_request(request: Mapping[str, Any]) -> datetime:
    request_date = _date_from_request(request)
    hour = int(str(request["time"]).split(":", maxsplit=1)[0])
    return datetime(request_date.year, request_date.month, request_date.day, hour, tzinfo=UTC)
