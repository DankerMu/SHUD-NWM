"""G6 filesystem reconciliation: moved-member bytes vs observed free-space deltas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.common.node27_issue1895_types import Issue1895ReadinessError

DEFAULT_ALLOCATION_GRANULARITY_BYTES = 4096
DEFAULT_CONCURRENT_NOISE_BYTES = 64 * 1024 * 1024


def _nonneg_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Issue1895ReadinessError(
            f"{label} is not a non-negative integer",
            code="FS_VALUE_INVALID",
            stage="filesystem",
        )
    if value < 0:
        raise Issue1895ReadinessError(
            f"{label} is negative",
            code="FS_VALUE_INVALID",
            stage="filesystem",
        )
    return value


def member_bytes(members: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for member in members:
        if not isinstance(member, Mapping):
            raise Issue1895ReadinessError(
                "member payload is not an object",
                code="FS_MEMBER_INVALID",
                stage="filesystem",
            )
        total += _nonneg_int(member.get("bytes"), label="member.bytes")
    return total


def reconcile_moved_group_filesystem(
    *,
    members: Sequence[Mapping[str, Any]],
    hot_avail_before: int,
    hot_avail_after: int,
    cold_avail_before: int,
    cold_avail_after: int,
    allocation_granularity_bytes: int = DEFAULT_ALLOCATION_GRANULARITY_BYTES,
    concurrent_noise_bytes: int = DEFAULT_CONCURRENT_NOISE_BYTES,
) -> dict[str, Any]:
    """Tie hot reclamation and cold growth to the exact moved member bytes.

    Direction: hot free must not shrink (reclamation) and cold free must not
    grow (the destination consumed space). Tolerance covers allocation
    granularity plus concurrent noise. Reversed or missing deltas are NO-GO.
    """

    moved = member_bytes(members)
    if moved <= 0:
        raise Issue1895ReadinessError(
            "moved member bytes are missing",
            code="FS_MEMBERS_EMPTY",
            stage="filesystem",
        )
    granularity = _nonneg_int(allocation_granularity_bytes, label="allocation_granularity_bytes")
    noise = _nonneg_int(concurrent_noise_bytes, label="concurrent_noise_bytes")
    tolerance = granularity + noise
    hot_reclaim = _nonneg_int(hot_avail_after, label="hot_avail_after") - _nonneg_int(
        hot_avail_before, label="hot_avail_before"
    )
    cold_growth = _nonneg_int(cold_avail_before, label="cold_avail_before") - _nonneg_int(
        cold_avail_after, label="cold_avail_after"
    )
    if hot_reclaim <= 0:
        raise Issue1895ReadinessError(
            "hot reclamation is not positive",
            code="FS_HOT_NOT_POSITIVE",
            stage="filesystem",
        )
    if cold_growth <= 0:
        raise Issue1895ReadinessError(
            "cold growth is not positive",
            code="FS_COLD_NOT_POSITIVE",
            stage="filesystem",
        )
    hot_residual = abs(hot_reclaim - moved)
    cold_residual = abs(cold_growth - moved)
    if hot_residual > tolerance or cold_residual > tolerance:
        raise Issue1895ReadinessError(
            "filesystem deltas are outside allocation/noise tolerance",
            code="FS_RECONCILE_NO_GO",
            stage="filesystem",
        )
    return {
        "approved": True,
        "moved_member_bytes": moved,
        "hot_reclaim_bytes": hot_reclaim,
        "cold_growth_bytes": cold_growth,
        "hot_residual_bytes": hot_residual,
        "cold_residual_bytes": cold_residual,
        "tolerance_bytes": tolerance,
        "allocation_granularity_bytes": granularity,
        "concurrent_noise_bytes": noise,
        "governance": "within_tolerance",
        "blockers": (),
    }
