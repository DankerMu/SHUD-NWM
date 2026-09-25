"""Strict-identity field comparison shared by the production-closure evidence lanes.

A forecast cycle is identified by its UTC cycle hour. The readonly DB route smoke
and the two-node E2E evidence lane both compare ``cycle_time`` through
``cycle_time_identity_matches``, so ``2026-09-24T12:00:00Z``,
``2026-09-24T12:00:00+00:00``, ``2026-09-24T20:00:00+08:00`` and ``2026092412``
all name the same cycle in both lanes. A value that does not parse matches only
its byte-identical spelling.
"""

from __future__ import annotations

from datetime import UTC, datetime


def cycle_time_identity_matches(observed: str, expected: str) -> bool:
    if observed == expected:
        return True
    observed_normalized = normalized_cycle_time_identity(observed)
    expected_normalized = normalized_cycle_time_identity(expected)
    return bool(observed_normalized and expected_normalized and observed_normalized == expected_normalized)


def normalized_cycle_time_identity(value: str) -> str | None:
    candidate = value.strip()
    try:
        if len(candidate) == 10 and candidate.isdigit():
            parsed = datetime.strptime(candidate, "%Y%m%d%H").replace(tzinfo=UTC)
        else:
            iso_candidate = f"{candidate[:-1]}+00:00" if candidate.endswith("Z") else candidate
            parsed = datetime.fromisoformat(iso_candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
    except ValueError:
        return None
    return parsed.strftime("%Y%m%d%H")
