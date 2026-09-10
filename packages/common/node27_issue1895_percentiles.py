"""Nearest-rank P95 matching the merged #1970 river-click binder."""

from __future__ import annotations

import math
from collections.abc import Sequence

from packages.common.node27_issue1895_types import Issue1895ReadinessError

WARMUP_COUNT = 1
ACCEPTED_SAMPLE_COUNT = 20
P95_NEAREST_RANK_INDEX = 18  # 0-based; ceil(20 * 0.95) - 1
SQL_P95_LIMIT_MS = 300
API_P95_LIMIT_MS = 500
BROWSER_P95_LIMIT_MS = 2000


def nearest_rank_p95(samples: Sequence[float]) -> float:
    """Return the nearest-rank P95; N=20 uses 0-based index 18."""

    if len(samples) != ACCEPTED_SAMPLE_COUNT:
        raise Issue1895ReadinessError(
            "P95 requires exactly 20 accepted samples",
            code="P95_SAMPLE_COUNT",
            stage="p95",
        )
    values: list[float] = []
    for item in samples:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise Issue1895ReadinessError(
                "P95 sample is not a finite number",
                code="P95_SAMPLE_INVALID",
                stage="p95",
            )
        number = float(item)
        if not math.isfinite(number) or number < 0:
            raise Issue1895ReadinessError(
                "P95 sample is not a finite non-negative number",
                code="P95_SAMPLE_INVALID",
                stage="p95",
            )
        values.append(number)
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    if index != P95_NEAREST_RANK_INDEX:
        raise Issue1895ReadinessError(
            "nearest-rank index drifted from the 20-sample contract",
            code="P95_INDEX_DRIFT",
            stage="p95",
        )
    return ordered[index]
