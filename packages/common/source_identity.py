"""Shared source-id normalization for storage and repository boundaries."""

from __future__ import annotations

from typing import Literal, get_args

#: The `source` values the national display surface serves (`/cycles`,
#: `/valid-times`, the canonical national tile route). A `Literal`, never an
#: `Enum`: an Enum makes FastAPI emit a `$ref` the hand-maintained
#: `openapi/nhms.v1.yaml` would have to mirror. The display routes annotate
#: their `source` parameter with this alias, and
#: `scripts/node27_coverage_freshness_alert.py` evaluates only these keys
#: (#2464), so the lane's observed set cannot drift from the served set.
#: Deliberately NOT the storage map below: that one accepts `ERA5`, which the
#: display surface does not serve.
DisplaySourceId = Literal["gfs", "ifs"]
DISPLAY_SOURCE_IDS: frozenset[str] = frozenset(get_args(DisplaySourceId))

_STORAGE_SOURCE_IDS = {
    "GFS": "gfs",
    "ERA5": "ERA5",
    "IFS": "IFS",
}


def normalize_source_id(source_id: str | None) -> str:
    if source_id is None:
        raise ValueError("source_id must not be None")
    normalized = _STORAGE_SOURCE_IDS.get(source_id.upper())
    if normalized is None:
        raise ValueError(f"Unknown source_id: {source_id!r}")
    return normalized
