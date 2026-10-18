from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from packages.common.contracts.time_normalization import (
    cycle_id_for,
    ensure_utc,
    format_cycle_time,
    generate_segmented_forecast_hours,
    parse_cycle_date,
    parse_cycle_time,
    parse_resolution_segments,
    valid_time_for,
    validate_forecast_hours,
)

__all__ = [
    "CycleDiscovery",
    "ManifestEntry",
    "DownloadManifest",
    "DownloadFileResult",
    "DownloadPlanResult",
    "VerificationFailure",
    "VerificationResult",
    "DataSourceAdapter",
    "ensure_utc",
    "parse_cycle_time",
    "parse_cycle_date",
    "format_cycle_time",
    "cycle_id_for",
    "valid_time_for",
    "parse_resolution_segments",
    "generate_segmented_forecast_hours",
    "validate_forecast_hours",
]


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
