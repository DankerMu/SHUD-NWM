from __future__ import annotations

import os
from collections.abc import Sequence


def env_cycle_hours_utc(name: str, default: Sequence[int]) -> tuple[int, ...]:
    value = os.getenv(name)
    if value is None:
        return normalize_cycle_hours_utc(default, field_name=name)
    return parse_cycle_hours_utc(value, name)


def parse_cycle_hours_utc(value: str, name: str) -> tuple[int, ...]:
    if value == "":
        raise ValueError(f"{name} must contain at least one UTC cycle hour")
    parsed: list[int] = []
    for raw_token in value.split(","):
        token = raw_token.strip()
        if token == "":
            raise ValueError(f"{name} must not contain empty cycle hour tokens")
        try:
            hour = int(token)
        except ValueError as error:
            raise ValueError(f"{name} must contain integer UTC cycle hours") from error
        parsed.append(hour)
    return normalize_cycle_hours_utc(parsed, field_name=name)


def normalize_cycle_hours_utc(
    value: Sequence[int],
    *,
    field_name: str = "cycle_hours_utc",
) -> tuple[int, ...]:
    hours: set[int] = set()
    try:
        raw_hours = iter(value)
    except TypeError as error:
        raise ValueError(f"{field_name} must contain integer UTC cycle hours") from error
    for raw_hour in raw_hours:
        if isinstance(raw_hour, bool) or not isinstance(raw_hour, int):
            raise ValueError(f"{field_name} must contain integer UTC cycle hours")
        if raw_hour < 0 or raw_hour > 23:
            raise ValueError(f"{field_name} must only contain values in 0..23")
        hours.add(raw_hour)
    if not hours:
        raise ValueError(f"{field_name} must contain at least one UTC cycle hour")
    return tuple(sorted(hours))
