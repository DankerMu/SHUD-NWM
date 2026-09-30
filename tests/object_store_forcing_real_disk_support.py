"""Run-time cycle selection for the real-disk forcing suite (#2595).

``tests/test_object_store_forcing_real_disk.py`` used to pin one cycle. node-27
retention removed it and every case 404ed, so the suite now asks the store: the
newest cycle for which every station/source combination has a settled CSV.

Non-collectible support module: the real-disk suite is ``e2e``/``real_disk``
gated, and the selection logic is proved locally, with temporary stores and a
fake station lookup, by ``tests/test_object_store_forcing_real_disk_support.py``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pytest

from packages.common.object_store_forcing import _normalize_source_id, _resolve_disk_path

# A CSV younger than this may still be being written by the forcing producer,
# so the cycle it belongs to is not a candidate yet.
MIN_SETTLED_AGE_SECONDS = 600

_CYCLE_DIR_RE = re.compile(r"^\d{10}$")


class StationLookup(Protocol):
    """``PsycopgStationLookup`` in production; a fake in the local tests."""

    def lookup(self, station_id: str) -> Any: ...


def latest_complete_cycle(
    root: Path,
    combos: Sequence[tuple[str, str, str]],
    lookup: StationLookup,
    *,
    now: float | None = None,
) -> str:
    """Return the newest cycle (ISO ``Z``) where every ``(station, source, model)`` combo is settled.

    Paths come from the production resolver (``_normalize_source_id`` +
    ``_resolve_disk_path``), with each station's ``basin_version_id`` and
    ``forcing_filename`` taken from ``lookup`` once per combo. Candidates are the
    ``YYYYMMDDHH`` directories under ``<root>/forcing/<source>/`` for the combos'
    sources. A cycle qualifies only when all combos resolve to an existing file
    whose mtime is at least ``MIN_SETTLED_AGE_SECONDS`` old. No qualifying cycle
    is a hard failure naming the root and, per combo, how many cycles it is
    settled in -- never a skip.
    """
    current = time.time() if now is None else now
    resolved: list[tuple[str, str, str, str, str]] = []
    for station_id, source_id, model_id in combos:
        station = lookup.lookup(station_id)
        resolved.append(
            (
                f"{station_id}/{source_id}/{model_id}",
                _normalize_source_id(source_id),
                model_id,
                station.basin_version_id,
                station.forcing_filename or "",
            )
        )

    candidates: set[str] = set()
    for source in sorted({entry[1] for entry in resolved}):
        source_root = root / "forcing" / source
        if source_root.is_dir():
            candidates.update(
                child.name
                for child in source_root.iterdir()
                if child.is_dir() and _CYCLE_DIR_RE.fullmatch(child.name) and _cycle_time(child.name) is not None
            )

    settled_counts = {label: 0 for label, *_ in resolved}
    newest: str | None = None
    for cycle in sorted(candidates, reverse=True):
        settled = [
            label
            for label, source, model_id, basin_version_id, forcing_filename in resolved
            if _is_settled(
                _resolve_disk_path(root, source, cycle, basin_version_id, model_id, forcing_filename),
                current,
            )
        ]
        for label in settled:
            settled_counts[label] += 1
        if newest is None and len(settled) == len(resolved):
            newest = cycle

    if newest is None:
        pytest.fail(
            f"real store has no cycle where all {len(resolved)} combos are present "
            f"(root={root}, per-combo counts={settled_counts})"
        )
    cycle_time = _cycle_time(newest)
    assert cycle_time is not None
    return cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cycle_time(compact: str) -> datetime | None:
    try:
        return datetime.strptime(compact, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def _is_settled(path: Path, now: float) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    return path.is_file() and now - stat.st_mtime >= MIN_SETTLED_AGE_SECONDS
