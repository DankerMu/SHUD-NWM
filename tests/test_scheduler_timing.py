"""Regression tests for services/orchestrator/scheduler_timing.

These tests currently cover only the finding-driven regression that guards
against Slurm wall-clock silently leaking into ``pass.python_time_ms``.
The rest of SUB-5 (tasks 3.1-3.16) lands as separate work; do not add
unrelated coverage here without expanding the SUB-5 wiring.
"""

from __future__ import annotations

import time

import pytest

from services.orchestrator.scheduler_timing import (
    SCHEDULER_PASS_TIMING_SCHEMA_VERSION,
    SchedulerPassTiming,
    StageSpan,
)


def test_add_slurm_wait_ms_removed_from_stage_span_surface() -> None:
    """The delta-only ``add_slurm_wait_ms`` accumulator MUST NOT exist.

    Removing it eliminates the footgun where a caller records Slurm
    wall-clock without endpoints, which the pass-level union-of-intervals
    cannot see; ``pass.python_time_ms`` would then absorb Slurm wait
    time in violation of spec.md §Attribution direct-measured.
    """

    assert not hasattr(StageSpan, "add_slurm_wait_ms"), (
        "add_slurm_wait_ms must not be reintroduced: it silently leaks "
        "slurm_wait into pass.python_time_ms; callers must use "
        "add_slurm_wait_interval so the pass-level union sees the wait."
    )


def test_pass_slurm_wait_reflects_stage_intervals() -> None:
    """A Slurm-wait interval recorded on a stage MUST bubble up to the pass union.

    Regression for finding C1 on PR #870: the previous delta-only
    ``add_slurm_wait_ms`` never appended to the shared interval list, so
    a stage-level 200 ms Slurm wait would appear only inside the stage
    record while ``pass.slurm_wait_ms`` stayed at 0 ms and the pass
    ``python_time_ms`` absorbed the wall-clock silently.  With the
    interval-only API, the pass union MUST reflect the recorded interval.
    """

    timing = SchedulerPassTiming(pass_id="scheduler_test_deadbeef01ab", level="stage")
    with timing.pass_span():
        with timing.stage_span("legacy_analysis", source_id="src", cycle_id="cyc") as stage:
            assert isinstance(stage, StageSpan)
            # Simulate a Slurm wait that runs during the stage: record the
            # start-of-interval offset now, sleep the actual wait, then
            # record the end offset.  The pass timeline (ms from pass
            # entry) is the shared clock.
            start_ms = timing._ms_from_pass_entry()
            time.sleep(0.2)
            end_ms = timing._ms_from_pass_entry()
            stage.add_slurm_wait_interval(start_ms, end_ms)
        # Give the pass a small tail so pass.total_wall_ms > interval end.
        time.sleep(0.02)

    evidence = timing.finalize_evidence(status="submitted")

    assert evidence["schema_version"] == SCHEDULER_PASS_TIMING_SCHEMA_VERSION
    pass_block = evidence["pass"]
    tolerance_ms = 15.0
    assert pass_block["slurm_wait_ms"] >= 200.0 - tolerance_ms, (
        f"pass.slurm_wait_ms={pass_block['slurm_wait_ms']!r} did not absorb "
        "the stage-recorded ~200 ms Slurm-wait interval; the union of "
        "intervals is not seeing intervals appended via StageSpan."
    )
    # And symmetrically: python_time_ms must NOT have absorbed the wait.
    assert pass_block["python_time_ms"] < pass_block["total_wall_ms"] - (200.0 - tolerance_ms), (
        f"pass.python_time_ms={pass_block['python_time_ms']!r} still "
        "contains Slurm wall-clock; the interval did not reach the "
        "pass-level union."
    )
    # And the sum invariant must hold within ±5 ms (no invariant_violations).
    assert "invariant_violations" not in evidence, evidence.get("invariant_violations")


def test_pass_slurm_wait_is_zero_without_intervals() -> None:
    """A pass with no direct-measured Slurm interval MUST report ``slurm_wait_ms == 0``.

    This guards the counter-scenario for the removal: after deleting the
    delta accumulator, a caller that opens a stage without touching
    ``add_slurm_wait_interval`` must see an all-python pass, not spurious
    Slurm attribution.
    """

    timing = SchedulerPassTiming(pass_id="scheduler_test_deadbeef02ab", level="stage")
    with timing.pass_span():
        with timing.stage_span("legacy_analysis", source_id="src", cycle_id="cyc"):
            time.sleep(0.02)

    evidence = timing.finalize_evidence(status="submitted")
    pass_block = evidence["pass"]
    assert pass_block["slurm_wait_ms"] == pytest.approx(0.0, abs=1e-6)
    assert pass_block["python_time_ms"] == pytest.approx(pass_block["total_wall_ms"], abs=5.0)
