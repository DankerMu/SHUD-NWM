"""Per-group timing evidence for cold-residency observations."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from packages.common.compressed_chunk_cold_residency import ResidencyGroup
from packages.common.compressed_chunk_cold_runtime_catalog import WindowParity

Clock = Callable[[], float]

TIMING_FIELDS = (
    "total_ms",
    "heap_lock_wait_ms",
    "revalidation_ms",
    "shell_move_ms",
    "decompress_ms",
    "recompress_ms",
    "commit_ms",
    "fresh_reconciliation_ms",
)
MUTATION_FIELDS = TIMING_FIELDS[1:]


def default_clock() -> float:
    return time.monotonic()


def elapsed_ms(started: float, finished: float) -> int:
    return max(0, int(round((finished - started) * 1000.0)))


def timing_payload(
    *,
    total_ms: int | None,
    heap_lock_wait_ms: int | None = None,
    revalidation_ms: int | None = None,
    shell_move_ms: int | None = None,
    decompress_ms: int | None = None,
    recompress_ms: int | None = None,
    commit_ms: int | None = None,
    fresh_reconciliation_ms: int | None = None,
) -> dict[str, int | None]:
    return {
        "total_ms": total_ms,
        "heap_lock_wait_ms": heap_lock_wait_ms,
        "revalidation_ms": revalidation_ms,
        "shell_move_ms": shell_move_ms,
        "decompress_ms": decompress_ms,
        "recompress_ms": recompress_ms,
        "commit_ms": commit_ms,
        "fresh_reconciliation_ms": fresh_reconciliation_ms,
    }


def inspect_timing_payload(started: float, finished: float) -> dict[str, int | None]:
    return timing_payload(total_ms=elapsed_ms(started, finished))


class StageTimer:
    """Measure named mutation stages; unreached stages stay null."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._started = clock()
        self._open: str | None = None
        self._open_at: float | None = None
        self._ms: dict[str, int | None] = {field: None for field in MUTATION_FIELDS}

    def start(self, field: str) -> None:
        if field not in self._ms:
            raise ValueError(f"unknown timing field {field}")
        self.stop()
        self._open = field
        self._open_at = self._clock()

    def stop(self) -> None:
        if self._open is None or self._open_at is None:
            return
        self._ms[self._open] = elapsed_ms(self._open_at, self._clock())
        self._open = None
        self._open_at = None

    def as_dict(self) -> dict[str, int | None]:
        self.stop()
        return timing_payload(total_ms=elapsed_ms(self._started, self._clock()), **self._ms)


@dataclass(frozen=True)
class MoveObservation:
    outcome: str
    reconciliation: str
    plan_kind: str
    shell_sql_executed: bool
    before: ResidencyGroup
    after: ResidencyGroup | None
    before_parity: WindowParity | None
    after_parity: WindowParity | None
    intermediate: Mapping[str, Any]
    capacity: Mapping[str, Any] | None
    error_class: str | None = None
    stage: str | None = None
    reason: str | None = None
    commit_ack_lost: bool = False
    replayed: bool = False
    timing: Mapping[str, int | None] | None = None


def build_move_observation(
    *,
    outcome: str,
    reconciliation: str,
    plan_kind: str,
    shell_sql_executed: bool,
    before: ResidencyGroup,
    after: ResidencyGroup | None,
    before_parity: WindowParity | None,
    after_parity: WindowParity | None,
    intermediate: Mapping[str, Any] | None = None,
    capacity: Mapping[str, Any] | None = None,
    error_class: str | None = None,
    stage: str | None = None,
    reason: str | None = None,
    commit_ack_lost: bool = False,
    timing: Mapping[str, int | None] | None = None,
) -> MoveObservation:
    return MoveObservation(
        outcome=outcome,
        reconciliation=reconciliation,
        plan_kind=plan_kind,
        shell_sql_executed=shell_sql_executed,
        before=before,
        after=after,
        before_parity=before_parity,
        after_parity=after_parity,
        intermediate=dict(intermediate or {}),
        capacity=capacity,
        error_class=error_class,
        stage=stage,
        reason=reason,
        commit_ack_lost=commit_ack_lost,
        replayed=False,
        timing=None if timing is None else dict(timing),
    )
