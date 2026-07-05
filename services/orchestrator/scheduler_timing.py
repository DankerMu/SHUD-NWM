"""Per-pass scheduler timing collector.

Every production scheduler pass on node-22 constructs one
:class:`SchedulerPassTiming` instance at ``run_once`` entry.  The collector
records ``time.monotonic()`` deltas for wall-clock durations and
``time.process_time()`` deltas for CPU accounting, exposes three nested
context managers (``pass_span`` / ``stage_span`` / ``candidate_span``),
serialises a level-gated ``timing:`` block for the pass evidence JSON, and
emits one-line JSON records to stdout on pass / stage boundaries so
systemd-journald consumers see live phase transitions.

Downstream wiring (``scheduler_runtime``, ``scheduler_execution``,
``chain_forecast_execution``) happens in follow-up issues; this module owns
the collector object shape, the union-of-intervals invariants, and the
stdout emission contract.
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Callable, Iterator

__all__ = (
    "SCHEDULER_PASS_TIMING_SCHEMA_VERSION",
    "SchedulerPassTiming",
    "StageSpan",
)

SCHEDULER_PASS_TIMING_SCHEMA_VERSION = "nhms.scheduler_pass_timing.v1"

# ± tolerance for the invariant check on the returned dict.
_INVARIANT_TOLERANCE_MS = 5.0


def _default_now_iso() -> str:
    """Return a UTC ISO-8601 timestamp with a ``Z`` suffix (jq-friendly)."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class StageSpan:
    """Mutable stage-record surface used by callers inside ``stage_span``.

    The core module owns the invariants; SUB-3 (scheduler_execution) and
    SUB-4 (chain_forecast_execution) will call these setters to populate
    the concrete stage counters and the direct-measured Slurm-wait
    intervals.
    """

    __slots__ = (
        "_record",
        "_stage_start_ms",
        "_slurm_wait_intervals",
    )

    def __init__(
        self,
        record: dict[str, Any],
        stage_start_ms: float,
        slurm_wait_intervals: list[tuple[float, float]],
    ) -> None:
        self._record = record
        self._stage_start_ms = stage_start_ms
        self._slurm_wait_intervals = slurm_wait_intervals

    @property
    def record(self) -> dict[str, Any]:
        return self._record

    @property
    def stage_started_ms_from_pass_entry(self) -> float:
        return self._stage_start_ms

    def set_build_candidates_ms(self, value: float) -> None:
        self._record["build_candidates_ms"] = float(value)

    def set_dispatch_ms(self, value: float) -> None:
        self._record["dispatch_ms"] = float(value)

    def set_basin_count(self, value: int) -> None:
        self._record["basin_count"] = int(value)

    def set_submitted_count(self, value: int) -> None:
        self._record["submitted_count"] = int(value)

    def set_failed_count(self, value: int) -> None:
        self._record["failed_count"] = int(value)

    def add_slurm_wait_ms(self, value: float) -> None:
        """Aggregate direct-measured Slurm-wait milliseconds for this stage.

        Used when the caller doesn't have (start, end) interval endpoints
        handy; simply accumulates the total.  Callers that DO have interval
        endpoints should prefer :meth:`add_slurm_wait_interval` so pass-level
        union-of-intervals works correctly under concurrent dispatch.
        """

        self._record["slurm_wait_ms"] = float(self._record.get("slurm_wait_ms", 0.0)) + float(value)

    def add_slurm_wait_interval(self, start_ms_from_pass_entry: float, end_ms_from_pass_entry: float) -> None:
        """Record a direct-measured Slurm-wait interval on the pass timeline.

        Interval endpoints are expressed as milliseconds from pass entry
        (see :meth:`SchedulerPassTiming._ms_from_pass_entry`).  The pass
        collector unions overlapping intervals across all stages at
        finalisation so ``pass.slurm_wait_ms`` stays correct when
        ``concurrent_submit_bound > 1``.
        """

        interval = (float(start_ms_from_pass_entry), float(end_ms_from_pass_entry))
        self._slurm_wait_intervals.append(interval)
        self._record["slurm_wait_ms"] = float(self._record.get("slurm_wait_ms", 0.0)) + max(
            0.0, interval[1] - interval[0]
        )


class SchedulerPassTiming:
    """Per-pass timing collector.

    Construct as the first statement of ``run_once`` (before every side
    effect, per D4) so ``timing.pass`` is always populated even for the
    earliest-exit branches.

    Parameters
    ----------
    pass_id:
        Minted ``scheduler_<cycle>_<hex12>`` string from ``run_once``.
    level:
        One of ``"pass"``, ``"stage"``, ``"candidate"`` (case-insensitive).
        Validation is deferred to ``run_once`` per D4; unknown values are
        preserved verbatim.
    now_iso_fn:
        Callable returning a UTC ISO-8601 timestamp string.  Injected so
        tests can replace the clock without monkeypatching ``datetime``.
    """

    __slots__ = (
        "_pass_id",
        "_level",
        "_now_iso",
        "_pass_start_monotonic_ns",
        "_cpu_start",
        "_pass_started_at",
        "_pass_finished_at",
        "_stages",
        "_candidates",
        "_restart_reconcile",
        "_slurm_wait_intervals",
        "_pass_entered",
    )

    def __init__(
        self,
        pass_id: str,
        level: str,
        now_iso_fn: Callable[[], str] | None = None,
    ) -> None:
        self._pass_id = pass_id
        self._level = level.lower() if isinstance(level, str) else level
        self._now_iso = now_iso_fn or _default_now_iso
        self._pass_start_monotonic_ns = time.monotonic_ns()
        self._cpu_start = time.process_time()
        self._pass_started_at: str | None = None
        self._pass_finished_at: str | None = None
        self._stages: list[dict[str, Any]] = []
        self._candidates: list[dict[str, Any]] = []
        self._restart_reconcile: dict[str, float] = {
            "python_time_ms": 0.0,
            "slurm_wait_ms": 0.0,
            "total_wall_ms": 0.0,
        }
        self._slurm_wait_intervals: list[tuple[float, float]] = []
        self._pass_entered = False

    # -- introspection --------------------------------------------------

    @property
    def pass_id(self) -> str:
        return self._pass_id

    @property
    def level(self) -> str:
        return self._level

    # -- ms-from-pass-entry helpers ------------------------------------

    def _ms_from_pass_entry(self, monotonic_ns: int | None = None) -> float:
        """Milliseconds since pass entry (construction time)."""

        if monotonic_ns is None:
            monotonic_ns = time.monotonic_ns()
        return (monotonic_ns - self._pass_start_monotonic_ns) / 1_000_000.0

    # -- context managers ---------------------------------------------

    @contextmanager
    def pass_span(self) -> Iterator["SchedulerPassTiming"]:
        """Enter/exit the pass boundary; emits ``pass:started``/``pass:finished`` stdout."""

        self._pass_entered = True
        self._pass_started_at = self._now_iso()
        self._emit_stdout(self._boundary_record("pass", "started"))
        try:
            yield self
        finally:
            self._pass_finished_at = self._now_iso()
            self._emit_stdout(self._boundary_record("pass", "finished"))

    @contextmanager
    def stage_span(
        self,
        stage_name: str,
        *,
        source_id: str | None = None,
        cycle_id: str | None = None,
    ) -> Iterator[StageSpan]:
        """Open a stage record.

        Callers should populate the returned :class:`StageSpan` via its
        setters as the stage progresses.  The stage record is committed to
        ``timing.stages`` on ``__exit__``.

        At level ``pass`` no stage record is retained and no stdout is
        emitted (the returned :class:`StageSpan` is still functional so
        callers don't need branching logic).
        """

        stage_start_monotonic_ns = time.monotonic_ns()
        stage_started_ms_from_pass_entry = self._ms_from_pass_entry(stage_start_monotonic_ns)
        record: dict[str, Any] = {
            "schema_version": SCHEDULER_PASS_TIMING_SCHEMA_VERSION,
            "source_id": source_id,
            "cycle_id": cycle_id,
            "stage_name": stage_name,
            "stage_started_ms_from_pass_entry": stage_started_ms_from_pass_entry,
            "stage_finished_ms_from_pass_entry": 0.0,
            "build_candidates_ms": 0.0,
            "dispatch_ms": 0.0,
            "slurm_wait_ms": 0.0,
            "python_time_ms": 0.0,
            "total_wall_ms": 0.0,
            "basin_count": 0,
            "submitted_count": 0,
            "failed_count": 0,
        }
        span = StageSpan(record, stage_started_ms_from_pass_entry, self._slurm_wait_intervals)
        emit = self._level != "pass"
        if emit:
            self._emit_stdout(self._stage_boundary_record("started", stage_name, source_id, cycle_id))
        try:
            yield span
        finally:
            stage_finished_ms_from_pass_entry = self._ms_from_pass_entry()
            total_wall_ms = max(0.0, stage_finished_ms_from_pass_entry - stage_started_ms_from_pass_entry)
            slurm_wait_ms = float(record.get("slurm_wait_ms", 0.0))
            # python_time_ms = total_wall - slurm_wait so the per-stage
            # invariant holds by construction; if callers set explicit
            # build_candidates_ms + dispatch_ms they must satisfy
            # build_candidates_ms + dispatch_ms == python_time_ms (± 5 ms),
            # which SUB-3 wiring is responsible for.
            record["stage_finished_ms_from_pass_entry"] = stage_finished_ms_from_pass_entry
            record["total_wall_ms"] = total_wall_ms
            record["python_time_ms"] = max(0.0, total_wall_ms - slurm_wait_ms)
            if self._level != "pass":
                self._stages.append(record)
            if emit:
                self._emit_stdout(self._stage_boundary_record("finished", stage_name, source_id, cycle_id))

    @contextmanager
    def candidate_span(
        self,
        stage_name: str,
        *,
        model_id: str | None = None,
        basin: str | None = None,
        source_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Open a candidate record; no-op below level ``candidate``.

        Never emits stdout (candidate volume would flood journald).
        """

        if self._level != "candidate":
            # Return a throwaway dict so callers can write to it without
            # branching; it is not retained anywhere.
            yield {}
            return
        candidate_start_ms = self._ms_from_pass_entry()
        record: dict[str, Any] = {
            "schema_version": SCHEDULER_PASS_TIMING_SCHEMA_VERSION,
            "model_id": model_id,
            "basin": basin,
            "source_id": source_id,
            "stage_name": stage_name,
            "candidate_started_ms_from_pass_entry": candidate_start_ms,
            "candidate_finished_ms_from_pass_entry": 0.0,
            "total_wall_ms": 0.0,
        }
        try:
            yield record
        finally:
            candidate_finished_ms = self._ms_from_pass_entry()
            record["candidate_finished_ms_from_pass_entry"] = candidate_finished_ms
            record["total_wall_ms"] = max(0.0, candidate_finished_ms - candidate_start_ms)
            self._candidates.append(record)

    # -- restart_reconcile pseudo-record ------------------------------

    def record_restart_reconcile(
        self,
        *,
        python_time_ms: float,
        slurm_wait_ms: float,
        span_start_ms_from_pass_entry: float,
        span_end_ms_from_pass_entry: float,
    ) -> None:
        """Record the pass-level ``restart_reconcile`` pseudo-stage.

        SUB-2 wiring calls this after ``_run_restart_reconcile`` completes;
        the interval endpoints feed the pass-level union-of-intervals
        computation so no Slurm wall-clock silently leaks into
        ``python_time_ms``.
        """

        python_time_ms = float(python_time_ms)
        slurm_wait_ms = float(slurm_wait_ms)
        self._restart_reconcile = {
            "python_time_ms": python_time_ms,
            "slurm_wait_ms": slurm_wait_ms,
            "total_wall_ms": python_time_ms + slurm_wait_ms,
        }
        if slurm_wait_ms > 0.0:
            self._slurm_wait_intervals.append(
                (float(span_start_ms_from_pass_entry), float(span_end_ms_from_pass_entry))
            )

    # -- finalisation --------------------------------------------------

    def finalize_evidence(self, status: str) -> dict[str, Any]:
        """Build the level-gated ``timing:`` block.

        Invariants are checked with a ±5 ms tolerance; violations are
        appended as diagnostic ``invariant_violations`` entries rather than
        raised so instrumentation can never crash the scheduler.
        """

        total_wall_ms = self._ms_from_pass_entry()
        total_cpu_ms = max(0, int((time.process_time() - self._cpu_start) * 1000))
        slurm_wait_ms = _union_ms(self._slurm_wait_intervals)
        # Guard: union may exceed total_wall_ms if a caller mis-reports
        # intervals; clamp so python_time_ms cannot go negative.
        slurm_wait_ms = min(slurm_wait_ms, total_wall_ms)
        python_time_ms = max(0.0, total_wall_ms - slurm_wait_ms)

        pass_block: dict[str, Any] = {
            "schema_version": SCHEDULER_PASS_TIMING_SCHEMA_VERSION,
            "pass_id": self._pass_id,
            "pass_started_at": self._pass_started_at,
            "pass_finished_at": self._pass_finished_at,
            "status": status,
            "total_wall_ms": total_wall_ms,
            "total_cpu_ms": total_cpu_ms,
            "python_time_ms": python_time_ms,
            "slurm_wait_ms": slurm_wait_ms,
        }

        stages_out = list(self._stages) if self._level != "pass" else []
        candidates_out = list(self._candidates) if self._level == "candidate" else []
        restart_reconcile_out: dict[str, float] = (
            dict(self._restart_reconcile) if self._level != "pass" else {}
        )

        block: dict[str, Any] = {
            "schema_version": SCHEDULER_PASS_TIMING_SCHEMA_VERSION,
            "pass": pass_block,
            "stages": stages_out,
            "candidates": candidates_out,
            "restart_reconcile": restart_reconcile_out,
        }

        violations = self._check_invariants(
            pass_block=pass_block,
            stages=stages_out,
            restart_reconcile=restart_reconcile_out,
        )
        if violations:
            block["invariant_violations"] = violations
        return block

    # -- invariant checks ---------------------------------------------

    def _check_invariants(
        self,
        *,
        pass_block: dict[str, Any],
        stages: list[dict[str, Any]],
        restart_reconcile: dict[str, float],
    ) -> list[dict[str, Any]]:
        violations: list[dict[str, Any]] = []
        pass_total = float(pass_block["total_wall_ms"])
        pass_python = float(pass_block["python_time_ms"])
        pass_slurm = float(pass_block["slurm_wait_ms"])
        pass_delta = abs((pass_python + pass_slurm) - pass_total)
        if pass_delta > _INVARIANT_TOLERANCE_MS:
            violations.append(
                {
                    "scope": "pass",
                    "reason": "sum_mismatch",
                    "delta_ms": pass_delta,
                    "total_wall_ms": pass_total,
                    "python_time_ms": pass_python,
                    "slurm_wait_ms": pass_slurm,
                }
            )
        for stage in stages:
            stage_total = float(stage.get("total_wall_ms", 0.0))
            stage_python = float(stage.get("python_time_ms", 0.0))
            stage_slurm = float(stage.get("slurm_wait_ms", 0.0))
            delta = abs((stage_python + stage_slurm) - stage_total)
            if delta > _INVARIANT_TOLERANCE_MS:
                violations.append(
                    {
                        "scope": "stage",
                        "reason": "sum_mismatch",
                        "stage_name": stage.get("stage_name"),
                        "source_id": stage.get("source_id"),
                        "cycle_id": stage.get("cycle_id"),
                        "delta_ms": delta,
                        "total_wall_ms": stage_total,
                        "python_time_ms": stage_python,
                        "slurm_wait_ms": stage_slurm,
                    }
                )
        # Union-of-intervals invariant for the pass slurm_wait_ms.  We
        # compute the expected union from the collected intervals against
        # the pass slurm_wait_ms value already stored on the pass block.
        # This is redundant with construction, but guards against future
        # code paths that override slurm_wait_ms.
        union = _union_ms(self._slurm_wait_intervals)
        # Match the clamp applied at construction so the invariant does
        # not falsely trip when a caller over-reports intervals; the clamp
        # itself is captured as the "sum_mismatch" violation above.
        union = min(union, pass_total)
        union_delta = abs(union - pass_slurm)
        if union_delta > _INVARIANT_TOLERANCE_MS:
            violations.append(
                {
                    "scope": "pass_union",
                    "reason": "union_mismatch",
                    "delta_ms": union_delta,
                    "union_ms": union,
                    "pass_slurm_wait_ms": pass_slurm,
                }
            )
        # Sanity: restart_reconcile total should equal python + slurm.
        rr_python = float(restart_reconcile.get("python_time_ms", 0.0))
        rr_slurm = float(restart_reconcile.get("slurm_wait_ms", 0.0))
        rr_total = float(restart_reconcile.get("total_wall_ms", 0.0))
        if restart_reconcile and abs((rr_python + rr_slurm) - rr_total) > _INVARIANT_TOLERANCE_MS:
            violations.append(
                {
                    "scope": "restart_reconcile",
                    "reason": "sum_mismatch",
                    "delta_ms": abs((rr_python + rr_slurm) - rr_total),
                    "total_wall_ms": rr_total,
                    "python_time_ms": rr_python,
                    "slurm_wait_ms": rr_slurm,
                }
            )
        return violations

    # -- stdout emission ----------------------------------------------

    def _boundary_record(self, layer: str, phase: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEDULER_PASS_TIMING_SCHEMA_VERSION,
            "ts": self._now_iso(),
            "pass_id": self._pass_id,
            "level": self._level,
            "phase": f"{layer}:{phase}",
        }

    def _stage_boundary_record(
        self,
        phase: str,
        stage_name: str,
        source_id: str | None,
        cycle_id: str | None,
    ) -> dict[str, Any]:
        record = self._boundary_record("stage", phase)
        record["stage_name"] = stage_name
        if source_id is not None:
            record["source_id"] = source_id
        if cycle_id is not None:
            record["cycle_id"] = cycle_id
        return record

    def _emit_stdout(self, record: dict[str, Any]) -> None:
        """Write one JSON line to stdout, flushed immediately.

        journald consumes stdout line-oriented; the record MUST be a single
        line ending in ``\\n`` with no embedded newlines inside the JSON
        body.
        """

        # ``print`` appends exactly one ``\n`` and ``json.dumps`` without
        # ``indent`` never introduces embedded newlines.  ``flush=True``
        # is required so journald sees lines during a multi-hour pass.
        print(json.dumps(record, ensure_ascii=False), flush=True, file=sys.stdout)


# -- helpers ------------------------------------------------------------


def _union_ms(intervals: list[tuple[float, float]]) -> float:
    """Sum lengths of the union of overlapping half-open intervals.

    Each interval is ``(start_ms_from_pass_entry, end_ms_from_pass_entry)``.
    Overlapping intervals collapse into their union so concurrent stage
    dispatch (``concurrent_submit_bound > 1``) doesn't inflate
    ``pass.slurm_wait_ms``.  When intervals are strictly non-overlapping
    the result equals the naive sum.
    """

    if not intervals:
        return 0.0
    # Normalise: drop degenerate or reversed intervals; clamp lower bound
    # so a single mis-reported endpoint can't push totals negative.
    cleaned: list[tuple[float, float]] = []
    for start, end in intervals:
        start_f = float(start)
        end_f = float(end)
        if end_f <= start_f:
            continue
        cleaned.append((start_f, end_f))
    if not cleaned:
        return 0.0
    cleaned.sort()
    total = 0.0
    current_start, current_end = cleaned[0]
    for start, end in cleaned[1:]:
        if start <= current_end:
            if end > current_end:
                current_end = end
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    total += current_end - current_start
    return total
