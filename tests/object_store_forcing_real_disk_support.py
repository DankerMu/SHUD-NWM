"""Run-time cycle, model and station selection for the real-disk forcing suite (#2595, #2699).

``tests/test_object_store_forcing_real_disk.py`` used to pin one cycle and name
its stations and models. node-27 retention removed the cycle, and the store now
holds only Direct Grid (``dg_*``) model directories, so the suite asks the store
and the database: the newest cycle in which every ``(basin_version_id, source)``
combination resolves to one ``dg_*`` model, a station of that model, and a
settled CSV.

Non-collectible support module, and free of any database import: the station of
a model comes from a callable the real-disk suite builds on its own connection.
The real-disk suite is ``e2e``/``real_disk`` gated, so the selection logic is
proved locally, with temporary stores, a fake resolver and a fake station
lookup, by ``tests/test_object_store_forcing_real_disk_support.py``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pytest

from packages.common.object_store_forcing import _normalize_source_id, _resolve_disk_path

# A CSV younger than this may still be being written by the forcing producer,
# so the cycle it belongs to is not a candidate yet.
MIN_SETTLED_AGE_SECONDS = 600

_CYCLE_DIR_RE = re.compile(r"^\d{10}$")
_DIRECT_GRID_MODEL_PREFIX = "dg_"


class StationLookup(Protocol):
    """``PsycopgStationLookup`` in production; a fake in the local tests."""

    def lookup(self, station_id: str) -> Any: ...


# ``model_id -> station_id | None``: ``min(station_id)`` over the model's
# ``met.interp_weight`` rows on node-27, a dict in the local tests.
StationResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class ResolvedCombo:
    """One ``(basin_version_id, source)`` combination as the chosen cycle holds it."""

    basin_version_id: str
    source_id: str
    model_id: str
    station_id: str
    active_flag: bool | None
    path: Path


@dataclass(frozen=True)
class Selection:
    cycle: str
    combos: tuple[ResolvedCombo, ...]


def latest_complete_cycle(
    root: Path,
    combos: Sequence[tuple[str, str]],
    station_for_model: StationResolver,
    lookup: StationLookup,
    *,
    now: float | None = None,
) -> Selection:
    """Return the newest cycle (ISO ``Z``) where every ``(basin_version_id, source)`` combo resolves.

    Candidates are the ``YYYYMMDDHH`` directories under ``<root>/forcing/<source>/``
    for the combos' sources. In a cycle a combo resolves when
    ``<root>/forcing/<source>/<cycle>/<basin_version_id>/`` holds exactly one
    ``dg_*`` directory (the model; none or several is absent, never a guess),
    ``station_for_model`` names a station for it, and that station's CSV -- path
    from the production resolver (``_normalize_source_id`` + ``_resolve_disk_path``)
    with ``basin_version_id`` and ``forcing_filename`` from ``lookup`` -- exists
    with an mtime at least ``MIN_SETTLED_AGE_SECONDS`` old. Both callables are
    asked once per model / station, not per cycle. No qualifying cycle is a hard
    failure naming the root, per combo how many cycles it resolves in, and why
    each absent combo was absent in the newest cycle examined -- never a skip.
    """
    current = time.time() if now is None else now
    labelled = [
        (f"{basin_version_id}/{source_id}", basin_version_id, source_id, _normalize_source_id(source_id))
        for basin_version_id, source_id in combos
    ]

    candidates: set[str] = set()
    for source in sorted({entry[3] for entry in labelled}):
        source_root = root / "forcing" / source
        if source_root.is_dir():
            candidates.update(
                child.name
                for child in source_root.iterdir()
                if child.is_dir() and _CYCLE_DIR_RE.fullmatch(child.name) and _cycle_time(child.name) is not None
            )

    stations_by_model: dict[str, str | None] = {}
    stations: dict[str, Any] = {}

    def resolve(basin_version_id: str, source_id: str, source: str, cycle: str) -> ResolvedCombo | str:
        basin_dir = root / "forcing" / source / cycle / basin_version_id
        models = (
            sorted(
                child.name
                for child in basin_dir.iterdir()
                if child.is_dir() and child.name.startswith(_DIRECT_GRID_MODEL_PREFIX)
            )
            if basin_dir.is_dir()
            else []
        )
        if not models:
            return f"no dg_* directory under {basin_dir.relative_to(root)}"
        if len(models) > 1:
            return (
                f"{len(models)} dg_* directories under {basin_dir.relative_to(root)}, not guessing: {', '.join(models)}"
            )
        model_id = models[0]
        if model_id not in stations_by_model:
            stations_by_model[model_id] = station_for_model(model_id)
        station_id = stations_by_model[model_id]
        if station_id is None:
            return f"no interp_weight row for {model_id}"
        if station_id not in stations:
            stations[station_id] = lookup.lookup(station_id)
        station = stations[station_id]
        path = _resolve_disk_path(
            root, source, cycle, station.basin_version_id, model_id, station.forcing_filename or ""
        )
        if not path.is_file():
            return f"station file missing: {path.relative_to(root)}"
        if current - path.stat().st_mtime < MIN_SETTLED_AGE_SECONDS:
            return f"station file younger than {MIN_SETTLED_AGE_SECONDS} s: {path.relative_to(root)}"
        return ResolvedCombo(basin_version_id, source_id, model_id, station_id, station.active_flag, path)

    counts = {label: 0 for label, *_ in labelled}
    reasons = {label: f"no cycle directory under forcing/{source}" for label, _, _, source in labelled}
    ordered = sorted(candidates, reverse=True)
    for cycle in ordered:
        outcomes = {
            label: resolve(basin_version_id, source_id, source, cycle)
            for label, basin_version_id, source_id, source in labelled
        }
        resolved = tuple(outcome for outcome in outcomes.values() if isinstance(outcome, ResolvedCombo))
        if len(resolved) == len(labelled):
            cycle_time = _cycle_time(cycle)
            assert cycle_time is not None
            return Selection(cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"), resolved)
        for label, outcome in outcomes.items():
            if isinstance(outcome, ResolvedCombo):
                counts[label] += 1
        if cycle == ordered[0]:
            reasons = {label: outcome for label, outcome in outcomes.items() if isinstance(outcome, str)}

    pytest.fail(
        f"real store has no cycle where all {len(labelled)} combos are present "
        f"(root={root}, per-combo counts={counts}, "
        f"newest cycle examined={ordered[0] if ordered else 'none'}, reasons={reasons})"
    )


def _cycle_time(compact: str) -> datetime | None:
    try:
        return datetime.strptime(compact, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None
