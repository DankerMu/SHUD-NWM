from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence
from urllib.parse import urlparse


@dataclass(frozen=True)
class ObjectPathValidation:
    """Result returned by object storage path validation."""

    valid: bool
    category: str | None
    components: dict[str, str]
    error: str | None = None


@dataclass(frozen=True)
class ObjectPrefixPattern:
    """Configured object storage prefix pattern."""

    display: str
    category: str
    segments: tuple[str, ...]
    captured_literals: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ArchiveProvenancePaths:
    """Deterministic archive object and manifest paths for one identity."""

    archive: Path
    manifest: Path


@dataclass(frozen=True)
class ArchiveStorageConfig:
    """Validated archive configuration shared by node-27 maintenance tools."""

    archive_root: Path
    archive_min_age_days: int
    retention_days: int


class ArchiveConfigurationError(ValueError):
    """Raised when archive configuration is unsafe or incomplete."""


ARCHIVE_LANES = frozenset({"forcing", "runs", "states"})
DEFAULT_ARCHIVE_MIN_AGE_DAYS = 45
DEFAULT_DB_RETENTION_DAYS = 30


VALID_PREFIX_PATTERNS: tuple[ObjectPrefixPattern, ...] = (
    ObjectPrefixPattern("raw/{source}/{cycle_time}/...", "raw", ("raw", "{source}", "{cycle_time}")),
    ObjectPrefixPattern(
        "canonical/{source}/{cycle_time}/{variable}/...",
        "canonical",
        ("canonical", "{source}", "{cycle_time}", "{variable}"),
    ),
    ObjectPrefixPattern(
        "forcing/{source}/{cycle_time}/{basin_version_id}/{model_id}/...",
        "forcing",
        ("forcing", "{source}", "{cycle_time}", "{basin_version_id}", "{model_id}"),
    ),
    ObjectPrefixPattern("models/{model_id}/...", "models", ("models", "{model_id}")),
    ObjectPrefixPattern("states/{model_id}/{valid_time}/...", "states", ("states", "{model_id}", "{valid_time}")),
    ObjectPrefixPattern(
        "runs/{run_id}/input/...",
        "runs",
        ("runs", "{run_id}", "input"),
        captured_literals={2: "sub_prefix"},
    ),
    ObjectPrefixPattern(
        "runs/{run_id}/output/...",
        "runs",
        ("runs", "{run_id}", "output"),
        captured_literals={2: "sub_prefix"},
    ),
    ObjectPrefixPattern(
        "runs/{run_id}/logs/...",
        "runs",
        ("runs", "{run_id}", "logs"),
        captured_literals={2: "sub_prefix"},
    ),
    ObjectPrefixPattern(
        "tiles/hydro/{run_id}/...",
        "tiles",
        ("tiles", "hydro", "{run_id}"),
        captured_literals={1: "tile_type"},
    ),
)


VALID_PREFIX_MESSAGE = ", ".join(pattern.display for pattern in VALID_PREFIX_PATTERNS)


def resolve_archive_root(
    script_name: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the shared archive root, honoring a per-script override."""
    source_env = os.environ if env is None else env
    override_name = _archive_override_name(script_name) if script_name else None
    value = source_env.get(override_name, "") if override_name else ""
    if not value.strip():
        value = source_env.get("NHMS_ARCHIVE_ROOT", "")
    if not value.strip():
        expected = f" or {override_name}" if override_name else ""
        raise ArchiveConfigurationError(f"NHMS_ARCHIVE_ROOT{expected} must be set")
    return _normalized_filesystem_path(value, label="archive root")


def resolve_archive_storage_config(
    *,
    cleanup_roots: Mapping[str, str | os.PathLike[str]],
    script_name: str | None = None,
    env: Mapping[str, str] | None = None,
    retention_days: int = DEFAULT_DB_RETENTION_DAYS,
) -> ArchiveStorageConfig:
    """Resolve and validate archive root, age, and every cleanup target root."""
    source_env = os.environ if env is None else env
    archive_root = resolve_archive_root(script_name, env=source_env)
    raw_age = source_env.get("NHMS_ARCHIVE_MIN_AGE_DAYS", str(DEFAULT_ARCHIVE_MIN_AGE_DAYS)).strip()
    try:
        archive_min_age_days = int(raw_age)
    except ValueError as error:
        raise ArchiveConfigurationError("NHMS_ARCHIVE_MIN_AGE_DAYS must be an integer") from error
    return validate_archive_configuration(
        archive_root=archive_root,
        cleanup_roots=cleanup_roots,
        archive_min_age_days=archive_min_age_days,
        retention_days=retention_days,
    )


def validate_archive_configuration(
    *,
    archive_root: str | os.PathLike[str],
    cleanup_roots: Mapping[str, str | os.PathLike[str]],
    archive_min_age_days: int = DEFAULT_ARCHIVE_MIN_AGE_DAYS,
    retention_days: int = DEFAULT_DB_RETENTION_DAYS,
) -> ArchiveStorageConfig:
    """Reject unsafe root overlap and archive ages shorter than DB retention."""
    if not cleanup_roots:
        raise ArchiveConfigurationError("cleanup_roots must explicitly contain every cleanup target root")
    if retention_days <= 0:
        raise ArchiveConfigurationError("retention_days must be positive")
    if archive_min_age_days < retention_days:
        raise ArchiveConfigurationError(
            f"archive minimum age {archive_min_age_days} days is below DB retention {retention_days} days"
        )

    normalized_archive = _normalized_filesystem_path(archive_root, label="archive root")
    for name, cleanup_root in cleanup_roots.items():
        if not name.strip():
            raise ArchiveConfigurationError("cleanup root name must be non-empty")
        normalized_cleanup = _normalized_filesystem_path(cleanup_root, label=f"cleanup root {name}")
        if _paths_overlap(normalized_archive, normalized_cleanup):
            raise ArchiveConfigurationError(
                "archive root overlaps cleanup root "
                f"{name}: archive_root={normalized_archive}; cleanup_root={normalized_cleanup}"
            )

    return ArchiveStorageConfig(
        archive_root=normalized_archive,
        archive_min_age_days=archive_min_age_days,
        retention_days=retention_days,
    )


def archive_provenance_paths(
    archive_root: str | os.PathLike[str],
    *,
    lane: str,
    cycle_identity: str,
    scope_components: Sequence[str] = (),
) -> ArchiveProvenancePaths:
    """Map a safe archive identity to its tarball and sibling manifest."""
    if lane not in ARCHIVE_LANES:
        raise ArchiveConfigurationError(f"archive lane must be one of {sorted(ARCHIVE_LANES)}: {lane!r}")
    components = (cycle_identity, *scope_components)
    for component in components:
        _validate_identity_component(component)
    root = _normalized_filesystem_path(archive_root, label="archive root")
    parent = root.joinpath(lane, *components)
    return ArchiveProvenancePaths(
        archive=parent / "archive.tar.zst",
        manifest=parent / "manifest.json",
    )


def _archive_override_name(script_name: str) -> str:
    normalized = script_name.strip().upper().replace("-", "_")
    if not normalized or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in normalized):
        raise ArchiveConfigurationError(f"invalid archive script name: {script_name!r}")
    return f"NODE27_{normalized}_ARCHIVE_ROOT"


def _normalized_filesystem_path(path: str | os.PathLike[str], *, label: str) -> Path:
    raw = os.fspath(path).strip()
    if not raw:
        raise ArchiveConfigurationError(f"{label} must be non-empty")
    return Path(raw).expanduser().resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_identity_component(component: str) -> None:
    if not isinstance(component, str):
        raise ArchiveConfigurationError("archive identity components must be strings")
    if component != component.strip() or component in {"", ".", ".."}:
        raise ArchiveConfigurationError(f"unsafe archive identity component: {component!r}")
    if Path(component).is_absolute() or "/" in component or "\\" in component:
        raise ArchiveConfigurationError(f"unsafe archive identity component: {component!r}")


def validate_object_path(path: str) -> ObjectPathValidation:
    """Validate an S3 object key or URI against the NHMS storage layout."""
    normalized_path = _normalize_object_path(path)
    if not normalized_path:
        return _invalid("Object path is empty.")

    parts = normalized_path.split("/")
    if any(part == "" for part in parts):
        return _invalid("Object path contains an empty path segment.")

    for pattern in VALID_PREFIX_PATTERNS:
        components = _match_pattern(parts, pattern)
        if components is not None:
            return ObjectPathValidation(
                valid=True,
                category=pattern.category,
                components=components,
                error=None,
            )

    return _invalid("Unrecognized object path prefix.")


def _normalize_object_path(path: str) -> str:
    candidate = path.strip()
    if candidate.startswith("s3://"):
        parsed = urlparse(candidate)
        candidate = parsed.path
    return candidate.strip("/")


def _match_pattern(parts: list[str], pattern: ObjectPrefixPattern) -> dict[str, str] | None:
    if len(parts) <= len(pattern.segments):
        return None

    components: dict[str, str] = {}
    captured_literals = MappingProxyType(pattern.captured_literals)
    for index, expected_segment in enumerate(pattern.segments):
        actual_segment = parts[index]
        if _is_variable_segment(expected_segment):
            components[expected_segment[1:-1]] = actual_segment
            continue

        if actual_segment != expected_segment:
            return None

        if index in captured_literals:
            components[captured_literals[index]] = actual_segment

    return components


def _is_variable_segment(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _invalid(message: str) -> ObjectPathValidation:
    return ObjectPathValidation(
        valid=False,
        category=None,
        components={},
        error=f"{message} Valid prefixes: {VALID_PREFIX_MESSAGE}",
    )
