from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any


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
    return f"{source_id.lower()}_{format_cycle_time(cycle_time)}"


def valid_time_for(cycle_time: str | datetime, forecast_hour: int) -> datetime:
    return parse_cycle_time(cycle_time) + timedelta(hours=forecast_hour)


@dataclass(frozen=True)
class CycleDiscovery:
    cycle_id: str
    source_id: str
    cycle_time: datetime
    cycle_hour: int
    available: bool
    status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "source_id": self.source_id,
            "cycle_time": self.cycle_time.isoformat(),
            "cycle_hour": self.cycle_hour,
            "available": self.available,
            "status": self.status,
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
    def discover_cycles(self, cycle_date: str | date | datetime) -> list[CycleDiscovery]:
        """Discover available cycles for a source on a UTC date."""

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
