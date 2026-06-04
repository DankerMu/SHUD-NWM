"""Shared warm-start time-consistency helper (M24 §2 Lane 2).

The three-way (snapshot ``valid_time`` / native ``.cfg.ic`` header minute-time / run
``start_time``) equality check is used both at daemon selection time
(``services.orchestrator.chain``) and on the forecast-runtime consume path
(``workers.shud_runtime.runtime``). It lives here as a single source of truth so the
two call sites cannot drift, and so importing it into the runtime does not drag in the
whole orchestrator chain module.
"""

from __future__ import annotations

from datetime import UTC, datetime


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def check_three_way_time_consistency(
    *,
    snapshot_valid_time: datetime | None,
    ic_header_minute_time: float | None,
    run_start_time: datetime | None,
) -> str | None:
    """Verify snapshot valid_time, ``.cfg.ic`` header minute-time, and run start agree.

    All three must equal ``T_{N+1}`` (to whole-minute resolution). Returns a human
    reason string on mismatch (a recorded blocker), or None when consistent. Inputs
    that are unavailable (None) are skipped so partial metadata is not a false blocker.
    """

    times: list[tuple[str, int]] = []
    if snapshot_valid_time is not None:
        times.append(("snapshot_valid_time", round(_ensure_utc(snapshot_valid_time).timestamp() / 60.0)))
    if ic_header_minute_time is not None:
        times.append(("ic_header_minute_time", round(float(ic_header_minute_time))))
    if run_start_time is not None:
        times.append(("run_start_time", round(_ensure_utc(run_start_time).timestamp() / 60.0)))
    if len(times) < 2:
        return None
    reference_name, reference_minute = times[0]
    for name, minute in times[1:]:
        if minute != reference_minute:
            return (
                f"warm-start time mismatch: {name}={minute} != {reference_name}={reference_minute} "
                "(minutes since epoch); restart at the wrong time is a blocker."
            )
    return None
