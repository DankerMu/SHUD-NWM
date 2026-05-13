"""Shared source-id normalization for storage and repository boundaries."""

from __future__ import annotations

_STORAGE_SOURCE_IDS = {
    "GFS": "gfs",
    "ERA5": "ERA5",
    "IFS": "IFS",
}


def normalize_source_id(source_id: str) -> str:
    """Normalize source_id for storage/repository use.

    GFS -> gfs, ERA5 -> ERA5, IFS -> IFS.
    Case-insensitive input, deterministic output.
    """
    normalized = _STORAGE_SOURCE_IDS.get(source_id.upper())
    if normalized is None:
        raise ValueError(f"Unknown source_id: {source_id!r}")
    return normalized
