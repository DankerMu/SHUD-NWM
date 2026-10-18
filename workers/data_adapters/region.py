"""Compatibility re-export of :mod:`packages.common.contracts.region` (removed in burndown task 1.3)."""

from packages.common.contracts.region import (
    DEFAULT_BBOX_EAST,
    DEFAULT_BBOX_NORTH,
    DEFAULT_BBOX_SOUTH,
    DEFAULT_BBOX_WEST,
    GeoBBox,
    china_buffered_bbox_from_env,
)

__all__ = [
    "DEFAULT_BBOX_EAST",
    "DEFAULT_BBOX_NORTH",
    "DEFAULT_BBOX_SOUTH",
    "DEFAULT_BBOX_WEST",
    "GeoBBox",
    "china_buffered_bbox_from_env",
]
