"""Shared source-id normalization for storage and repository boundaries."""

from __future__ import annotations

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
