"""Lane vocabulary and env parsing for `scripts/node27_raw_retention.py`.

Split out of the runner by responsibility (#3087, 1000-line guard): WHICH lanes
exist and how their env values are read live here; how a lane is walked, locked
and removed stays in the runner, which remains the only entry point (callers
keep importing `scripts.node27_raw_retention`). Stdlib + `source_identity` only.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any

from packages.common.source_identity import normalize_source_id

DEFAULT_SOURCES = ("gfs", "ifs")
CYCLE_NAME_LENGTH = 10

# Lane identity: (target key prefix, `planned[].reason`).
RAW_LANE_KEY = "raw"
RAW_LANE_REASON = "raw_cycle_aged_out"
CANONICAL_LANE_KEY = "canonical"
CANONICAL_LANE_REASON = "canonical_cycle_aged_out"
PRECIP_CACHE_LANE_KEY = "precip-cache"
PRECIP_CACHE_LANE_REASON = "precip_cache_aged_out"
FORCING_LANE_KEY = "forcing"
FORCING_LANE_REASON = "forcing_cycle_aged_out"
# `NODE27_RAW_RETENTION_LANES` vocabulary (#2360, forcing #3087): the lane key
# prefixes above. Unset selects DEFAULT_LANES, the pre-#3087 run byte for byte.
ALL_LANES = frozenset({RAW_LANE_KEY, CANONICAL_LANE_KEY, PRECIP_CACHE_LANE_KEY, FORCING_LANE_KEY})
DEFAULT_LANES = frozenset({RAW_LANE_KEY, CANONICAL_LANE_KEY, PRECIP_CACHE_LANE_KEY})
# Reasons whose removal holds the copyback batch mutex: node-22 copyback writers
# promote `canonical/` and `forcing/` trees under that lock.
COPYBACK_LOCKED_REASONS = frozenset({CANONICAL_LANE_REASON, FORCING_LANE_REASON})
# The forcing lane's own retention period (#3087, owner decision: 30 days).
FORCING_DAYS_ENV = "NODE27_RAW_RETENTION_FORCING_DAYS"
DEFAULT_FORCING_RETENTION_DAYS = 30
# Fail-closed floor = max(services.tiles.mvt.NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS
# + 1, AUTOPIPE_MAX_CYCLE_AGE_DAYS in infra/env/node27-ingest.example) =
# max(12 + 1, 14): display station-series reads forcing inside the lookback and
# ingest reads the forcing handoff inside the autopipe cycle floor. A literal so
# this module imports neither; a drift test pins it against both sources.
FORCING_RETENTION_FLOOR_DAYS = 14
# Upper bound for BOTH retention periods (#3089): an unbounded value made
# `reference_time - timedelta(days=...)` raise OverflowError -- a traceback and
# no summary. 3650 days (10 years) is far above any operational value.
RETENTION_DAYS_CEILING = 3650
LANE_NOT_SELECTED_REASON = "lane_not_selected"
# The one directory name under `canonical/<S>/` that is never a cycle: grid
# definitions are not reproducible from node-27, so they are pinned out of the
# target set explicitly instead of relying on `_parse_cycle_name` alone.
GRID_DIR_NAME = "grid"


def _env_int(name: str, *, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_sources(raw: str | None) -> frozenset[str]:
    values = raw if raw not in (None, "") else ",".join(DEFAULT_SOURCES)
    sources = {item.strip().lower() for item in str(values).split(",") if item.strip()}
    return frozenset(sources or DEFAULT_SOURCES)


def _split_lanes(raw: str | None) -> tuple[frozenset[str] | None, dict[str, Any] | None]:
    """The selected lanes, or the `lanes` preflight blocker.

    Unset is the legacy three lanes, never forcing (#3087). A set value is
    fail-closed: exact names only, and a value that trims to nothing is refused
    rather than read as "all" -- a blank line in the env file must not silently
    widen a canonical-only unit.
    """
    if raw is None:
        return DEFAULT_LANES, None
    names = [item.strip() for item in raw.split(",")]
    lanes = frozenset(name for name in names if name)
    if not lanes:
        return None, {"field": "lanes", "reason": "empty"}
    unknown = sorted(lanes - ALL_LANES)
    if unknown:
        return None, {"field": "lanes", "reason": "unknown_lane", "value": unknown}
    return lanes, None


def _forcing_retention_days(raw_value: str | None) -> tuple[int | None, dict[str, Any] | None]:
    """Strict `NODE27_RAW_RETENTION_FORCING_DAYS` (#3087): unset/blank is 30.

    `scripts/node27_autopipeline.py:_max_cycle_age_days`'s pattern, not
    `_env_int`, which would read `0` or `abc` as the default. The floor applies
    whether or not the lane is selected: one env file feeds both units.
    """
    raw = (raw_value or "").strip()
    if not raw:
        return DEFAULT_FORCING_RETENTION_DAYS, None
    if re.fullmatch(r"[0-9]+", raw) is None:
        reason = "not_an_integer"
    elif int(raw) < 1:
        reason = "must_be_positive"
    elif int(raw) < FORCING_RETENTION_FLOOR_DAYS:
        reason = "below_floor"
    elif int(raw) > RETENTION_DAYS_CEILING:
        return None, {
            "field": "forcing_retention_days",
            "reason": "above_ceiling",
            "value": raw_value,
            "floor": FORCING_RETENTION_FLOOR_DAYS,
            "ceiling": RETENTION_DAYS_CEILING,
        }
    else:
        return int(raw), None
    return None, {
        "field": "forcing_retention_days",
        "reason": reason,
        "value": raw_value,
        "floor": FORCING_RETENTION_FLOOR_DAYS,
    }


def _forcing_segment(source: str) -> str:
    """The forcing producer's object-store segment (`_object_source_segment`)."""
    return normalize_source_id(source).lower()


def _parse_cycle_name(name: str) -> datetime | None:
    if len(name) != CYCLE_NAME_LENGTH or not name.isdigit():
        return None
    try:
        return datetime.strptime(name, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None
