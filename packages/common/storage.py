from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
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


# Runner-equivalent default for the DB retention window (#1227). This constant
# exists ONLY to mirror `scripts/node27_timeseries_retention.py`, whose window
# variable is optional: a missing or empty assignment means the runner runs
# this many days. The retention runner imports this same constant so the two
# sides cannot drift. It is NEVER a fallback for an unreadable window source:
# the runner refuses those itself, and the env extractor that used to mirror
# that refusal on this side retired with its last caller (#1395).
DEFAULT_RETENTION_WINDOW_DAYS = 14

#: The retention window's one env spelling (#2504 D6). Read by the retention
#: runner (from `infra/env/node27-timeseries-retention.env`) and by the display
#: coverage refresh (from `infra/env/node27-ingest.env`); both go through
#: `configured_retention_window_days`, so the two cannot parse it differently.
RETENTION_WINDOW_ENV = "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS"


def configured_retention_window_days(env: Mapping[str, str]) -> int | None:
    """The window ``env`` states, or ``None`` when it states none (#2504 D6).

    ``None`` covers BOTH an absent/empty assignment and every value the
    retention runner's strict parser refuses (surrounding whitespace, a
    non-integer, anything below 1): accepting a value here that the runner
    rejects would let the coverage refresh act on a window retention never
    runs. There is deliberately no default. The runner maps ``None`` onto its
    own default or typed refusal; the coverage refresh maps it onto "no
    relaxation" (fail-closed), because falling back to
    ``DEFAULT_RETENTION_WINDOW_DAYS`` there would relax the #1446 guard for a
    band whose chunks a longer live window has not dropped.
    """
    raw = env.get(RETENTION_WINDOW_ENV)
    if raw is None or raw == "" or raw != raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 1 else None


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
