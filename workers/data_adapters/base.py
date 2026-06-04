from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from packages.common.source_identity import normalize_source_id


def ensure_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_cycle_time(value: str | datetime) -> datetime:
    """Parse YYYYMMDDHH or ISO-8601 cycle time values as UTC datetimes."""
    if isinstance(value, datetime):
        return ensure_utc(value)

    candidate = value.strip()
    if len(candidate) == 10 and candidate.isdigit():
        return datetime.strptime(candidate, "%Y%m%d%H").replace(tzinfo=UTC)

    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    return ensure_utc(datetime.fromisoformat(candidate))


def parse_cycle_date(value: str | date | datetime) -> date:
    """Parse a date input accepted by cycle discovery."""
    if isinstance(value, datetime):
        return ensure_utc(value).date()
    if isinstance(value, date):
        return value
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def format_cycle_time(value: str | datetime) -> str:
    return parse_cycle_time(value).strftime("%Y%m%d%H")


def cycle_id_for(source_id: str, cycle_time: str | datetime) -> str:
    source_id = normalize_source_id(source_id)
    return f"{source_id.lower()}_{format_cycle_time(cycle_time)}"


def valid_time_for(cycle_time: str | datetime, forecast_hour: int) -> datetime:
    return parse_cycle_time(cycle_time) + timedelta(hours=forecast_hour)


def parse_resolution_segments(spec: str | None) -> tuple[tuple[int, int], ...] | None:
    """Parse a piecewise forecast-resolution spec like "120:1,384:3".

    Each segment is ``upto_hour:step_hours`` (ascending upto), meaning forecast hours
    up to and including ``upto_hour`` use ``step_hours``. Segments may be separated by
    ``,`` or ``;`` (the latter survives Slurm env-export value filtering). Returns None
    for an empty spec so callers fall back to a single uniform step.
    """
    if not spec or not spec.strip():
        return None
    segments: list[tuple[int, int]] = []
    last_upto = -1
    for raw in re.split(r"[,;]", spec):
        token = raw.strip()
        if not token:
            continue
        try:
            upto_text, step_text = token.split(":")
            upto, step = int(upto_text), int(step_text)
        except ValueError as error:
            raise ValueError(f"Invalid forecast-resolution segment {token!r}; expected 'upto:step'.") from error
        if step <= 0:
            raise ValueError(f"Forecast-resolution step must be positive in segment {token!r}.")
        if upto <= last_upto:
            raise ValueError(f"Forecast-resolution segments must have strictly ascending upto: {token!r}.")
        segments.append((upto, step))
        last_upto = upto
    return tuple(segments) or None


def generate_segmented_forecast_hours(
    start_hour: int,
    end_hour: int,
    segments: tuple[tuple[int, int], ...],
) -> list[int]:
    """Generate non-uniform forecast hours from piecewise (upto, step) segments.

    Each segment emits hours on its own step grid within ``(previous_upto, upto]``,
    so e.g. GFS ``((120, 1), (384, 3))`` yields hourly 0..120 then 3-hourly 123..end.
    """
    hours: list[int] = []
    seen: set[int] = set()
    lower = start_hour
    for upto, step in segments:
        seg_hi = min(end_hour, upto)
        remainder = lower % step
        hour = lower if remainder == 0 else lower + (step - remainder)
        while hour <= seg_hi:
            if hour not in seen:
                seen.add(hour)
                hours.append(hour)
            hour += step
        lower = upto + 1
        if lower > end_hour:
            break
    return hours


def validate_forecast_hours(
    forecast_hours: list[int],
    *,
    source_id: str,
    min_hour: int,
    max_hour: int,
    step_hours: int,
    allowed_hours: set[int] | None = None,
) -> list[int]:
    """Validate caller-supplied lead hours before manifest path generation.

    When ``allowed_hours`` is provided each hour must be a member of that canonical
    set (supporting non-uniform native resolution); otherwise hours must align to a
    single uniform ``step_hours`` grid.
    """
    if step_hours <= 0:
        raise ValueError(f"{source_id} forecast-hour step must be positive.")

    normalized: list[int] = []
    seen: set[int] = set()
    for forecast_hour in forecast_hours:
        if not isinstance(forecast_hour, int) or isinstance(forecast_hour, bool):
            raise ValueError(f"{source_id} forecast hour must be an integer: {forecast_hour!r}")
        if forecast_hour in seen:
            raise ValueError(f"{source_id} forecast hours must be unique: {forecast_hour}")
        if forecast_hour < min_hour:
            raise ValueError(f"{source_id} forecast hour {forecast_hour} is below minimum {min_hour}.")
        if forecast_hour > max_hour:
            raise ValueError(f"{source_id} forecast hour {forecast_hour} exceeds maximum {max_hour}.")
        if allowed_hours is not None:
            if forecast_hour not in allowed_hours:
                raise ValueError(
                    f"{source_id} forecast hour {forecast_hour} is not in the native resolution schedule."
                )
        elif (forecast_hour - min_hour) % step_hours != 0:
            raise ValueError(f"{source_id} forecast hour {forecast_hour} is not aligned to {step_hours}h steps.")
        seen.add(forecast_hour)
        normalized.append(forecast_hour)
    return normalized


@dataclass(frozen=True)
class CycleDiscovery:
    cycle_id: str
    source_id: str
    cycle_time: datetime
    cycle_hour: int
    available: bool
    status: str | None = None
    reason: str | None = None
    classifier: str | None = None
    retryable: bool | None = None
    probe_uri: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "source_id": self.source_id,
            "cycle_time": self.cycle_time.isoformat(),
            "cycle_hour": self.cycle_hour,
            "available": self.available,
            "status": self.status,
            "reason": self.reason,
            "classifier": self.classifier,
            "retryable": self.retryable,
            "probe_uri": self.probe_uri,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ManifestEntry:
    remote_url: str
    local_key: str
    variable: str
    forecast_hour: int
    expected_checksum: str | None = None
    expected_size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "remote_url": self.remote_url,
            "local_key": self.local_key,
            "variable": self.variable,
            "forecast_hour": self.forecast_hour,
            "expected_checksum": self.expected_checksum,
            "expected_size_bytes": self.expected_size_bytes,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ManifestEntry:
        return cls(
            remote_url=value["remote_url"],
            local_key=value["local_key"],
            variable=value["variable"],
            forecast_hour=int(value["forecast_hour"]),
            expected_checksum=value.get("expected_checksum"),
            expected_size_bytes=value.get("expected_size_bytes"),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class DownloadManifest:
    source_id: str
    cycle_time: datetime
    entries: tuple[ManifestEntry, ...]
    manifest_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "cycle_time": self.cycle_time.isoformat(),
            "manifest_uri": self.manifest_uri,
            "metadata": self.metadata,
            "entries": [entry.as_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DownloadManifest:
        return cls(
            source_id=value["source_id"],
            cycle_time=parse_cycle_time(value["cycle_time"]),
            manifest_uri=value.get("manifest_uri"),
            metadata=dict(value.get("metadata") or {}),
            entries=tuple(ManifestEntry.from_dict(entry) for entry in value["entries"]),
        )


@dataclass(frozen=True)
class DownloadFileResult:
    local_key: str
    status: str
    checksum: str | None = None
    bytes_written: int = 0
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class DownloadPlanResult:
    status: str
    files: tuple[DownloadFileResult, ...]
    total_bytes_written: int
    retry_count: int = 0


@dataclass(frozen=True)
class VerificationFailure:
    local_key: str
    error_code: str
    error_message: str


@dataclass(frozen=True)
class VerificationResult:
    status: str
    failures: tuple[VerificationFailure, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "passed"


class DataSourceAdapter(ABC):
    @abstractmethod
    def discover_cycles(
        self,
        cycle_date: str | date | datetime,
        end_date: str | date | datetime | None = None,
    ) -> list[CycleDiscovery]:
        """Discover available cycles for a source on a UTC date or date range."""

    @abstractmethod
    def build_manifest(
        self,
        cycle_time: str | datetime,
        forecast_hours: list[int] | None = None,
    ) -> DownloadManifest:
        """Build and persist a raw download manifest."""

    @abstractmethod
    def download_plan(self, manifest: DownloadManifest) -> DownloadPlanResult:
        """Download all files described by a manifest."""

    @abstractmethod
    def verify_manifest(self, manifest: DownloadManifest) -> VerificationResult:
        """Verify downloaded files against manifest expectations."""
