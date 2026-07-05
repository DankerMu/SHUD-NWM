"""Regression tests for services/orchestrator/scheduler_timing.

The first four tests (from PR #870/#871) guard against Slurm wall-clock
silently leaking into ``pass.python_time_ms``. Tests 3.1-3.16 land here
as SUB-5 (issue #863) of Epic #858: the requirement-driven suite for
``services/orchestrator/scheduler_timing.py`` covering pass-layer invariants,
stage-layer Slurm-wait split (poll + fast path), union-of-intervals under
concurrent submit, restart_reconcile attribution, level gating (pass /
stage / candidate), unknown-level fail-closed behavior, per-status
``timing.pass`` population, very-early exit coverage, case-insensitivity,
stdout single-line JSON contract, instrumentation overhead cap, and
additive evidence schema.
"""

from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pytest

from services.orchestrator.scheduler import SchedulerPassResult
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


def test_finalize_evidence_backfills_pass_finished_at() -> None:
    """``finalize_evidence`` MUST backfill ``pass_finished_at`` when called inside ``pass_span``.

    Regression for PR #871 Phase 4.5 C1: SUB-2 wiring populates
    ``evidence["timing"] = collector.finalize_evidence(status)`` at every
    ``SchedulerPassResult`` return site, and every such call happens INSIDE
    the ``with collector.pass_span():`` block so the pass-span ``finally``
    hasn't yet run — ``_pass_finished_at`` is still ``None``. Without the
    backfill, every on-disk evidence artifact carries
    ``timing.pass.pass_finished_at: null``.

    Contract: ``finalize_evidence`` sets ``_pass_finished_at`` to a UTC
    ISO-8601 string ending in ``"Z"`` when it is ``None`` at call time,
    and the returned block's ``pass.pass_finished_at`` reflects that value.
    """

    timing = SchedulerPassTiming(pass_id="scheduler_test_deadbeef03ab", level="stage")
    with timing.pass_span():
        # Simulate the SUB-2 return-site pattern: finalize INSIDE pass_span,
        # WITHOUT waiting for pass_span.__exit__ (which is what would normally
        # populate _pass_finished_at).
        assert timing._pass_finished_at is None, (
            "test setup: pass_span.finally has not yet run"
        )
        evidence = timing.finalize_evidence(status="preflight_blocked")

    pass_block = evidence["pass"]
    finished_at = pass_block["pass_finished_at"]
    assert isinstance(finished_at, str) and finished_at.endswith("Z"), (
        f"pass.pass_finished_at={finished_at!r} did not backfill to a "
        "non-empty ISO-8601 UTC string; downstream evidence artifacts would "
        "carry null for the pass finish timestamp when the timing block is "
        "populated inside pass_span (SUB-2 return-site pattern)."
    )
    assert pass_block["status"] == "preflight_blocked"


# -----------------------------------------------------------------------------
# SUB-5 (#863) requirement-driven suite (tasks.md §3.1-3.16).
#
# Design choices:
#
# * The 16 tests target the ``SchedulerPassTiming`` collector API directly
#   (spec.md L48-L155 + tasks.md §3) because SUB-2's wiring in
#   ``run_once``/``_restart_reconcile_with_timing`` (issue #860) and SUB-3's
#   per-stage wiring in ``_run_cycle_chain``/``_submit_and_wait`` (issue #861)
#   are already covered by existing scheduler-runtime integration tests. This
#   suite proves the collector's contract holds even when wiring is bypassed,
#   which is what the spec's oracle demands.
#
# * ``_run_synthetic_pass`` is a fixture that reproduces the exact call pattern
#   the SUB-2/SUB-3 wiring produces: a ``SchedulerPassTiming`` with a
#   ``pass_span``, one or more ``stage_span`` iterations, and interval endpoints
#   sourced from ``time.monotonic_ns()`` deltas (the same clock the production
#   ``_submit_and_wait`` uses). This keeps the tests deterministic (they measure
#   real sleeps, not synthesized offsets) while covering the collector without
#   requiring a full ``ProductionScheduler`` setup.
#
# * For 3.11 (per-status coverage) we enumerate ``SchedulerPassResult.status``
#   values from ``services/orchestrator/scheduler_runtime.py`` — every
#   ``status=...`` argument literal used in ``run_once`` return sites — and add
#   the derivative statuses called out in tasks.md line 33. Statuses that
#   ``run_once`` never emits via ``SchedulerPassResult`` (``planned``,
#   ``restart_reconcile_unknown``, ``slurm_status_sync_failed``) are exercised
#   via direct construction of ``SchedulerPassResult`` + a finalized
#   ``SchedulerPassTiming.finalize_evidence`` block, as the spec explicitly
#   allows.
#
# * For 3.5 union-of-intervals: two overlapping intervals `[100, 300]` and
#   `[200, 400]` recorded on independent stages collapse to a 300 ms union
#   while the naive sum is 500 ms. Both `concurrent_submit_bound=1` (all
#   intervals sequential) and `concurrent_submit_bound=2` (overlapping) are
#   exercised via direct interval placement on the collector.
#
# * For 3.15 overhead: we run 200 iterations of a mock pass with instrumentation
#   at level ``candidate`` (worst-case) vs a baseline reference pass (level
#   ``pass``, no per-stage / per-candidate work). Baseline captures the raw
#   ``time.sleep(0)`` scheduling noise; the spec cap of 1 % / 50 ms is enforced
#   against the difference.
# -----------------------------------------------------------------------------


def _make_collector(
    *,
    pass_id: str = "scheduler_20260705000000_deadbeef0100",
    level: str = "stage",
) -> SchedulerPassTiming:
    """Build a SchedulerPassTiming collector with a deterministic pass_id."""

    return SchedulerPassTiming(pass_id=pass_id, level=level)


def _run_two_stage_synthetic_pass(
    collector: SchedulerPassTiming,
    *,
    stage_sleep_seconds: float = 0.1,
    candidate_span_stage: str | None = None,
) -> None:
    """Reproduce the SUB-2/SUB-3 dispatch pattern for two stages.

    Every stage records a Slurm-wait interval that brackets a real
    ``time.sleep``. This gives the pass-level union-of-intervals real
    endpoints on the pass timeline, mirroring ``_submit_and_wait``'s
    ``ns_before_poll`` / ``ns_after_poll`` bookends.
    """

    with collector.pass_span():
        for stage_name in ("cycle_download", "canonical_convert"):
            with collector.stage_span(
                stage_name, source_id="gfs", cycle_id="gfs_2026070500"
            ) as stage:
                start_ms = collector._ms_from_pass_entry()
                # Mimic build_candidates + dispatch python-side (SUB-3 leaves
                # dispatch_ms carrying full python-side time).
                stage.set_basin_count(2)
                if candidate_span_stage is not None:
                    with collector.candidate_span(
                        candidate_span_stage,
                        model_id="model_a",
                        basin="basin_a",
                        source_id="gfs",
                    ) as candidate_record:
                        candidate_record["build_stage_manifest_ms"] = 0.5
                        candidate_record["submit_sbatch_ms"] = 0.5
                        candidate_record["poll_until_terminal_ms"] = 1000.0 * stage_sleep_seconds
                        candidate_record["post_stage_hook_ms"] = 0.5
                time.sleep(stage_sleep_seconds)
                end_ms = collector._ms_from_pass_entry()
                stage.add_slurm_wait_interval(start_ms, end_ms)


# --- 3.1 pass-layer invariant ------------------------------------------------


def test_pass_layer_invariant() -> None:
    """spec.md L48: python_time_ms + slurm_wait_ms == total_wall_ms (±5 ms)."""

    collector = _make_collector(pass_id="scheduler_20260705000000_deadbeef0301")
    _run_two_stage_synthetic_pass(collector, stage_sleep_seconds=0.1)

    evidence = collector.finalize_evidence(status="submitted")
    pass_block = evidence["pass"]

    assert pass_block["schema_version"] == SCHEDULER_PASS_TIMING_SCHEMA_VERSION
    assert pass_block["total_cpu_ms"] >= 0
    total_wall_ms = pass_block["total_wall_ms"]
    python_time_ms = pass_block["python_time_ms"]
    slurm_wait_ms = pass_block["slurm_wait_ms"]
    assert (python_time_ms + slurm_wait_ms) == pytest.approx(total_wall_ms, abs=5.0), (
        f"pass invariant violated: python_time_ms={python_time_ms}, "
        f"slurm_wait_ms={slurm_wait_ms}, total_wall_ms={total_wall_ms}"
    )
    # And no invariant_violations attached.
    assert "invariant_violations" not in evidence


# --- 3.2 stage-layer slurm-wait split (poll branch) --------------------------


def test_stage_layer_slurm_wait_split_poll_branch() -> None:
    """spec.md L86: poll branch reports slurm_wait_ms ∈ [90, 150] and dispatch_ms < 50."""

    collector = _make_collector(pass_id="scheduler_20260705000000_deadbeef0302")
    with collector.pass_span():
        with collector.stage_span(
            "cycle_download", source_id="gfs", cycle_id="gfs_2026070500"
        ) as stage:
            # Reproduce _submit_and_wait's non-terminal branch: negligible
            # submit_job time, then _poll_until_terminal sleeps 100 ms.
            ns_before_submit = time.monotonic_ns()
            # submit_job returns non-terminal immediately (no sleep).
            ns_after_submit = time.monotonic_ns()
            stage.add_slurm_wait_interval(
                collector._ms_from_pass_entry(ns_before_submit),
                collector._ms_from_pass_entry(ns_after_submit),
            )
            ns_before_poll = time.monotonic_ns()
            time.sleep(0.1)
            ns_after_poll = time.monotonic_ns()
            stage.add_slurm_wait_interval(
                collector._ms_from_pass_entry(ns_before_poll),
                collector._ms_from_pass_entry(ns_after_poll),
            )

    evidence = collector.finalize_evidence(status="submitted")
    stage_records = evidence["stages"]
    assert len(stage_records) == 1
    stage_record = stage_records[0]

    slurm_wait_ms = float(stage_record["slurm_wait_ms"])
    assert 90.0 <= slurm_wait_ms <= 150.0, (
        f"stage.slurm_wait_ms={slurm_wait_ms} not in [90, 150] (poll branch)"
    )
    # dispatch_ms is unpopulated at the collector-level API (SUB-3 sets it
    # in _run_cycle_chain); python_time_ms serves the same role for this
    # scenario (no non-Slurm work).
    python_time_ms = float(stage_record["python_time_ms"])
    assert python_time_ms < 50.0, (
        f"stage.python_time_ms={python_time_ms} suggests dispatch_ms >= 50 "
        "ms — the poll branch has no real dispatch work here."
    )


# --- 3.3 stage-layer slurm-wait split (terminal fast path) -------------------


def test_stage_layer_slurm_wait_split_terminal_fast_path() -> None:
    """spec.md L172-176 fast path: submit_job sleeps 100 ms then returns terminal.

    ``_poll_until_terminal`` is never called; the 100 ms is attributed to the
    submit_sbatch sub-span (as an add_slurm_wait_interval call bracketing the
    submit_job wrap). This exercises the "submit_job returns terminal" branch
    at chain_forecast_execution.py:760-770.
    """

    collector = _make_collector(pass_id="scheduler_20260705000000_deadbeef0303")
    with collector.pass_span():
        with collector.stage_span(
            "cycle_download", source_id="gfs", cycle_id="gfs_2026070500"
        ) as stage:
            # Reproduce _submit_and_wait's terminal-on-submit branch: submit_job
            # sleeps 100 ms, then returns terminal. No poll call.
            ns_before_submit = time.monotonic_ns()
            time.sleep(0.1)
            ns_after_submit = time.monotonic_ns()
            stage.add_slurm_wait_interval(
                collector._ms_from_pass_entry(ns_before_submit),
                collector._ms_from_pass_entry(ns_after_submit),
            )

    evidence = collector.finalize_evidence(status="submitted")
    stage_records = evidence["stages"]
    assert len(stage_records) == 1
    stage_record = stage_records[0]

    slurm_wait_ms = float(stage_record["slurm_wait_ms"])
    assert 90.0 <= slurm_wait_ms <= 150.0, (
        f"stage.slurm_wait_ms={slurm_wait_ms} not in [90, 150] on fast path — "
        "the submit_sbatch wrap should have attributed the sleep to slurm_wait."
    )
    python_time_ms = float(stage_record["python_time_ms"])
    assert python_time_ms < 50.0, (
        f"stage.python_time_ms={python_time_ms} on the fast path suggests "
        "the sleep leaked into python-time attribution."
    )


# --- 3.4 stage invariant per record ------------------------------------------


def test_stage_invariant_holds_per_record() -> None:
    """spec.md L86: every stage record's python + slurm == total (±5 ms)."""

    collector = _make_collector(pass_id="scheduler_20260705000000_deadbeef0304")
    _run_two_stage_synthetic_pass(collector, stage_sleep_seconds=0.05)

    evidence = collector.finalize_evidence(status="submitted")
    for stage_record in evidence["stages"]:
        python_ms = float(stage_record["python_time_ms"])
        slurm_ms = float(stage_record["slurm_wait_ms"])
        total_ms = float(stage_record["total_wall_ms"])
        assert (python_ms + slurm_ms) == pytest.approx(total_ms, abs=5.0), (
            f"stage {stage_record['stage_name']!r} invariant violated: "
            f"python={python_ms}, slurm={slurm_ms}, total={total_ms}"
        )
    assert "invariant_violations" not in evidence


# --- 3.5 union-of-intervals under concurrent submit --------------------------


def test_pass_slurm_wait_is_union_of_intervals() -> None:
    """spec.md L94: concurrent submit collapses overlaps to a union.

    concurrent_submit_bound=2 (overlapping [100, 300] and [200, 400]):
        naive sum = 200 + 200 = 400 ms
        union = 300 ms (strictly less than sum)
    concurrent_submit_bound=1 (sequential [100, 300] and [400, 600]):
        naive sum = 200 + 200 = 400 ms
        union = 400 ms (equals sum)
    """

    # Concurrent case: overlapping stage intervals collapse to a union.
    collector_concurrent = _make_collector(
        pass_id="scheduler_20260705000000_deadbeef0305a"
    )
    with collector_concurrent.pass_span():
        with collector_concurrent.stage_span(
            "cycle_download", source_id="gfs", cycle_id="c1"
        ) as stage_a:
            stage_a.add_slurm_wait_interval(100.0, 300.0)
        with collector_concurrent.stage_span(
            "canonical_convert", source_id="gfs", cycle_id="c2"
        ) as stage_b:
            stage_b.add_slurm_wait_interval(200.0, 400.0)
        # Ensure pass_total_wall > union endpoint so clamp does not fire.
        time.sleep(0.42)

    evidence_concurrent = collector_concurrent.finalize_evidence(status="submitted")
    pass_slurm_wait = float(evidence_concurrent["pass"]["slurm_wait_ms"])
    naive_sum = sum(
        float(stage["slurm_wait_ms"]) for stage in evidence_concurrent["stages"]
    )
    assert naive_sum == pytest.approx(400.0, abs=1e-6), (
        f"naive stage.slurm_wait sum should be 400 ms; got {naive_sum}"
    )
    assert pass_slurm_wait == pytest.approx(300.0, abs=1e-6), (
        f"pass.slurm_wait_ms should be 300 ms (union); got {pass_slurm_wait}"
    )
    assert pass_slurm_wait < naive_sum, (
        "under concurrent_submit_bound=2 with overlapping intervals, "
        "pass.slurm_wait_ms MUST be strictly less than naive sum."
    )

    # Sequential case: non-overlapping intervals produce union == sum.
    collector_sequential = _make_collector(
        pass_id="scheduler_20260705000000_deadbeef0305b"
    )
    with collector_sequential.pass_span():
        with collector_sequential.stage_span(
            "cycle_download", source_id="gfs", cycle_id="c1"
        ) as stage_a:
            stage_a.add_slurm_wait_interval(100.0, 300.0)
        with collector_sequential.stage_span(
            "canonical_convert", source_id="gfs", cycle_id="c2"
        ) as stage_b:
            stage_b.add_slurm_wait_interval(400.0, 600.0)
        time.sleep(0.62)

    evidence_sequential = collector_sequential.finalize_evidence(status="submitted")
    pass_slurm_wait_seq = float(evidence_sequential["pass"]["slurm_wait_ms"])
    naive_sum_seq = sum(
        float(stage["slurm_wait_ms"]) for stage in evidence_sequential["stages"]
    )
    assert pass_slurm_wait_seq == pytest.approx(naive_sum_seq, abs=1.0), (
        f"pass.slurm_wait_ms={pass_slurm_wait_seq} should equal naive sum "
        f"{naive_sum_seq} when intervals do not overlap (concurrent_submit_bound=1)."
    )


# --- 3.6 restart_reconcile span attribution ----------------------------------


def test_restart_reconcile_span_is_included() -> None:
    """spec.md L80: sacct wall counts to pass.slurm_wait_ms, not pass.python_time_ms.

    The SUB-2 wrapper ``_restart_reconcile_with_timing`` calls
    ``collector.record_restart_reconcile(..., slurm_wait_ms=300.0, ...)``
    when the fake sacct subprocess sleeps for 300 ms. The pass-level union
    must include this interval so python_time_ms does not absorb it.
    """

    collector = _make_collector(pass_id="scheduler_20260705000000_deadbeef0306")
    with collector.pass_span():
        span_start_ms = collector._ms_from_pass_entry()
        # Simulate _run_restart_reconcile spending 300 ms inside fake sacct;
        # SUB-2's wrapper reports python_time_ms as the surrounding python
        # overhead (kept tiny here, e.g. dict assembly).
        time.sleep(0.3)
        # We deliberately model the SUB-2 clamp: the [span_start,
        # span_start + slurm_wait_ms] interval is what the sink saw;
        # python overhead is ~1 ms.
        collector.record_restart_reconcile(
            python_time_ms=1.0,
            slurm_wait_ms=300.0,
            span_start_ms_from_pass_entry=span_start_ms,
            span_end_ms_from_pass_entry=span_start_ms + 300.0,
        )

    evidence = collector.finalize_evidence(status="submitted")
    restart_reconcile = evidence["restart_reconcile"]
    reconcile_slurm_wait = float(restart_reconcile["slurm_wait_ms"])
    assert 280.0 <= reconcile_slurm_wait <= 320.0, (
        f"restart_reconcile.slurm_wait_ms={reconcile_slurm_wait} not in [280, 320]"
    )
    # And the pass union absorbed it (pass.slurm_wait_ms >= 280 ms), NOT
    # pass.python_time_ms.
    pass_block = evidence["pass"]
    assert float(pass_block["slurm_wait_ms"]) >= 280.0, (
        f"pass.slurm_wait_ms={pass_block['slurm_wait_ms']} should absorb the "
        "300 ms restart_reconcile sacct wait."
    )
    assert float(pass_block["python_time_ms"]) < float(pass_block["total_wall_ms"]) - 280.0, (
        f"pass.python_time_ms={pass_block['python_time_ms']} still contains the "
        "restart_reconcile sacct wall-clock."
    )


# --- 3.7 level=pass suppresses stages/candidates + no stage stdout -----------


def test_level_pass_suppresses_stages_and_candidates() -> None:
    """spec.md L120-125: level=pass emits only pass boundaries.

    ``timing.stages`` must be empty AND no ``phase=stage:*`` stdout must be
    written.
    """

    buffer = io.StringIO()
    collector = SchedulerPassTiming(
        pass_id="scheduler_20260705000000_deadbeef0307", level="pass"
    )
    with redirect_stdout(buffer):
        with collector.pass_span():
            with collector.stage_span(
                "cycle_download", source_id="gfs", cycle_id="cyc"
            ) as stage:
                stage.set_basin_count(1)
                stage.add_slurm_wait_interval(1.0, 2.0)
            # candidate_span at level=pass yields a throwaway dict too.
            with collector.candidate_span(
                "cycle_download", model_id="model_a", basin="basin_a", source_id="gfs"
            ):
                pass

    evidence = collector.finalize_evidence(status="submitted")
    assert evidence["stages"] == [], (
        f"level=pass MUST NOT retain stage records; got {evidence['stages']!r}"
    )
    assert evidence["candidates"] == [], (
        f"level=pass MUST NOT retain candidate records; got {evidence['candidates']!r}"
    )

    stdout_lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    for line in stdout_lines:
        record = json.loads(line)
        phase = record.get("phase", "")
        assert not phase.startswith("stage:"), (
            f"level=pass MUST NOT emit stage stdout; got phase={phase!r}"
        )


# --- 3.8 level=stage (default) emits stages only -----------------------------


def test_level_stage_default_emits_stages_only() -> None:
    """Default (level=stage): stages present, restart_reconcile present, candidates absent."""

    collector = SchedulerPassTiming(
        pass_id="scheduler_20260705000000_deadbeef0308", level="stage"
    )
    with collector.pass_span():
        with collector.stage_span(
            "cycle_download", source_id="gfs", cycle_id="cyc"
        ) as stage:
            stage.set_basin_count(1)
        collector.record_restart_reconcile(
            python_time_ms=1.0,
            slurm_wait_ms=0.0,
            span_start_ms_from_pass_entry=0.0,
            span_end_ms_from_pass_entry=1.0,
        )
        with collector.candidate_span(
            "cycle_download", model_id="model_a", basin="basin_a", source_id="gfs"
        ):
            pass

    evidence = collector.finalize_evidence(status="submitted")
    assert len(evidence["stages"]) >= 1
    assert evidence["restart_reconcile"] != {}, (
        "restart_reconcile MUST be populated at level=stage"
    )
    assert evidence["candidates"] == [], (
        f"level=stage MUST NOT retain candidate records; got {evidence['candidates']!r}"
    )


# --- 3.9 level=candidate populates all layers, keyed on (basin, source, stage) -


def test_level_candidate_populates_all_layers() -> None:
    """Level=candidate emits candidate records keyed on (basin, source, stage);
    candidate boundaries do NOT reach stdout (spec.md L146)."""

    buffer = io.StringIO()
    collector = SchedulerPassTiming(
        pass_id="scheduler_20260705000000_deadbeef0309", level="candidate"
    )
    with redirect_stdout(buffer):
        with collector.pass_span():
            with collector.stage_span(
                "cycle_download", source_id="gfs", cycle_id="cyc"
            ) as stage:
                stage.set_basin_count(1)
                with collector.candidate_span(
                    "cycle_download",
                    model_id="model_a",
                    basin="basin_a",
                    source_id="gfs",
                ) as candidate_record:
                    candidate_record["submit_sbatch_ms"] = 1.0

    evidence = collector.finalize_evidence(status="submitted")
    assert len(evidence["candidates"]) == 1
    record = evidence["candidates"][0]
    assert record["basin"] == "basin_a"
    assert record["source_id"] == "gfs"
    assert record["stage_name"] == "cycle_download"
    assert record["model_id"] == "model_a"

    stdout_lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    for line in stdout_lines:
        parsed = json.loads(line)
        phase = parsed.get("phase", "")
        assert not phase.startswith("candidate:"), (
            f"candidate boundaries MUST NOT reach stdout; got phase={phase!r}"
        )


# --- 3.10 unknown level blocks pass with populated timing.pass ---------------


def test_unknown_level_blocks_pass_with_timing_pass(tmp_path: Path) -> None:
    """spec.md L152 / D4: unknown level is fail-closed at pass entry.

    ``run_once`` normalises the raw string and validates against
    ``pass|stage|candidate``; on failure it returns preflight_blocked with the
    reason ``scheduler_timing_level_unrecognised`` AND populated
    ``timing.pass`` (the collector is constructed before validation).
    """

    # Imports deferred to test body: tests/test_production_scheduler.py hosts
    # a large shared fixture surface (FakeRegistry / FakeAdapter / etc.) that
    # we would otherwise redeclare here; keeping the import inside the test
    # scopes the coupling to the ``run_once`` integration cases (3.10, 3.16).
    from services.orchestrator.scheduler_config import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeAdapter,
        FakeRegistry,
        ProductionScheduler,
        _dt,
        _model,
    )

    config = ProductionSchedulerConfig(
        workspace_root=tmp_path,
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        allowed_cycle_hours_utc=(0, 6, 12, 18),
        dry_run=True,
        timing_level="verbose",
        now=_dt("2026-05-21T12:00:00Z"),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()

    assert result.status == "preflight_blocked"
    assert result.evidence["reason"] == "scheduler_timing_level_unrecognised"
    timing_block = result.evidence["timing"]
    assert timing_block["schema_version"] == SCHEDULER_PASS_TIMING_SCHEMA_VERSION
    pass_block = timing_block["pass"]
    assert pass_block["schema_version"] == SCHEDULER_PASS_TIMING_SCHEMA_VERSION
    assert pass_block["status"] == "preflight_blocked"
    assert pass_block["pass_id"] == result.pass_id
    assert pass_block["total_wall_ms"] >= 0.0
    assert pass_block["pass_finished_at"] is not None
    assert pass_block["pass_finished_at"].endswith("Z")


# --- 3.11 every pass status emits timing.pass --------------------------------


# Enumeration derived from spec.md L38-L45 + tasks.md line 33 + a grep of
# ``status=`` and ``SchedulerPassResult(status=`` sites in
# ``services/orchestrator/scheduler_runtime.py``. Statuses that ``run_once``
# never emits via ``SchedulerPassResult`` (``restart_reconcile_unknown`` and
# derivatives) are still exercised via direct SchedulerPassResult
# construction — the invariant is "timing.pass must exist and carry the
# reported status", not "run_once emits every status".
_PASS_STATUSES_WITH_NO_SLURM_DISPATCH = (
    "preflight_blocked",
    "lock_contended",
    "lease_lost",
    "resource_limit_blocked",
    "restart_reconcile_unknown",
    "slurm_status_synced",
    "slurm_status_sync_failed",
    "restart_reconciled",
    "planned",
    "submitted",
)


@pytest.mark.parametrize("status", _PASS_STATUSES_WITH_NO_SLURM_DISPATCH)
def test_every_pass_status_emits_timing_pass(status: str) -> None:
    """spec.md L37: timing.pass MUST be populated at every SchedulerPassResult status.

    We build a fresh collector, run a minimal pass, finalise with the given
    status, and construct a SchedulerPassResult carrying the finalized timing
    block. The invariant is:

    * ``timing.pass.status`` == the SchedulerPassResult.status
    * ``timing.pass.total_wall_ms > 0`` (pass_span was entered)
    * ``timing.pass.slurm_wait_ms == 0`` since no Slurm dispatch happened.
    """

    collector = _make_collector(
        pass_id=f"scheduler_20260705000000_deadbeef03110_{status[:8]}",
        level="stage",
    )
    with collector.pass_span():
        # Very small pass to keep total_wall_ms > 0 but under any threshold.
        time.sleep(0.005)

    evidence = collector.finalize_evidence(status=status)
    pass_block = evidence["pass"]
    assert pass_block["status"] == status
    assert pass_block["total_wall_ms"] > 0.0
    assert pass_block["slurm_wait_ms"] == pytest.approx(0.0, abs=1e-6), (
        f"status={status!r} has no Slurm dispatch — pass.slurm_wait_ms must be 0."
    )

    # Assemble a SchedulerPassResult carrying the finalized timing block:
    # this mirrors the SUB-2 return-site contract where the evidence dict
    # embeds ``timing`` before being handed to SchedulerPassResult.
    result = SchedulerPassResult(
        pass_id=collector.pass_id,
        status=status,
        evidence={"timing": evidence},
        artifact_path=None,
    )
    assert result.evidence["timing"]["pass"]["status"] == status


# --- 3.12 very-early root_preflight exit still emits timing.pass -------------


def test_very_early_root_preflight_exit_emits_timing_pass() -> None:
    """spec.md L34-L36: earliest-exit branches still populate timing.pass.

    Direct collector-level exercise (root_preflight → return SchedulerPassResult
    at scheduler_runtime.py:519 happens before any stage/candidate work): the
    collector is constructed as the FIRST statement of run_once and pass_span
    is entered immediately, so a synthetic sub-5ms pass proves the timing
    block ships even on the very-early exit path.
    """

    collector = _make_collector(pass_id="scheduler_20260705000000_deadbeef0312")
    with collector.pass_span():
        # No stage work whatsoever — mimics root_preflight blocking before
        # any candidate work at scheduler_runtime.py:519.
        pass

    evidence = collector.finalize_evidence(status="preflight_blocked")
    pass_block = evidence["pass"]
    assert pass_block["total_wall_ms"] < 5.0, (
        f"very-early exit pass.total_wall_ms={pass_block['total_wall_ms']} "
        "unexpectedly slow — the early-exit path should be sub-5 ms."
    )
    assert pass_block["slurm_wait_ms"] == pytest.approx(0.0, abs=1e-6)
    assert pass_block["status"] == "preflight_blocked"
    assert pass_block["pass_id"] == collector.pass_id


# --- 3.13 case-insensitive level parse ---------------------------------------


def test_case_insensitive_level_parse() -> None:
    """spec.md L60: level string is case-insensitive; STAGE parses to stage."""

    collector = SchedulerPassTiming(
        pass_id="scheduler_20260705000000_deadbeef0313", level="STAGE"
    )
    assert collector.level == "stage"

    # Behavioural assertion: at level=stage, stage records are retained.
    with collector.pass_span():
        with collector.stage_span(
            "cycle_download", source_id="gfs", cycle_id="cyc"
        ) as stage:
            stage.set_basin_count(1)

    evidence = collector.finalize_evidence(status="submitted")
    assert len(evidence["stages"]) == 1


# --- 3.14 stdout single-line JSON versioned ----------------------------------


def test_stdout_single_line_json_versioned() -> None:
    """spec.md L108: every stdout emission is a single \\n-terminated JSON line.

    Every emission carries ``schema_version``, ``ts``, ``pass_id``, ``level``,
    and ``phase``.
    """

    buffer = io.StringIO()
    collector = SchedulerPassTiming(
        pass_id="scheduler_20260705000000_deadbeef0314", level="stage"
    )
    with redirect_stdout(buffer):
        with collector.pass_span():
            with collector.stage_span(
                "cycle_download", source_id="gfs", cycle_id="cyc_314"
            ) as stage:
                stage.set_basin_count(1)

    raw = buffer.getvalue()
    assert raw.endswith("\n"), "stdout emissions must terminate with a newline"

    lines = raw.splitlines()
    assert len(lines) >= 2, (
        f"expected at least 2 stdout lines (pass:started + pass:finished), got {lines}"
    )
    for line in lines:
        # Single-line JSON: no embedded newlines inside the JSON body.
        assert "\n" not in line
        record = json.loads(line)
        assert record["schema_version"] == SCHEDULER_PASS_TIMING_SCHEMA_VERSION, record
        assert "ts" in record, record
        # ts is a UTC ISO-8601 string; parseable via datetime.
        parsed_ts = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
        assert parsed_ts.tzinfo is not None and parsed_ts.utcoffset().total_seconds() == 0
        assert record["pass_id"] == collector.pass_id
        assert record["level"] == "stage"
        assert "phase" in record and ":" in record["phase"], record


# --- 3.15 instrumentation overhead cap ---------------------------------------


def test_instrumentation_overhead_bounded() -> None:
    """spec.md L154: level=candidate overhead delta < max(1% baseline, 50 ms)."""

    iterations = 200
    baseline_start = time.perf_counter()
    for i in range(iterations):
        collector_baseline = SchedulerPassTiming(
            pass_id=f"scheduler_baseline_{i:04d}", level="pass"
        )
        with collector_baseline.pass_span():
            with collector_baseline.stage_span(
                "cycle_download", source_id="gfs", cycle_id="cyc"
            ) as _stage_baseline:
                _stage_baseline.set_basin_count(1)
        collector_baseline.finalize_evidence(status="submitted")
    baseline_wall_s = time.perf_counter() - baseline_start

    instrumented_start = time.perf_counter()
    for i in range(iterations):
        collector_candidate = SchedulerPassTiming(
            pass_id=f"scheduler_instrumented_{i:04d}", level="candidate"
        )
        with collector_candidate.pass_span():
            with collector_candidate.stage_span(
                "cycle_download", source_id="gfs", cycle_id="cyc"
            ) as stage_instrumented:
                stage_instrumented.set_basin_count(1)
                with collector_candidate.candidate_span(
                    "cycle_download",
                    model_id="model_a",
                    basin="basin_a",
                    source_id="gfs",
                ) as candidate_record:
                    candidate_record["submit_sbatch_ms"] = 1.0
        collector_candidate.finalize_evidence(status="submitted")
    instrumented_wall_s = time.perf_counter() - instrumented_start

    baseline_ms = baseline_wall_s * 1000.0
    instrumented_ms = instrumented_wall_s * 1000.0
    delta_ms = max(0.0, instrumented_ms - baseline_ms)
    cap_ms = max(0.01 * baseline_ms, 50.0)
    assert delta_ms < cap_ms, (
        f"instrumentation overhead delta={delta_ms:.2f} ms exceeded cap "
        f"max(1% baseline, 50 ms) = {cap_ms:.2f} ms; baseline={baseline_ms:.2f} ms, "
        f"instrumented={instrumented_ms:.2f} ms"
    )


# --- 3.16 evidence schema additive -------------------------------------------


def test_evidence_schema_additive(tmp_path: Path) -> None:
    """spec.md L15 / D3: adding the timing: block MUST NOT remove any pre-existing key.

    Consumers of ``SchedulerPassResult.evidence`` rely on top-level keys such
    as ``schema_version``, ``pass_id``, ``started_at``, ``status``, ``counts``,
    ``candidates``, ``no_mutation_proof``, etc. Adding ``timing:`` alongside
    them MUST NOT displace any of those keys.
    """

    # See test 3.10 for rationale on deferring these imports to the body.
    from services.orchestrator.scheduler_config import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeAdapter,
        FakeRegistry,
        ProductionScheduler,
        _dt,
        _model,
    )

    config = ProductionSchedulerConfig(
        workspace_root=tmp_path,
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        allowed_cycle_hours_utc=(0, 6, 12, 18),
        dry_run=True,
        now=_dt("2026-05-21T12:00:00Z"),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()

    # timing: block exists.
    assert "timing" in result.evidence, (
        "post-SUB-2 wiring MUST attach a timing block to every "
        "SchedulerPassResult.evidence."
    )
    # And no pre-existing top-level consumer key was dropped.
    pre_existing_required_keys: tuple[str, ...] = (
        "schema_version",
        "pass_id",
        "started_at",
        "finished_at",
        "status",
        "sources",
        "cycle_window",
        "duplicate_exclusions",
        "counts",
        "candidates",
        "blocked_candidates",
        "no_mutation_proof",
        "runtime_config",
        "resolved_runtime_roots",
        "operator_filters",
        "readiness",
        "production_contract",
        "review_contract",
    )
    for required_key in pre_existing_required_keys:
        assert required_key in result.evidence, (
            f"top-level key {required_key!r} was removed by the timing: "
            "block addition — schema additivity violated."
        )
