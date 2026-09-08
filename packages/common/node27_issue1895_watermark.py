"""Independent G8 watermark/cutoff and systemd invocation facts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from packages.common.compressed_chunk_cold_residency import compute_cutoff, json_ready
from packages.common.display_watermark import fetch_display_watermark
from packages.common.node27_issue1895_env import validate_canonical_positive_decimal
from packages.common.node27_issue1895_types import Issue1895ReadinessError

COMPRESSION_TIMER = "nhms-node27-timeseries-compression.timer"
COMPRESSION_SERVICE = "nhms-node27-timeseries-compression.service"
COMPRESSION_WRAPPER = "/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh"
COLD_WRAPPER = "/home/nwm/NWM/scripts/node27_cold_residency_once.sh"
TIMER_FRAGMENT = "/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.timer"
SERVICE_FRAGMENT = "/home/nwm/NWM/infra/systemd/nhms-node27-timeseries-compression.service"


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


def _require_text(facts: Mapping[str, Any], key: str) -> str:
    value = facts.get(key)
    if not isinstance(value, str) or not value.strip() or value.strip() in {"n/a", "0"}:
        raise Issue1895ReadinessError(
            f"systemd fact {key} is missing",
            code="SYSTEMD_FACT_MISSING",
            stage="systemd",
        )
    return value.strip()


def parse_exec_start(value: str) -> tuple[str, ...]:
    entries: list[str] = []
    for raw in str(value).split("\n"):
        text = raw.strip()
        if not text:
            continue
        if text.startswith("{") and "; " in text:
            text = text.split("; ", 1)[1].rstrip("}")
        entries.append(text)
    return tuple(entries)


def assert_systemd_invocation_facts(
    *,
    timer: Mapping[str, Any],
    service: Mapping[str, Any],
    expected_timer: str = COMPRESSION_TIMER,
    expected_service: str = COMPRESSION_SERVICE,
) -> dict[str, Any]:
    timer_id = _require_text(timer, "Id")
    if timer_id != expected_timer:
        raise Issue1895ReadinessError(
            "timer Id is not the compression timer",
            code="SYSTEMD_TIMER_ID",
            stage="systemd",
        )
    if _require_text(timer, "Unit") != expected_service:
        raise Issue1895ReadinessError(
            "timer Unit is not the compression service",
            code="SYSTEMD_TIMER_UNIT",
            stage="systemd",
        )
    fragment = _require_text(timer, "FragmentPath")
    if not fragment.endswith("nhms-node27-timeseries-compression.timer"):
        raise Issue1895ReadinessError(
            "timer FragmentPath is not the shipping unit",
            code="SYSTEMD_TIMER_FRAGMENT",
            stage="systemd",
        )
    service_id = _require_text(service, "Id")
    if service_id != expected_service:
        raise Issue1895ReadinessError(
            "service Id is not the compression service",
            code="SYSTEMD_SERVICE_ID",
            stage="systemd",
        )
    service_fragment = _require_text(service, "FragmentPath")
    if not service_fragment.endswith("nhms-node27-timeseries-compression.service"):
        raise Issue1895ReadinessError(
            "service FragmentPath is not the shipping unit",
            code="SYSTEMD_SERVICE_FRAGMENT",
            stage="systemd",
        )
    exec_start = parse_exec_start(str(service.get("ExecStart") or ""))
    if len(exec_start) < 2:
        raise Issue1895ReadinessError(
            "service ExecStart does not list both wrappers",
            code="SYSTEMD_EXEC_START",
            stage="systemd",
        )
    if COMPRESSION_WRAPPER not in exec_start[0] or COLD_WRAPPER not in exec_start[1]:
        raise Issue1895ReadinessError(
            "ExecStart order is not compression wrapper then cold wrapper",
            code="SYSTEMD_EXEC_ORDER",
            stage="systemd",
        )
    invocation = _require_text(service, "InvocationID")
    start = _require_text(service, "ExecMainStartTimestamp")
    end = _require_text(service, "ExecMainExitTimestamp")
    result = _require_text(service, "Result")
    if result != "success":
        raise Issue1895ReadinessError(
            "service Result is not success",
            code="SYSTEMD_RESULT",
            stage="systemd",
        )
    return {
        "timer_id": timer_id,
        "timer_unit": expected_service,
        "timer_fragment": fragment,
        "service_id": service_id,
        "service_fragment": service_fragment,
        "exec_start": list(exec_start),
        "invocation_id": invocation,
        "start": start,
        "end": end,
        "result": result,
    }


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
