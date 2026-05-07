from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from packages.common.met_store import PsycopgMetStore
from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes

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
    parse_cycle_date,
    parse_cycle_time,
    valid_time_for,
)

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
    ) -> dict[str, Any]:
        ...

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
    ) -> dict[str, Any]:
        ...

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
    ) -> dict[str, Any] | None:
        ...

    def get_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any] | None:
        ...


class GFSAdapterError(RuntimeError):
    error_code = "GFS_ADAPTER_ERROR"

    def __init__(self, message: str, *, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


class FileUnavailableError(GFSAdapterError):
    error_code = "HTTP_404"


class NetworkDownloadError(GFSAdapterError):
    error_code = "NETWORK_ERROR"


class PollingTimeoutError(GFSAdapterError):
    error_code = "POLL_TIMEOUT"


class ChecksumMismatchError(GFSAdapterError):
    error_code = "CHECKSUM_MISMATCH"


class FileTooLargeError(GFSAdapterError):
    error_code = "FILE_TOO_LARGE"


@dataclass(frozen=True)
class DownloadedPayload:
    content: bytes
    checksum: str
    bytes_written: int


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
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_prefix: str = field(default_factory=lambda: os.getenv("OBJECT_STORE_PREFIX", ""))
    cycle_hours_utc: tuple[int, ...] = (0, 6, 12, 18)
    forecast_start_hour: int = 0
    forecast_end_hour: int = 168
    forecast_step_hours: int = 3
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

    def forecast_hours(self) -> list[int]:
        return list(range(self.forecast_start_hour, self.forecast_end_hour + 1, self.forecast_step_hours))

    def as_data_source_config(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
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
    ) -> None:
        self.config = config or GFSAdapterConfig()
        self.repository = repository
        self.object_store = object_store or LocalObjectStore(
            self.config.workspace_root,
            object_store_prefix=self.config.object_store_prefix,
        )
        self.downloader = downloader or self._download_url
        self.availability_checker = availability_checker or self._url_exists
        self.sleeper = sleeper or time.sleep
        self.clock = clock or time.monotonic

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
            remote_url = self.remote_url(cycle_time, forecast_hour=0, variable=self.config.variables[0])
            try:
                available = self.availability_checker(remote_url)
            except Exception:
                LOGGER.exception("Failed to check GFS availability for %s", remote_url)
                available = False

            discovery = CycleDiscovery(
                cycle_id=cycle_id,
                source_id=self.config.source_id,
                cycle_time=cycle_time,
                cycle_hour=cycle_hour,
                available=available,
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

    def build_manifest(
        self,
        cycle_time: str | datetime,
        forecast_hours: list[int] | None = None,
    ) -> DownloadManifest:
        self.initialize_data_source()
        parsed_cycle_time = parse_cycle_time(cycle_time)
        compact_cycle = format_cycle_time(parsed_cycle_time)
        hours = forecast_hours if forecast_hours is not None else self.config.forecast_hours()
        entries: list[ManifestEntry] = []

        for forecast_hour in hours:
            for variable in self.config.variables:
                filename = self.raw_filename(parsed_cycle_time, forecast_hour, variable)
                local_key = f"raw/{self.config.source_id}/{compact_cycle}/{filename}"
                entries.append(
                    ManifestEntry(
                        remote_url=self.remote_url(parsed_cycle_time, forecast_hour, variable),
                        local_key=local_key,
                        variable=variable,
                        forecast_hour=forecast_hour,
                        metadata={
                            "cycle_time": parsed_cycle_time.isoformat(),
                            "valid_time": valid_time_for(parsed_cycle_time, forecast_hour).isoformat(),
                        },
                    )
                )

        metadata = {
            "cycle_time": parsed_cycle_time.isoformat(),
            "first_forecast_hour": min(hours) if hours else None,
            "last_forecast_hour": max(hours) if hours else None,
            "variable_count": len(self.config.variables),
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
            return DownloadPlanResult(
                status="raw_complete",
                files=results,
                total_bytes_written=0,
                retry_count=0,
            )

        self._update_cycle(cycle_time=cycle_time, status="downloading", error_code="", error_message="")

        results: list[DownloadFileResult] = []
        retry_count = 0
        total_bytes_written = 0

        for entry in manifest.entries:
            try:
                if entry.local_key in already_done_by_key:
                    results.append(already_done_by_key[entry.local_key])
                    continue

                result, retries = self._download_entry(entry)
                retry_count += retries
                total_bytes_written += result.bytes_written
                results.append(result)
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

    def _download_entry(self, entry: ManifestEntry) -> tuple[DownloadFileResult, int]:
        deadline = self.clock() + self.config.max_wait_seconds
        total_retries = 0

        while True:
            try:
                payload, retries = self._download_with_retries(entry.remote_url)
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
                total_retries += max(0, error.attempts - 1)
                if self.clock() >= deadline:
                    raise PollingTimeoutError(
                        f"Timed out waiting for {entry.remote_url}",
                        attempts=total_retries,
                    ) from error
                self.sleeper(self.config.poll_interval_seconds)
            except (NetworkDownloadError, ChecksumMismatchError, FileTooLargeError):
                self._delete_partial(entry.local_key)
                raise
            except (OSError, ObjectStoreError, ValueError) as error:
                self._delete_partial(entry.local_key)
                raise GFSAdapterError(f"Failed to store {entry.local_key}: {error}", attempts=total_retries) from error

    def _download_with_retries(self, remote_url: str) -> tuple[DownloadedPayload, int]:
        last_error: Exception | None = None
        max_attempts = max(1, self.config.max_retries)
        for attempt in range(1, max_attempts + 1):
            try:
                return self._normalize_payload(self.downloader(remote_url)), attempt - 1
            except (FileUnavailableError, FileTooLargeError):
                raise
            except Exception as error:
                last_error = error
                if attempt >= max_attempts:
                    break
                backoff = self._backoff_for(attempt)
                self.sleeper(backoff)

        raise NetworkDownloadError(
            f"Failed to download {remote_url} after {max_attempts} attempts: {last_error}",
            attempts=max_attempts - 1,
        )

    def _normalize_payload(self, payload: bytes | DownloadedPayload) -> DownloadedPayload:
        if isinstance(payload, DownloadedPayload):
            return payload
        content = bytes(payload)
        return DownloadedPayload(content=content, checksum=sha256_bytes(content), bytes_written=len(content))

    def _entry_already_done(self, entry: ManifestEntry) -> bool:
        try:
            if not self.object_store.exists(entry.local_key):
                return False
            minimum_size = entry.expected_size_bytes or self.config.min_file_size_bytes
            if self.object_store.size(entry.local_key) < minimum_size:
                return False
            if entry.expected_checksum is None:
                return True
            return self.object_store.checksum(entry.local_key) == entry.expected_checksum
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to check idempotency for %s", entry.local_key)
            return False

    def _already_done_result(self, entry: ManifestEntry) -> DownloadFileResult | None:
        if not self._entry_already_done(entry):
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
                    raise FileUnavailableError(f"Remote file is unavailable: {remote_url}", attempts=1)
                if status >= 400:
                    raise NetworkDownloadError(f"HTTP {status} while downloading {remote_url}", attempts=1)
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > self.config.max_file_size_bytes:
                    raise FileTooLargeError(
                        (
                            f"Remote file {remote_url} is {content_length} bytes; "
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
                                f"Remote file {remote_url} exceeded maximum size "
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
                raise FileUnavailableError(f"Remote file is unavailable: {remote_url}", attempts=1) from error
            raise NetworkDownloadError(f"HTTP {error.code} while downloading {remote_url}: {error}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise NetworkDownloadError(f"Network error while downloading {remote_url}: {error}") from error

    def _url_exists(self, remote_url: str) -> bool:
        request = Request(remote_url, method="HEAD")
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                status = getattr(response, "status", 200)
                return 200 <= status < 400
        except HTTPError as error:
            if error.code == 404:
                return False
            LOGGER.warning("HEAD %s returned HTTP %s", remote_url, error.code)
            return False
        except (URLError, TimeoutError, OSError):
            LOGGER.warning("HEAD %s failed", remote_url, exc_info=True)
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
            error_message=error_message,
        )

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

    def remote_url(self, cycle_time: str | datetime, forecast_hour: int, variable: str) -> str:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        if variable not in NOMADS_QUERY_PARAMS:
            raise ValueError(f"Unsupported GFS variable: {variable}")

        file_name = f"gfs.t{parsed_cycle_time:%H}z.pgrb2.0p25.f{forecast_hour:03d}"
        directory = f"/gfs.{parsed_cycle_time:%Y%m%d}/{parsed_cycle_time:%H}/atmos"
        query = {
            "dir": directory,
            "file": file_name,
            **NOMADS_QUERY_PARAMS[variable],
        }
        return f"{self._filter_endpoint()}?{urlencode(query, quote_via=quote)}"

    def _filter_endpoint(self) -> str:
        parsed = urlparse(self.config.base_url)
        if "/cgi-bin/" in parsed.path:
            return self.config.base_url.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}/cgi-bin/filter_gfs_0p25.pl"
