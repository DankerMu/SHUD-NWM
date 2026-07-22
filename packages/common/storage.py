from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlparse

from packages.common.source_identity import normalize_source_id


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
class ArchiveIdentity:
    """Canonical source-qualified identity for one archived product."""

    lane: str
    source: str
    cycle_identity: str
    cycle_time: str
    basin_version_id: str | None = None
    model_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identity_component(self.source)
        if self.source == LEGACY_UNQUALIFIED_ARCHIVE_SOURCE:
            if self.lane != "states":
                raise ArchiveConfigurationError(
                    f"archive source {LEGACY_UNQUALIFIED_ARCHIVE_SOURCE!r} is reserved for the states lane"
                )
            canonical_source = LEGACY_UNQUALIFIED_ARCHIVE_SOURCE
        else:
            try:
                canonical_source = normalize_source_id(self.source)
            except ValueError as error:
                raise ArchiveConfigurationError(f"invalid archive source: {self.source!r}") from error
        object.__setattr__(self, "source", canonical_source)
        _validate_archive_identity(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ArchiveIdentity:
        """Build a canonical identity from a strict manifest mapping."""
        lane = _required_mapping_string(value, "lane", label="archive identity")
        allowed = {
            "forcing": {"lane", "source", "cycle_identity", "cycle_time", "basin_version_id", "model_id"},
            "runs": {"lane", "source", "cycle_identity", "cycle_time", "run_id"},
            "states": {"lane", "source", "cycle_identity", "cycle_time", "model_id"},
        }.get(lane)
        if allowed is None:
            raise ArchiveConfigurationError(f"archive lane must be one of {sorted(ARCHIVE_LANES)}: {lane!r}")
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise ArchiveConfigurationError(f"archive identity contains fields invalid for {lane}: {unexpected}")
        return cls(
            lane=lane,
            source=_required_mapping_string(value, "source", label="archive identity"),
            cycle_identity=_required_mapping_string(value, "cycle_identity", label="archive identity"),
            cycle_time=_required_mapping_string(value, "cycle_time", label="archive identity"),
            basin_version_id=_optional_mapping_string(value, "basin_version_id", label="archive identity"),
            model_id=_optional_mapping_string(value, "model_id", label="archive identity"),
            run_id=_optional_mapping_string(value, "run_id", label="archive identity"),
        )


@dataclass(frozen=True)
class ArchiveStorageConfig:
    """Validated archive configuration shared by node-27 maintenance tools."""

    archive_root: Path
    archive_min_age_days: int
    retention_days: int


class ArchiveConfigurationError(ValueError):
    """Raised when archive configuration is unsafe or incomplete."""


ARCHIVE_LANES = frozenset({"forcing", "runs", "states"})
LEGACY_UNQUALIFIED_ARCHIVE_SOURCE = "legacy-unqualified"
DEFAULT_ARCHIVE_MIN_AGE_DAYS = 14
DEFAULT_DB_RETENTION_DAYS = 14


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
    identity: ArchiveIdentity,
) -> ArchiveProvenancePaths:
    """Map a safe archive identity to its tarball and sibling manifest."""
    _validate_archive_identity(identity)
    root = _normalized_filesystem_path(archive_root, label="archive root")
    parent = root.joinpath(*_archive_identity_path_components(identity))
    return ArchiveProvenancePaths(
        archive=parent / "archive.tar.zst",
        manifest=parent / "manifest.json",
    )


def archive_identity_for_state_reference(
    *,
    source_id: str | None,
    model_id: str,
    valid_time: datetime,
) -> ArchiveIdentity:
    """Derive the canonical archive identity for a persisted state reference."""
    if not isinstance(valid_time, datetime):
        raise ArchiveConfigurationError("state reference valid_time must be a datetime")
    if valid_time.tzinfo is None or valid_time.utcoffset() is None:
        raise ArchiveConfigurationError("state reference valid_time must be timezone-aware")
    valid_time_utc = valid_time.astimezone(UTC)
    if any((valid_time_utc.minute, valid_time_utc.second, valid_time_utc.microsecond)):
        raise ArchiveConfigurationError("state reference valid_time must resolve to a canonical UTC hourly instant")

    if source_id is None or source_id == "":
        source = LEGACY_UNQUALIFIED_ARCHIVE_SOURCE
    elif source_id == LEGACY_UNQUALIFIED_ARCHIVE_SOURCE:
        raise ArchiveConfigurationError(
            f"state reference source {LEGACY_UNQUALIFIED_ARCHIVE_SOURCE!r} "
            "is derived only from source_id None or an empty string"
        )
    else:
        source = source_id
    return ArchiveIdentity(
        lane="states",
        source=source,
        cycle_identity=valid_time_utc.strftime("%Y%m%d%H"),
        cycle_time=valid_time_utc.strftime("%Y-%m-%dT%H:00:00Z"),
        model_id=model_id,
    )


def validate_product_archive_manifest_binding(
    archive_root: str | os.PathLike[str],
    manifest: Mapping[str, object],
) -> ArchiveProvenancePaths:
    """Require manifest identity and declared sibling paths to bind canonically."""
    identity_value = manifest.get("identity")
    if not isinstance(identity_value, Mapping):
        raise ArchiveConfigurationError("product archive manifest identity must be an object")
    declared_source = _required_mapping_string(identity_value, "source", label="product archive manifest identity")
    try:
        identity = ArchiveIdentity.from_mapping(identity_value)
    except ArchiveConfigurationError as error:
        raise ArchiveConfigurationError(f"invalid product archive manifest identity: {error}") from error
    if declared_source != identity.source:
        raise ArchiveConfigurationError(
            "product archive manifest source must use its canonical storage ID: "
            f"declared={declared_source!r}; canonical={identity.source!r}"
        )

    archive_value = manifest.get("archive")
    if not isinstance(archive_value, Mapping):
        raise ArchiveConfigurationError("product archive manifest archive must be an object")
    declared_archive = _required_mapping_string(archive_value, "path", label="product archive manifest archive")
    declared_manifest = _required_mapping_string(
        archive_value,
        "manifest_path",
        label="product archive manifest archive",
    )
    _validate_root_relative_path(declared_archive, label="product archive path")
    _validate_root_relative_path(declared_manifest, label="product manifest path")

    paths = archive_provenance_paths(archive_root, identity=identity)
    root = _normalized_filesystem_path(archive_root, label="archive root")
    expected_archive = paths.archive.relative_to(root).as_posix()
    expected_manifest = paths.manifest.relative_to(root).as_posix()
    if declared_archive != expected_archive:
        raise ArchiveConfigurationError(
            "product archive path does not match canonical identity: "
            f"declared={declared_archive}; expected={expected_archive}"
        )
    if declared_manifest != expected_manifest:
        raise ArchiveConfigurationError(
            "product manifest path is not the canonical archive sibling: "
            f"declared={declared_manifest}; expected={expected_manifest}"
        )
    return paths


def _archive_override_name(script_name: str) -> str:
    normalized = script_name.strip().upper().replace("-", "_")
    if not normalized or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in normalized):
        raise ArchiveConfigurationError(f"invalid archive script name: {script_name!r}")
    return f"NODE27_{normalized}_ARCHIVE_ROOT"


def _normalized_filesystem_path(path: str | os.PathLike[str], *, label: str) -> Path:
    raw = os.fspath(path).strip()
    if not raw:
        raise ArchiveConfigurationError(f"{label} must be non-empty")
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ArchiveConfigurationError(f"{label} must be absolute: {raw}")
    return expanded.resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_identity_component(component: str) -> None:
    if not isinstance(component, str):
        raise ArchiveConfigurationError("archive identity components must be strings")
    if component != component.strip() or component in {"", ".", ".."}:
        raise ArchiveConfigurationError(f"unsafe archive identity component: {component!r}")
    if (
        Path(component).is_absolute()
        or "/" in component
        or "\\" in component
        or any(ord(character) < 32 or ord(character) == 127 for character in component)
    ):
        raise ArchiveConfigurationError(f"unsafe archive identity component: {component!r}")


def _validate_archive_identity(identity: ArchiveIdentity) -> None:
    if identity.lane not in ARCHIVE_LANES:
        raise ArchiveConfigurationError(f"archive lane must be one of {sorted(ARCHIVE_LANES)}: {identity.lane!r}")
    _validate_identity_component(identity.source)
    _validate_identity_component(identity.cycle_identity)
    try:
        parsed_cycle_time = datetime.strptime(identity.cycle_time, "%Y-%m-%dT%H:00:00Z")
    except ValueError as error:
        raise ArchiveConfigurationError(
            "archive identity cycle_time must be a valid canonical UTC hourly timestamp (YYYY-MM-DDTHH:00:00Z)"
        ) from error
    canonical_cycle_time = parsed_cycle_time.strftime("%Y-%m-%dT%H:00:00Z")
    if identity.cycle_time != canonical_cycle_time:
        raise ArchiveConfigurationError(
            "archive identity cycle_time must use canonical UTC hourly form: "
            f"declared={identity.cycle_time}; canonical={canonical_cycle_time}"
        )
    expected_cycle_identity = parsed_cycle_time.strftime("%Y%m%d%H")
    if identity.cycle_identity != expected_cycle_identity:
        raise ArchiveConfigurationError(
            "archive identity cycle_time does not match cycle_identity: "
            f"cycle_time={identity.cycle_time}; cycle_identity={identity.cycle_identity}; "
            f"expected={expected_cycle_identity}"
        )
    fields = {
        "basin_version_id": identity.basin_version_id,
        "model_id": identity.model_id,
        "run_id": identity.run_id,
    }
    required = {
        "forcing": {"basin_version_id", "model_id"},
        "runs": {"run_id"},
        "states": {"model_id"},
    }[identity.lane]
    supplied = {name for name, value in fields.items() if value is not None}
    missing = sorted(required - supplied)
    invalid = sorted(supplied - required)
    if missing:
        raise ArchiveConfigurationError(f"archive identity for {identity.lane} is missing fields: {missing}")
    if invalid:
        raise ArchiveConfigurationError(f"archive identity contains fields invalid for {identity.lane}: {invalid}")
    for name in sorted(required):
        value = fields[name]
        if value is None:
            raise AssertionError("required archive identity field unexpectedly absent")
        _validate_identity_component(value)


def _archive_identity_path_components(identity: ArchiveIdentity) -> tuple[str, ...]:
    source_segment = identity.source.lower()
    base = (identity.lane, source_segment, identity.cycle_identity)
    if identity.lane == "forcing":
        assert identity.basin_version_id is not None and identity.model_id is not None
        return (*base, identity.basin_version_id, identity.model_id)
    if identity.lane == "runs":
        assert identity.run_id is not None
        return (*base, identity.run_id)
    assert identity.model_id is not None
    return (*base, identity.model_id)


def _required_mapping_string(value: Mapping[str, object], field: str, *, label: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise ArchiveConfigurationError(f"{label} field {field} must be a non-empty string")
    return raw


def _optional_mapping_string(value: Mapping[str, object], field: str, *, label: str) -> str | None:
    if field not in value:
        return None
    return _required_mapping_string(value, field, label=label)


def _validate_root_relative_path(value: str, *, label: str) -> None:
    components = value.split("/")
    if (
        value.startswith("/")
        or (len(value) >= 3 and value[0].isalpha() and value[1:3] == ":/")
        or any(component in {"", ".", ".."} for component in components)
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ArchiveConfigurationError(f"{label} must be a safe root-relative path: {value!r}")


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
