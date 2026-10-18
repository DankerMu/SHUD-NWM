"""Cycle-time normalization and warm-start time-consistency contract.

Public functions:

- ``ensure_utc`` -- coerce a datetime to timezone-aware UTC.
- ``parse_cycle_time`` -- parse ``YYYYMMDDHH`` or ISO-8601 cycle times as UTC.
- ``parse_cycle_date`` -- parse a cycle-discovery date input.
- ``format_cycle_time`` -- render a cycle time as ``YYYYMMDDHH``.
- ``cycle_id_for`` -- build the canonical ``<source>_<YYYYMMDDHH>`` cycle id.
- ``valid_time_for`` -- cycle time plus a forecast-hour offset.
- ``parse_resolution_segments`` -- parse a piecewise ``upto:step`` resolution spec.
- ``generate_segmented_forecast_hours`` -- expand resolution segments to lead hours.
- ``validate_forecast_hours`` -- validate caller-supplied lead hours.
- ``check_three_way_time_consistency`` -- warm-start snapshot / ``.cfg.ic`` / run-start check.
- ``utc_now`` -- the current time as a timezone-aware UTC datetime.
- ``parse_iso_utc`` -- parse an ISO-8601 timestamp string to a timezone-aware UTC datetime.
- ``format_iso_utc`` -- render a datetime as an ISO-8601 UTC string with a ``Z`` suffix.

The three-way (snapshot ``valid_time`` / native ``.cfg.ic`` header minute-time / run
``start_time``) equality check is used both at daemon selection time
(``services.orchestrator.chain``) and on the forecast-runtime consume path
(``workers.shud_runtime.runtime``). It lives here as a single source of truth so the
two call sites cannot drift, and so importing it into the runtime does not drag in the
whole orchestrator chain module.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from packages.common.source_identity import normalize_source_id


def ensure_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_cycle_time(value: str | datetime) -> datetime:
    """Parse YYYYMMDDHH or ISO-8601 cycle time values as UTC datetimes."""
    if isinstance(value, datetime):
        return ensure_utc(value)

    candidate = value.strip()
    if len(candidate) == 10 and candidate.isdigit():
        return datetime.strptime(candidate, "%Y%m%d%H").replace(tzinfo=UTC)

    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    return ensure_utc(datetime.fromisoformat(candidate))


def parse_cycle_date(value: str | date | datetime) -> date:
    """Parse a date input accepted by cycle discovery."""
    if isinstance(value, datetime):
        return ensure_utc(value).date()
    if isinstance(value, date):
        return value
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def format_cycle_time(value: str | datetime) -> str:
    return parse_cycle_time(value).strftime("%Y%m%d%H")


def cycle_id_for(source_id: str, cycle_time: str | datetime) -> str:
    source_id = normalize_source_id(source_id)
    return f"{source_id.lower()}_{format_cycle_time(cycle_time)}"


def valid_time_for(cycle_time: str | datetime, forecast_hour: int) -> datetime:
    return parse_cycle_time(cycle_time) + timedelta(hours=forecast_hour)


def parse_resolution_segments(spec: str | None) -> tuple[tuple[int, int], ...] | None:
    """Parse a piecewise forecast-resolution spec like "120:1,384:3".

    Each segment is ``upto_hour:step_hours`` (ascending upto), meaning forecast hours
    up to and including ``upto_hour`` use ``step_hours``. Segments may be separated by
    ``,`` or ``;`` (the latter survives Slurm env-export value filtering). Returns None
    for an empty spec so callers fall back to a single uniform step.
    """
    if not spec or not spec.strip():
        return None
    segments: list[tuple[int, int]] = []
    last_upto = -1
    for raw in re.split(r"[,;]", spec):
        token = raw.strip()
        if not token:
            continue
        try:
            upto_text, step_text = token.split(":")
            upto, step = int(upto_text), int(step_text)
        except ValueError as error:
            raise ValueError(f"Invalid forecast-resolution segment {token!r}; expected 'upto:step'.") from error
        if step <= 0:
            raise ValueError(f"Forecast-resolution step must be positive in segment {token!r}.")
        if upto <= last_upto:
            raise ValueError(f"Forecast-resolution segments must have strictly ascending upto: {token!r}.")
        segments.append((upto, step))
        last_upto = upto
    return tuple(segments) or None


def generate_segmented_forecast_hours(
    start_hour: int,
    end_hour: int,
    segments: tuple[tuple[int, int], ...],
) -> list[int]:
    """Generate non-uniform forecast hours from piecewise (upto, step) segments.

    Each segment emits hours on its own step grid within ``(previous_upto, upto]``,
    so e.g. GFS ``((120, 1), (384, 3))`` yields hourly 0..120 then 3-hourly 123..end.
    """
    hours: list[int] = []
    seen: set[int] = set()
    lower = start_hour
    for upto, step in segments:
        seg_hi = min(end_hour, upto)
        remainder = lower % step
        hour = lower if remainder == 0 else lower + (step - remainder)
        while hour <= seg_hi:
            if hour not in seen:
                seen.add(hour)
                hours.append(hour)
            hour += step
        lower = upto + 1
        if lower > end_hour:
            break
    return hours


def validate_forecast_hours(
    forecast_hours: list[int],
    *,
    source_id: str,
    min_hour: int,
    max_hour: int,
    step_hours: int,
    allowed_hours: set[int] | None = None,
) -> list[int]:
    """Validate caller-supplied lead hours before manifest path generation.

    When ``allowed_hours`` is provided each hour must be a member of that canonical
    set (supporting non-uniform native resolution); otherwise hours must align to a
    single uniform ``step_hours`` grid.
    """
    if step_hours <= 0:
        raise ValueError(f"{source_id} forecast-hour step must be positive.")

    normalized: list[int] = []
    seen: set[int] = set()
    for forecast_hour in forecast_hours:
        if not isinstance(forecast_hour, int) or isinstance(forecast_hour, bool):
            raise ValueError(f"{source_id} forecast hour must be an integer: {forecast_hour!r}")
        if forecast_hour in seen:
            raise ValueError(f"{source_id} forecast hours must be unique: {forecast_hour}")
        if forecast_hour < min_hour:
            raise ValueError(f"{source_id} forecast hour {forecast_hour} is below minimum {min_hour}.")
        if forecast_hour > max_hour:
            raise ValueError(f"{source_id} forecast hour {forecast_hour} exceeds maximum {max_hour}.")
        if allowed_hours is not None:
            if forecast_hour not in allowed_hours:
                raise ValueError(
                    f"{source_id} forecast hour {forecast_hour} is not in the native resolution schedule."
                )
        elif (forecast_hour - min_hour) % step_hours != 0:
            raise ValueError(f"{source_id} forecast hour {forecast_hour} is not aligned to {step_hours}h steps.")
        seen.add(forecast_hour)
        normalized.append(forecast_hour)
    return normalized


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


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Takes no input. The result is ``datetime.now(UTC)``: ``tzinfo is UTC`` and the
    microseconds are kept (not truncated).
    """
    return datetime.now(UTC)


def parse_iso_utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp string to a timezone-aware UTC datetime.

    Accepts a ``str`` that ``datetime.fromisoformat`` parses after surrounding
    whitespace is stripped and an uppercase ``Z`` is replaced by ``+00:00``, e.g.
    ``"2026-10-17T06:00:00Z"``, ``"2026-10-17T14:00:00+08:00"``,
    ``"2026-10-17T06:00:00.123456"`` or the date-only ``"2026-10-17Z"`` (that day
    00:00 UTC). A naive result is taken to be UTC; an offset-aware result is
    converted to UTC. The returned datetime always has ``tzinfo is UTC``; ``None``
    is never returned.

    Raises ``TypeError`` for a non-``str`` input and ``ValueError`` for an empty or
    unparseable string, including a lowercase ``z`` suffix and the compact
    ``YYYYMMDDHH`` cycle form (that one is ``parse_cycle_time``'s concept).
    """
    if not isinstance(text, str):
        raise TypeError(f"parse_iso_utc expects str, got {type(text).__name__}")
    return ensure_utc(datetime.fromisoformat(text.strip().replace("Z", "+00:00")))


def format_iso_utc(value: datetime) -> str:
    """Render a datetime as an ISO-8601 UTC string with a ``Z`` suffix.

    Accepts a ``datetime``; a naive value is taken to be UTC and an offset-aware
    value is converted to UTC. The output is ``YYYY-MM-DDTHH:MM:SSZ``, or
    ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` when the microseconds are non-zero (the
    default ``isoformat`` timespec). Raises ``TypeError`` for any non-``datetime``
    input, including ``None``.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"format_iso_utc expects datetime, got {type(value).__name__}")
    return ensure_utc(value).isoformat().replace("+00:00", "Z")
