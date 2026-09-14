"""Independent G8 watermark/cutoff observation for display-runtime consumers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from packages.common.compressed_chunk_cold_residency import compute_cutoff, json_ready
from packages.common.display_watermark import fetch_display_watermark
from packages.common.node27_issue1895_env import validate_canonical_positive_decimal
from packages.common.node27_issue1895_types import Issue1895ReadinessError


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_lag_seconds(value: object) -> int:
    text = value if isinstance(value, str) else str(value or "")
    return int(validate_canonical_positive_decimal(text, label="lag_seconds"))


def observe_current_cutoff(
    *,
    dsn: str,
    lag_seconds: object,
    connect: Callable[..., Any] | None = None,
    watermark: datetime | None = None,
) -> dict[str, Any]:
    """Re-query W8 = UTC(MAX complete forecast cycle_time) and C8 = W8 - lag."""

    lag = parse_lag_seconds(lag_seconds)
    live = watermark if watermark is not None else fetch_display_watermark(dsn, connect=connect)
    cutoff = compute_cutoff(live, lag)
    return {
        "watermark": iso_utc(live),
        "cutoff": iso_utc(cutoff),
        "lag_seconds": lag,
    }


def assert_independent_receipt_horizon(
    receipt: Mapping[str, Any],
    *,
    expected_watermark: str,
    expected_cutoff: str,
    expected_lag_seconds: int,
    expected_per_tick_bound: int = 1,
) -> None:
    if json_ready(receipt.get("watermark")) != json_ready(expected_watermark):
        raise Issue1895ReadinessError(
            "receipt watermark is not the independently observed W8",
            code="TICK_WATERMARK_MISMATCH",
            stage="watermark",
        )
    if json_ready(receipt.get("cutoff")) != json_ready(expected_cutoff):
        raise Issue1895ReadinessError(
            "receipt cutoff is not the independently observed C8",
            code="TICK_CUTOFF_MISMATCH",
            stage="watermark",
        )
    if receipt.get("lag_seconds") != expected_lag_seconds:
        raise Issue1895ReadinessError(
            "receipt lag_seconds drifted from the exact cold env lag",
            code="TICK_LAG_MISMATCH",
            stage="watermark",
        )
    if receipt.get("per_tick_bound") != expected_per_tick_bound:
        raise Issue1895ReadinessError(
            "receipt per_tick_bound drifted",
            code="TICK_BOUND_MISMATCH",
            stage="watermark",
        )


def parse_systemctl_show(text: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    pending: str | None = None
    chunks: list[str] = []
    for line in str(text).splitlines():
        if "=" in line and not line.startswith(" "):
            if pending is not None:
                joined = "\n".join(chunks)
                facts[pending] = f"{facts[pending]}\n{joined}" if pending in facts else joined
            key, _sep, value = line.partition("=")
            pending = key
            chunks = [value]
        elif pending is not None:
            chunks.append(line)
    if pending is not None:
        joined = "\n".join(chunks)
        facts[pending] = f"{facts[pending]}\n{joined}" if pending in facts else joined
    return facts
