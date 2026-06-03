from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

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
    parse_cycle_date,
    parse_cycle_time,
    valid_time_for,
    validate_forecast_hours,
)

LOGGER = logging.getLogger(__name__)

IFS_VARIABLES: tuple[str, ...] = ("2t", "2d", "10u", "10v", "tp", "sp", "ssr", "str")
IFS_FALLBACK_SOURCES: tuple[str, ...] = ("aws", "azure", "google")


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


class IFSAdapterError(RuntimeError):
    error_code = "IFS_ADAPTER_ERROR"

    def __init__(self, message: str, *, attempts: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts


class FileUnavailableError(IFSAdapterError):
    error_code = "HTTP_404"


class ForbiddenSourceError(IFSAdapterError):
    error_code = "HTTP_403"


class NetworkDownloadError(IFSAdapterError):
    error_code = "NETWORK_ERROR"


class PollingTimeoutError(IFSAdapterError):
    error_code = "POLL_TIMEOUT"


class RateLimitedError(IFSAdapterError):
    error_code = "RATE_LIMITED"

    def __init__(self, message: str, *, retry_after_seconds: float | None = None, attempts: int = 0) -> None:
        super().__init__(message, attempts=attempts)
        self.retry_after_seconds = retry_after_seconds


class ChecksumMismatchError(IFSAdapterError):
    error_code = "CHECKSUM_MISMATCH"


class FileTooLargeError(IFSAdapterError):
    error_code = "FILE_TOO_LARGE"


@dataclass(frozen=True)
class DownloadedPayload:
    content: bytes
    checksum: str
    bytes_written: int


@dataclass(frozen=True)
class IFSAdapterConfig:
    source_id: str = "IFS"
    source_name: str = "IFS Open Data"
    source_type: str = "forecast"
    status: str = "enabled"
    native_format: str = "GRIB2"
    adapter_name: str = "ifs_adapter"
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_root: Path | str = field(default_factory=lambda: os.getenv("OBJECT_STORE_ROOT", ""))
    object_store_prefix: str = field(default_factory=lambda: os.getenv("OBJECT_STORE_PREFIX", ""))
    cycle_hours_utc: tuple[int, ...] = (0, 6, 12, 18)
    forecast_start_hour: int = field(default_factory=lambda: int(os.getenv("IFS_FORECAST_START_HOUR", "0")))
    forecast_step_hours: int = 3
    preferred_source: str = field(default_factory=lambda: os.getenv("IFS_OPEN_DATA_SOURCE", "ecmwf"))
    fallback_sources: tuple[str, ...] = IFS_FALLBACK_SOURCES
    poll_interval_seconds: float = 600.0
    max_wait_seconds: float = 14400.0
    max_retries: int = 3
    retry_backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0)
    request_timeout_seconds: float = 60.0
    min_file_size_bytes: int = 1
    download_chunk_size_bytes: int = field(
        default_factory=lambda: int(os.getenv("IFS_DOWNLOAD_CHUNK_SIZE_BYTES", str(8 * 1024 * 1024)))
    )
    max_file_size_bytes: int = field(
        default_factory=lambda: int(os.getenv("IFS_MAX_FILE_SIZE_BYTES", str(500 * 1024 * 1024)))
    )
    variables: tuple[str, ...] = IFS_VARIABLES

    def __post_init__(self) -> None:
        if not str(self.object_store_root):
            object.__setattr__(self, "object_store_root", self.workspace_root)

    def forecast_end_hour_for_cycle(self, cycle_hour: int) -> int:
        if override := os.getenv("IFS_FORECAST_END_HOUR"):
            override_hour = int(override)
            if override_hour < self.forecast_start_hour:
                raise ValueError("IFS_FORECAST_END_HOUR must be >= forecast_start_hour.")
            if (override_hour - self.forecast_start_hour) % self.forecast_step_hours != 0:
                raise ValueError("IFS_FORECAST_END_HOUR must align to the IFS forecast step.")
            return override_hour
        normalized = cycle_hour % 24
        if normalized in (0, 12):
            return 168
        if normalized in (6, 18):
            return 144
        raise ValueError(f"Unsupported IFS cycle hour: {cycle_hour}")

    def forecast_hours_for_cycle(self, cycle_time: str | datetime) -> list[int]:
        parsed = parse_cycle_time(cycle_time)
        return list(
            range(
                self.forecast_start_hour,
                self.forecast_end_hour_for_cycle(parsed.hour) + 1,
                self.forecast_step_hours,
            )
        )

    def sources(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for source in (self.preferred_source, *self.fallback_sources):
            if source and source not in ordered:
                ordered.append(source)
        return tuple(ordered)

    def as_data_source_config(self) -> dict[str, Any]:
        return {
            "cycle_hours_utc": list(self.cycle_hours_utc),
            "lead_time_policy": {
                "00": 168,
                "06": 144,
                "12": 168,
                "18": 144,
                "step_hours": self.forecast_step_hours,
            },
            "variables": list(self.variables),
            "preferred_source": self.preferred_source,
            "fallback_sources": list(self.fallback_sources),
            "poll_interval_seconds": self.poll_interval_seconds,
            "max_wait_seconds": self.max_wait_seconds,
            "max_retries": self.max_retries,
            "download_chunk_size_bytes": self.download_chunk_size_bytes,
            "max_file_size_bytes": self.max_file_size_bytes,
        }


class IFSAdapter(DataSourceAdapter):
    def __init__(
        self,
        *,
        config: IFSAdapterConfig | None = None,
        repository: ForecastCycleRepository | None = None,
        object_store: LocalObjectStore | None = None,
        downloader: Callable[[str], bytes | DownloadedPayload] | None = None,
        availability_checker: Callable[[str], bool] | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or IFSAdapterConfig()
        self.repository = repository
        self.object_store = object_store or LocalObjectStore(
            self.config.object_store_root,
            object_store_prefix=self.config.object_store_prefix,
        )
        self.downloader = downloader or self._download_url
        self.availability_checker = availability_checker or self._url_exists
        self.sleeper = sleeper or time.sleep
        self.clock = clock or time.monotonic

    @classmethod
    def from_env(cls) -> IFSAdapter:
        config = IFSAdapterConfig()
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
        start_date = parse_cycle_date(cycle_date)
        stop_date = parse_cycle_date(end_date) if end_date is not None else start_date
        if stop_date < start_date:
            raise ValueError(f"end_date {stop_date.isoformat()} is before cycle_date {start_date.isoformat()}")

        discoveries: list[CycleDiscovery] = []
        current_date = start_date
        while current_date <= stop_date:
            for cycle_hour in self.config.cycle_hours_utc:
                cycle_time = datetime(
                    current_date.year,
                    current_date.month,
                    current_date.day,
                    cycle_hour,
                    tzinfo=UTC,
                )
                cycle_id = cycle_id_for(self.config.source_id, cycle_time)
                remote_url = self.remote_url(cycle_time, forecast_hour=0, variable=self.config.variables[0])
                try:
                    available = self.availability_checker(remote_url)
                    status = "discovered" if available else "unavailable"
                    reason = None if available else "source_cycle_unavailable"
                    classifier = None if available else "unavailable"
                    retryable = None if available else True
                except ForbiddenSourceError:
                    LOGGER.warning(
                        "IFS availability check was forbidden for %s",
                        self._safe_text(remote_url),
                        exc_info=True,
                    )
                    available = False
                    status = "forbidden"
                    reason = "source_cycle_forbidden"
                    classifier = "forbidden"
                    retryable = False
                except Exception:
                    LOGGER.exception("Failed to check IFS availability for %s", self._safe_text(remote_url))
                    available = False
                    status = "unavailable"
                    reason = "source_cycle_unavailable"
                    classifier = "unavailable"
                    retryable = True

                discovery = CycleDiscovery(
                    cycle_id=cycle_id,
                    source_id=self.config.source_id,
                    cycle_time=cycle_time,
                    cycle_hour=cycle_hour,
                    available=available,
                    status=status,
                    reason=reason,
                    classifier=classifier,
                    retryable=retryable,
                    probe_uri=self._safe_text(remote_url),
                    evidence={
                        "source": self.config.source_id,
                        "probe": {
                            "uri": self._safe_text(remote_url),
                            "forecast_hour": 0,
                            "variable": self.config.variables[0],
                            "preferred_source": self.config.preferred_source,
                        },
                    },
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

            current_date += timedelta(days=1)

        return discoveries

    def build_manifest(
        self,
        cycle_time: str | datetime,
        forecast_hours: list[int] | None = None,
    ) -> DownloadManifest:
        self.initialize_data_source()
        parsed_cycle_time = parse_cycle_time(cycle_time)
        compact_cycle = format_cycle_time(parsed_cycle_time)
        max_lead_hours = self.config.forecast_end_hour_for_cycle(parsed_cycle_time.hour)
        if forecast_hours is not None:
            selected_forecast_hours = forecast_hours
        else:
            selected_forecast_hours = self.config.forecast_hours_for_cycle(parsed_cycle_time)
        hours = validate_forecast_hours(
            list(selected_forecast_hours),
            source_id=self.config.source_id,
            min_hour=self.config.forecast_start_hour,
            max_hour=max_lead_hours,
            step_hours=self.config.forecast_step_hours,
        )
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
                            "source": self.config.preferred_source,
                            "model": "ifs",
                            "type": "fc",
                            "levtype": "sfc",
                        },
                    )
                )

        metadata = {
            "cycle_time": parsed_cycle_time.isoformat(),
            "first_forecast_hour": min(hours) if hours else None,
            "last_forecast_hour": max(hours) if hours else None,
            "forecast_hours": list(hours),
            "max_lead_hours": max_lead_hours,
            "source_policy": self.source_policy_identity(parsed_cycle_time, hours),
            "source_object_identity": self.source_object_identity(parsed_cycle_time, hours),
            "variable_count": len(self.config.variables),
            "total_file_count": len(entries),
            "preferred_source": self.config.preferred_source,
            "fallback_sources": list(self.config.fallback_sources),
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
            LOGGER.exception("Failed to persist IFS manifest for %s", compact_cycle)
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
                LOGGER.exception("Failed to update manifest_uri for IFS cycle %s", compact_cycle)
                raise

        return manifest

    def load_manifest(self, manifest_uri: str) -> DownloadManifest:
        try:
            payload = json.loads(self.object_store.read_bytes(manifest_uri).decode("utf-8"))
        except (json.JSONDecodeError, OSError, ObjectStoreError, ValueError) as error:
            raise IFSAdapterError(f"Failed to load manifest {manifest_uri}: {error}") from error
        return DownloadManifest.from_dict(payload)

    def download_plan(self, manifest: DownloadManifest) -> DownloadPlanResult:
        cycle_time = manifest.cycle_time
        existing_cycle = self._get_cycle(cycle_time)
        trusted_source_object_identity = self._trusted_prior_source_object_identity(manifest, existing_cycle)
        already_done_by_key: dict[str, DownloadFileResult] = {}
        needs_download = False

        for entry in manifest.entries:
            try:
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
            except IFSAdapterError as error:
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
                LOGGER.exception("Unexpected IFS download failure for %s", entry.local_key)
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
                if size == 0:
                    failures.append(
                        VerificationFailure(
                            local_key=entry.local_key,
                            error_code="EMPTY_FILE",
                            error_message=f"{entry.local_key} is an empty GRIB2 file",
                        )
                    )
                    continue

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

                if self.object_store.read_bytes(entry.local_key)[:4] != b"GRIB":
                    failures.append(
                        VerificationFailure(
                            local_key=entry.local_key,
                            error_code="INVALID_GRIB",
                            error_message=f"{entry.local_key} does not start with a valid GRIB message",
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
            return VerificationResult(status="failed", failures=tuple(failures))

        self._update_cycle(cycle_time=manifest.cycle_time, status="raw_complete")
        return VerificationResult(status="passed")

    def _download_entry(self, entry: ManifestEntry) -> tuple[DownloadFileResult, int]:
        sources = self.config.sources()
        total_retries = 0
        waited_seconds = 0.0
        first_start = self.clock()
        last_network_error: NetworkDownloadError | None = None
        rate_limited_sources: set[str] = set()
        tried_sources: list[str] = []

        while True:
            saw_unavailable = False
            saw_rate_limit = False

            for source in sources:
                if source not in tried_sources:
                    tried_sources.append(source)
                source_url = self._remote_url_for_source(entry, source)
                try:
                    payload, retries = self._download_with_retries(source_url)
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
                    saw_unavailable = True
                    continue
                except ForbiddenSourceError:
                    self._delete_partial(entry.local_key)
                    raise
                except RateLimitedError as error:
                    total_retries += error.attempts
                    saw_rate_limit = True
                    rate_limited_sources.add(source)
                    LOGGER.warning("IFS source %s rate limited for %s", source, entry.local_key)
                    requested_wait = (
                        error.retry_after_seconds
                        if error.retry_after_seconds is not None
                        else self.config.poll_interval_seconds
                    )
                    wait_seconds = self._bounded_wait(requested_wait, waited_seconds, first_start)
                    if wait_seconds <= 0:
                        raise RateLimitedError(
                            (
                                f"IFS download rate limited across sources {', '.join(tried_sources)} "
                                f"for {entry.local_key}"
                            ),
                            attempts=total_retries,
                        ) from error
                    self.sleeper(wait_seconds)
                    waited_seconds += wait_seconds
                    continue
                except NetworkDownloadError as error:
                    total_retries += error.attempts
                    last_network_error = error
                    LOGGER.warning("IFS source %s failed for %s; trying next mirror", source, entry.local_key)
                    continue
                except (ChecksumMismatchError, FileTooLargeError):
                    self._delete_partial(entry.local_key)
                    raise
                except (OSError, ObjectStoreError, ValueError) as error:
                    self._delete_partial(entry.local_key)
                    raise IFSAdapterError(
                        f"Failed to store {entry.local_key}: {error}",
                        attempts=total_retries,
                    ) from error

            if saw_rate_limit and len(rate_limited_sources) == len(sources):
                wait_seconds = self._bounded_wait(self.config.poll_interval_seconds, waited_seconds, first_start)
                if wait_seconds <= 0:
                    raise RateLimitedError(
                        (f"IFS download rate limited across sources {', '.join(tried_sources)} for {entry.local_key}"),
                        attempts=total_retries,
                    )
                self.sleeper(wait_seconds)
                waited_seconds += wait_seconds
                continue

            if saw_unavailable:
                wait_seconds = self._bounded_wait(self.config.poll_interval_seconds, waited_seconds, first_start)
                if wait_seconds <= 0:
                    raise PollingTimeoutError(
                        f"Timed out waiting for {entry.local_key} from IFS sources {', '.join(tried_sources)}",
                        attempts=total_retries,
                    )
                self.sleeper(wait_seconds)
                waited_seconds += wait_seconds
                continue

            if last_network_error is not None:
                raise NetworkDownloadError(
                    (
                        f"Failed to download {entry.local_key} from IFS sources "
                        f"{', '.join(tried_sources)}: {last_network_error}"
                    ),
                    attempts=total_retries,
                ) from last_network_error

            raise IFSAdapterError(f"Failed to download {entry.local_key}", attempts=total_retries)

    def _bounded_wait(self, requested_seconds: float, waited_seconds: float, start_time: float) -> float:
        elapsed_clock = max(0.0, self.clock() - start_time)
        elapsed = max(waited_seconds, elapsed_clock)
        remaining = self.config.max_wait_seconds - elapsed
        if remaining <= 0:
            return 0.0
        return max(0.0, min(float(requested_seconds), remaining))

    def _remote_url_for_source(self, entry: ManifestEntry, source: str) -> str:
        parsed = urlparse(entry.remote_url)
        if parsed.scheme == "ecmwf-opendata":
            return parsed._replace(netloc=source).geturl()
        cycle_time = entry.metadata.get("cycle_time")
        if cycle_time is not None:
            return self.remote_url(cycle_time, entry.forecast_hour, entry.variable, source=source)
        return entry.remote_url

    def _download_with_retries(self, remote_url: str) -> tuple[DownloadedPayload, int]:
        last_error: Exception | None = None
        max_attempts = 1 + max(0, self.config.max_retries)
        for attempt in range(1, max_attempts + 1):
            try:
                return self._normalize_payload(self.downloader(remote_url)), attempt - 1
            except (FileUnavailableError, ForbiddenSourceError, RateLimitedError, FileTooLargeError):
                raise
            except HTTPError as error:
                if error.code == 404:
                    raise FileUnavailableError(
                        f"Remote IFS file is unavailable: {self._safe_text(remote_url)}",
                        attempts=attempt,
                    ) from error
                if error.code == 403:
                    raise ForbiddenSourceError(
                        f"Remote IFS file is forbidden: {self._safe_text(remote_url)}",
                        attempts=attempt,
                    ) from error
                if error.code == 429:
                    raise RateLimitedError(
                        f"IFS source rate limited while downloading {self._safe_text(remote_url)}",
                        retry_after_seconds=_retry_after_seconds(error.headers.get("Retry-After")),
                        attempts=attempt - 1,
                    ) from error
                last_error = error
            except Exception as error:
                last_error = error

            if attempt >= max_attempts:
                break
            self.sleeper(self._backoff_for(attempt))

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
                    f"Downloaded IFS payload exceeds maximum size {self.config.max_file_size_bytes} bytes"
                )
            return payload
        content = bytes(payload)
        if len(content) > self.config.max_file_size_bytes:
            raise FileTooLargeError(
                f"Downloaded IFS payload exceeds maximum size {self.config.max_file_size_bytes} bytes"
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
            size_bytes = self.object_store.size(entry.local_key)
            if size_bytes == 0:
                return False
            minimum_size = entry.expected_size_bytes or self.config.min_file_size_bytes
            if size_bytes < minimum_size:
                return False
            if size_bytes > self.config.max_file_size_bytes:
                raise FileTooLargeError(
                    f"Existing IFS raw object {entry.local_key} exceeds maximum size "
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
            LOGGER.exception("Failed to check IFS idempotency for %s", entry.local_key)
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
            LOGGER.exception("Failed to checksum existing IFS raw object %s", entry.local_key)
            return None
        return DownloadFileResult(local_key=entry.local_key, status="already_done", checksum=checksum)

    def _download_url(self, remote_url: str) -> DownloadedPayload:
        parsed = _parse_ifs_url(remote_url)
        with tempfile.NamedTemporaryFile(suffix=".grib2") as target:
            try:
                client = self._client_for_source(parsed["source"])
                client.retrieve(
                    date=parsed["date"],
                    time=parsed["time"],
                    step=parsed["step"],
                    type="fc",
                    param=parsed["variable"],
                    target=target.name,
                )
                payload_size = Path(target.name).stat().st_size
                if payload_size > self.config.max_file_size_bytes:
                    raise FileTooLargeError(
                        f"Remote IFS file {self._safe_text(remote_url)} exceeded maximum size "
                        f"{self.config.max_file_size_bytes} bytes"
                    )
                payload = Path(target.name).read_bytes()
            except FileTooLargeError:
                raise
            except HTTPError as error:
                if error.code == 404:
                    raise FileUnavailableError(
                        f"Remote IFS file is unavailable: {self._safe_text(remote_url)}",
                        attempts=1,
                    ) from error
                if error.code == 403:
                    raise ForbiddenSourceError(
                        f"Remote IFS file is forbidden: {self._safe_text(remote_url)}",
                        attempts=1,
                    ) from error
                if error.code == 429:
                    raise RateLimitedError(
                        f"IFS source rate limited while downloading {self._safe_text(remote_url)}",
                        retry_after_seconds=_retry_after_seconds(error.headers.get("Retry-After")),
                        attempts=0,
                    ) from error
                raise NetworkDownloadError(
                    f"HTTP {error.code} while downloading {self._safe_text(remote_url)}: {self._safe_text(str(error))}"
                ) from error
            except (URLError, TimeoutError, OSError) as error:
                raise NetworkDownloadError(
                    f"Network error while downloading {self._safe_text(remote_url)}: {self._safe_text(str(error))}"
                ) from error
            except Exception as error:
                raise NetworkDownloadError(
                    f"Failed to retrieve IFS Open Data {self._safe_text(remote_url)}: {self._safe_text(str(error))}"
                ) from error

        if len(payload) > self.config.max_file_size_bytes:
            raise FileTooLargeError(
                f"Remote IFS file {self._safe_text(remote_url)} exceeded maximum size "
                f"{self.config.max_file_size_bytes} bytes"
            )
        checksum = hashlib.sha256(payload).hexdigest()
        return DownloadedPayload(content=payload, checksum=checksum, bytes_written=len(payload))

    def _url_exists(self, remote_url: str) -> bool:
        try:
            self._download_url(remote_url)
            return True
        except FileUnavailableError:
            return False
        except RateLimitedError:
            LOGGER.warning("IFS availability check was rate limited for %s", self._safe_text(remote_url))
            return False
        except ForbiddenSourceError:
            raise
        except IFSAdapterError:
            LOGGER.warning("IFS availability check failed for %s", self._safe_text(remote_url), exc_info=True)
            return False

    def _client_for_source(self, source: str) -> Any:
        try:
            from ecmwf.opendata import Client
        except ImportError as error:
            raise IFSAdapterError("ecmwf-opendata is required for real IFS downloads.") from error
        return Client(source=source)

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
        if manifest.manifest_uri is None:
            return
        try:
            self.object_store.write_bytes_atomic(
                manifest.manifest_uri,
                json.dumps(manifest.as_dict(), indent=2, sort_keys=True).encode("utf-8"),
            )
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.exception("Failed to persist refreshed IFS source object identity for %s", manifest.manifest_uri)
            raise

    def _manifest_forecast_hours(self, manifest: DownloadManifest) -> list[int]:
        forecast_hours = manifest.metadata.get("forecast_hours")
        if isinstance(forecast_hours, list):
            return [int(forecast_hour) for forecast_hour in forecast_hours]
        return sorted({int(entry.forecast_hour) for entry in manifest.entries})

    def source_policy_identity(
        self,
        cycle_time: str | datetime,
        forecast_hours: list[int] | None = None,
    ) -> dict[str, Any]:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        hours = list(
            forecast_hours if forecast_hours is not None else self.config.forecast_hours_for_cycle(parsed_cycle_time)
        )
        return {
            "source": self.config.source_id,
            "cycle_hours_utc": list(self.config.cycle_hours_utc),
            "forecast_start_hour": self.config.forecast_start_hour,
            "forecast_end_hour": self.config.forecast_end_hour_for_cycle(parsed_cycle_time.hour),
            "forecast_step_hours": self.config.forecast_step_hours,
            "forecast_hours": hours,
            "variables": list(self.config.variables),
            "preferred_source": self.config.preferred_source,
            "fallback_sources": list(self.config.fallback_sources),
            "max_retries": self.config.max_retries,
            "max_wait_seconds": self.config.max_wait_seconds,
        }

    def source_object_identity(
        self,
        cycle_time: str | datetime,
        forecast_hours: list[int] | None = None,
    ) -> dict[str, Any]:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        hours = list(
            forecast_hours if forecast_hours is not None else self.config.forecast_hours_for_cycle(parsed_cycle_time)
        )
        entries = self._source_object_entry_identities(parsed_cycle_time, hours)
        entry_digest = _stable_digest(entries)
        return {
            "identity_schema_version": "nhms.source_object_identity.v2",
            "source": self.config.source_id,
            "cycle_time": parsed_cycle_time.isoformat(),
            "preferred_source": self.config.preferred_source,
            "first_forecast_hour": min(hours) if hours else None,
            "last_forecast_hour": max(hours) if hours else None,
            "forecast_hour_count": len(hours),
            "variable_count": len(self.config.variables),
            "manifest_object_key": f"raw/{self.config.source_id}/{format_cycle_time(parsed_cycle_time)}/manifest.json",
            "manifest_digest": _stable_digest(
                {
                    "source_id": self.config.source_id,
                    "cycle_time": parsed_cycle_time.isoformat(),
                    "entries": entries,
                    "source_policy": self.source_policy_identity(parsed_cycle_time, hours),
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
            for variable in self.config.variables:
                filename = self.raw_filename(cycle_time, forecast_hour, variable)
                remote_identities = [
                    self._safe_text(self.remote_url(cycle_time, forecast_hour, variable, source=source))
                    for source in self.config.sources()
                ]
                local_key = f"raw/{self.config.source_id}/{compact_cycle}/{filename}"
                entries.append(
                    {
                        "local_key": local_key,
                        "remote_identity": remote_identities[0] if remote_identities else None,
                        "remote_identity_fallbacks": remote_identities,
                        "variable": variable,
                        "forecast_hour": forecast_hour,
                        "expected_checksum": None,
                        "expected_size_bytes": None,
                        "observed_raw_object": self._raw_object_observation(local_key),
                    }
                )
        return entries

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
        except IFSAdapterError:
            LOGGER.warning("Raw-complete IFS cycle has unreadable prior manifest %s", manifest_uri, exc_info=True)
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
            LOGGER.exception("Failed to read IFS forecast cycle %s", format_cycle_time(cycle_time))
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
            LOGGER.exception("Failed to update IFS forecast cycle %s", format_cycle_time(cycle_time))
            raise

    def _delete_partial(self, local_key: str) -> None:
        try:
            self.object_store.delete(local_key)
        except (OSError, ObjectStoreError, ValueError):
            LOGGER.warning("Failed to clean partial IFS raw object %s", local_key, exc_info=True)

    def _backoff_for(self, attempt: int) -> float:
        index = max(0, min(attempt - 1, len(self.config.retry_backoff_seconds) - 1))
        return self.config.retry_backoff_seconds[index]

    def raw_filename(self, cycle_time: str | datetime, forecast_hour: int, variable: str) -> str:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        return f"ifs.t{parsed_cycle_time:%H}z.f{forecast_hour:03d}.{variable}.grib2"

    def remote_url(
        self,
        cycle_time: str | datetime,
        forecast_hour: int,
        variable: str,
        *,
        source: str | None = None,
    ) -> str:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        if variable not in self.config.variables:
            raise ValueError(f"Unsupported IFS variable: {variable}")
        selected_source = source or self.config.preferred_source
        return (
            f"ecmwf-opendata://{selected_source}/ifs/{parsed_cycle_time:%Y%m%d%H}/"
            f"ifs.t{parsed_cycle_time:%H}z.f{forecast_hour:03d}.{variable}.grib2"
        )


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


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


def _parse_ifs_url(remote_url: str) -> dict[str, Any]:
    parsed = urlparse(remote_url)
    if parsed.scheme != "ecmwf-opendata":
        raise IFSAdapterError(f"Unsupported IFS remote URL: {remote_url}")
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 3:
        raise IFSAdapterError(f"Malformed IFS remote URL: {remote_url}")
    compact_cycle = path_parts[1]
    filename = path_parts[2]
    variable = filename.rsplit(".", maxsplit=2)[-2]
    forecast_hour = int(filename.split(".f", maxsplit=1)[1].split(".", maxsplit=1)[0])
    return {
        "source": parsed.netloc,
        "date": compact_cycle[:8],
        "time": int(compact_cycle[8:10]),
        "step": forecast_hour,
        "variable": variable,
    }


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
