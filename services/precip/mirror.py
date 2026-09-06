"""Mirror keyspace and the past-24h window resolver.

Two pinned rules map a route `(source, cycle)` pair onto mirror paths and
nothing else does any string handling:

- source: the closed enum `{gfs, ifs}` FIRST, then
  `packages.common.source_identity.normalize_source_id` (`ifs` -> `IFS`);
- cycle: `strftime("%Y%m%d%H")`, the same token shape
  `workers/canonical_converter/converter.py::format_cycle_time` produces (that
  module is deliberately NOT imported: it belongs to the compute plane).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.common.source_identity import normalize_source_id
from services.precip.constants import (
    GRID_IDS,
    PRECIP_FORECAST_HORIZON_HOURS,
    PRECIP_ROUTE_SOURCES,
    PRECIP_STEP_HOURS,
    PRECIP_VARIABLE,
    PRECIP_WINDOW_SLICES,
)
from services.precip.errors import PrecipWindowIncomplete

_CYCLE_TOKEN_RE = re.compile(r"^\d{10}$")


@dataclass(frozen=True)
class Slice:
    """One mirrored 3h canonical slice selected for a window end time."""

    cycle: datetime
    lead_hours: int
    path: Path
    object_key: str


def storage_source_for(route_source: str) -> str:
    """Enum first, normalization second — 422 before any filesystem call."""
    if route_source not in PRECIP_ROUTE_SOURCES:
        raise ValueError(f"Unsupported precipitation source: {route_source!r}")
    return normalize_source_id(route_source)


def grid_id_for(storage_source: str) -> str:
    try:
        return GRID_IDS[storage_source]
    except KeyError as exc:  # pragma: no cover - unreachable behind the enum
        raise ValueError(f"Unknown storage source: {storage_source!r}") from exc


def cycle_token(cycle: datetime) -> str:
    instant = cycle.astimezone(UTC) if cycle.tzinfo is not None else cycle.replace(tzinfo=UTC)
    return instant.strftime("%Y%m%d%H")


def precip_directory_key(storage_source: str, token: str) -> str:
    return f"canonical/{storage_source}/{token}/{PRECIP_VARIABLE}"


def slice_object_key(storage_source: str, token: str, lead_hours: int) -> str:
    directory = precip_directory_key(storage_source, token)
    return f"{directory}/{storage_source}_{token}_{PRECIP_VARIABLE}_f{lead_hours:03d}.nc"


def grid_object_key(storage_source: str, grid_id: str | None = None) -> str:
    resolved = grid_id or grid_id_for(storage_source)
    return f"canonical/{storage_source}/grid/{resolved}/grid.json"


def cycle_is_mirrored(mirror_root: Path, storage_source: str, cycle: datetime) -> bool:
    """"Mirrored" is the presence of the variable directory, not of any lead."""
    key = precip_directory_key(storage_source, cycle_token(cycle))
    return (Path(mirror_root) / key).is_dir()


def discover_mirrored_cycles(mirror_root: Path, storage_source: str) -> tuple[datetime, ...]:
    """One `listdir` of `canonical/<S>/`, ascending; unparseable names ignored."""
    base = Path(mirror_root) / "canonical" / storage_source
    try:
        entries = os.listdir(base)
    except OSError:
        return ()
    cycles: list[datetime] = []
    for name in entries:
        if not _CYCLE_TOKEN_RE.match(name):
            continue
        try:
            parsed = datetime.strptime(name, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            continue
        if not (base / name / PRECIP_VARIABLE).is_dir():
            continue
        cycles.append(parsed)
    return tuple(sorted(cycles))


def horizon_valid_times(cycle: datetime) -> list[datetime]:
    """The 3h grid `cycle + 3h*k` for `k = 0 ... 56`, i.e. `[cycle, cycle+168h]`.

    The single definition of "a valid_time this cycle can answer for": the index
    enumerates it, the PNG route's request-shape gate tests membership in it, and
    #2013's prewarm reuses it. Lives here rather than in the route so the three
    cannot drift apart.
    """
    steps = PRECIP_FORECAST_HORIZON_HOURS // PRECIP_STEP_HOURS
    return [cycle + timedelta(hours=PRECIP_STEP_HOURS * step) for step in range(steps + 1)]


def window_end_times(valid_time: datetime) -> tuple[datetime, ...]:
    """The eight 3h end times `valid_time - 21h ... valid_time`, ascending."""
    return tuple(
        valid_time - timedelta(hours=PRECIP_STEP_HOURS * offset)
        for offset in range(PRECIP_WINDOW_SLICES - 1, -1, -1)
    )


def resolve_window(
    source: str,
    cycle: datetime,
    valid_time: datetime,
    mirror_root: Path | str,
    *,
    mirrored_cycles: tuple[datetime, ...] | None = None,
) -> list[Slice]:
    """The eight slices whose sum is the past-24h field at `valid_time`.

    For every end time `T` the most recent mirrored cycle
    `C <= min(requested cycle, T - 3h)` is selected and lead `T - C` read. The
    requested cycle is an UPPER BOUND: newer mirrored cycles are never borrowed
    from, which is what keeps the resolved set (and therefore the PNG cache key)
    stable as the mirror grows forward. `C <= T - 3h` also makes every lead >= 3h,
    so GFS shipping no f000 is not a gap.

    A gap inside the SELECTED cycle fails closed rather than falling back to an
    older cycle: falling back would make the slice set drift with mirror
    completeness and break cache-key determinism.

    Does NOT check that the requested cycle itself is mirrored — that is the
    route-level gate (`PrecipCycleNotMirrored`), because a lead-0 window is
    legitimately served entirely by earlier cycles.
    """
    storage_source = storage_source_for(source)
    root = Path(mirror_root)
    candidates = (
        discover_mirrored_cycles(root, storage_source) if mirrored_cycles is None else tuple(mirrored_cycles)
    )
    requested = cycle.astimezone(UTC) if cycle.tzinfo is not None else cycle.replace(tzinfo=UTC)
    slices: list[Slice] = []
    for end_time in window_end_times(valid_time):
        upper_bound = min(requested, end_time - timedelta(hours=PRECIP_STEP_HOURS))
        selected = _most_recent_at_or_before(candidates, upper_bound)
        if selected is None:
            raise PrecipWindowIncomplete(
                missing=None,
                reason="no_mirrored_cycle_before_window_end",
                window_end=end_time,
            )
        lead_hours = int((end_time - selected).total_seconds() // 3600)
        object_key = slice_object_key(storage_source, cycle_token(selected), lead_hours)
        path = root / object_key
        if not path.is_file():
            raise PrecipWindowIncomplete(missing=object_key)
        slices.append(Slice(cycle=selected, lead_hours=lead_hours, path=path, object_key=object_key))
    return slices


def _most_recent_at_or_before(candidates: tuple[datetime, ...], upper_bound: datetime) -> datetime | None:
    selected: datetime | None = None
    for candidate in candidates:  # ascending
        if candidate <= upper_bound:
            selected = candidate
        else:
            break
    return selected


def slice_digest(slices: list[Slice]) -> str:
    """First 12 hex of sha256 over the resolved object keys in window order.

    A late-arriving intermediate cycle changes the slice set, therefore the
    digest, therefore the cache file name — so new bytes never hide behind an
    old file under a new ETag.
    """
    payload = "\n".join(item.object_key for item in slices).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]
