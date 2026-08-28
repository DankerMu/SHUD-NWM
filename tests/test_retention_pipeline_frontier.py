"""Requirement-driven tests for retention respecting the pipeline frontier (#1307)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.orchestrator.retention import (
    PIPELINE_FRONTIER_EXEMPT_REASON,
    RetentionConfig,
    plan_retention,
    run_retention,
)
from tests.retention_test_helpers import (
    NOW,
    _cycle_name,
    _keys,
    _reasons,
    _seed_cycle,
    _write,
)

# ---------------------------------------------------------------------------
# Issue #1307 — retention respects the pipeline frontier.
#
# Reference points used throughout (NOW = 2026-06-03T12:00Z):
#   14-day cutoff                          -> 2026-05-20T12:00Z
#   window floor, lookback=168h, lag=6h    -> 2026-05-27T06:00Z  (> cutoff)
#   window floor, lookback=336h, lag=6h    -> 2026-05-20T06:00Z  (< cutoff)
# The first is the compliant steady-state config (168 + 6 + 2*6 <= 336), the
# second is the boundary config where the exemption gate actually bites.
# ---------------------------------------------------------------------------

FRONTIER_SOURCES = {"candidates", "skipped_in_flight", "window_floor"}


def _frontier_scheduler(
    tmp_path: Path,
    *,
    lookback_hours: int = 168,
    cycle_lag_hours: int = 6,
    sources: tuple[str, ...] = ("gfs",),
    adapters: dict | None = None,
    allowed_cycle_hours_utc: tuple[int, ...] = (0, 6, 12, 18),
):
    """Scheduler whose discovery knobs are pinned (not inherited from env)."""
    from services.orchestrator.scheduler import (
        ProductionScheduler,
        ProductionSchedulerConfig,
        _BlockedModelRegistry,
    )

    config = ProductionSchedulerConfig(
        workspace_root=str(tmp_path / "ws"),
        sources=sources,
        allowed_cycle_hours_utc=allowed_cycle_hours_utc,
        lookback_hours=lookback_hours,
        cycle_lag_hours=cycle_lag_hours,
    )
    return ProductionScheduler(
        config=config,
        registry=_BlockedModelRegistry(),
        adapters=adapters or {},
        active_repository=None,
    )


def _active_lower_bound(scheduler, started_at: datetime, **kwargs):
    from services.orchestrator import scheduler_runtime

    return scheduler_runtime._retention_active_lower_bound(scheduler, started_at, **kwargs)


def _skip_entry(reason: str, cycle_time: datetime | str) -> dict:
    """A skipped-candidate evidence dict in the shape the scheduler builds.

    ``cycle_time`` is a ``…Z`` string there (``scheduler_types.py:105-106``),
    which is exactly the parsing surface under test.
    """
    formatted = cycle_time if isinstance(cycle_time, str) else cycle_time.isoformat().replace("+00:00", "Z")
    return {"source_id": "gfs", "cycle_id": "gfs_x", "cycle_time": formatted, "reason": reason}


def _selected_candidate(cycle_time: datetime):
    """Stand-in for SchedulerCandidate: the helper reads cycle_time_utc only."""
    from types import SimpleNamespace

    return SimpleNamespace(cycle_time_utc=cycle_time, reason=None)


def test_catch_up_cycle_exempted_with_reason_distinct_from_not_yet_expired(tmp_path: Path) -> None:
    """[AC1] Aged-out but still in flight => exempt; both skip reasons coexist."""
    root = tmp_path / "object-store"
    in_flight = datetime(2026, 5, 18, 0, tzinfo=UTC)  # older than the 05-20T12:00Z cutoff
    collectable = datetime(2026, 5, 10, 0, tzinfo=UTC)  # older than cutoff AND below the bound
    fresh = datetime(2026, 6, 1, 0, tzinfo=UTC)  # not yet expired
    in_flight_keys = _seed_cycle(root, in_flight, run=True)
    collectable_keys = _seed_cycle(root, collectable, run=True)
    fresh_keys = _seed_cycle(root, fresh)

    result = run_retention(
        object_store_root=root,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=True, retention_days=14),
        active_lower_bound=in_flight,
        active_lower_bound_source="candidates",
    )

    reasons = _reasons(result.skipped)
    planned = _keys(result.planned)
    # (1) every artifact of the in-flight cycle is exempt, none is planned
    for key in in_flight_keys.values():
        assert reasons[key] == PIPELINE_FRONTIER_EXEMPT_REASON
        assert key not in planned
    # (2) the two skip reasons are distinguishable inside one plan
    assert reasons[fresh_keys["raw"]] == "within_retention_window"
    # (3) the gate is not a blanket amnesty: below-bound cycles still go
    for key in collectable_keys.values():
        assert key in planned
        assert key not in reasons
    # (4) receipt frontier block is complete and readable
    payload = result.to_dict()
    assert payload["frontier"] == {
        "active_lower_bound": "2026-05-18T00:00:00+00:00",
        "source": "candidates",
        "protected_count": 4,
    }


def test_frontier_exempt_entries_carry_no_size_bytes_and_are_never_size_scanned(
    monkeypatch, tmp_path: Path
) -> None:
    """[AC1/D4] Exemption is adjudicated before ``_dir_size``: no rglob+stat."""
    import services.orchestrator.retention as retention_mod

    root = tmp_path / "object-store"
    in_flight = datetime(2026, 5, 18, 0, tzinfo=UTC)
    collectable = datetime(2026, 5, 10, 0, tzinfo=UTC)
    in_flight_keys = _seed_cycle(root, in_flight, run=True)
    collectable_keys = _seed_cycle(root, collectable, run=True)

    scanned: list[str] = []
    real_dir_size = retention_mod._dir_size

    def recording_dir_size(path):
        scanned.append(Path(path).as_posix())
        return real_dir_size(path)

    monkeypatch.setattr(retention_mod, "_dir_size", recording_dir_size)

    result = plan_retention(
        object_store_root=root,
        cutoff=NOW - timedelta(days=14),
        retention_days=14,
        enabled=True,
        dry_run=True,
        active_lower_bound=in_flight,
        active_lower_bound_source="window_floor",
    )

    exempt = [entry for entry in result.skipped if entry["reason"] == PIPELINE_FRONTIER_EXEMPT_REASON]
    assert {entry["key"] for entry in exempt} == set(in_flight_keys.values())
    for entry in exempt:
        # ``root`` joined the shape in #1318 (identically named runs/<run_id>
        # on different roots must be distinguishable); it is a string already
        # held in memory, so the #1307 protection below is unaffected.
        assert set(entry) == {"key", "path", "cycle_time", "reason", "root"}
        assert "size_bytes" not in entry
        # the protected directory was never walked ...
        assert entry["path"] not in scanned
    # ... while the collectable ones were (the recorder itself is live)
    assert scanned
    assert {Path(entry["path"]).as_posix() for entry in result.planned} == set(scanned)
    assert {entry["key"] for entry in result.planned} == set(collectable_keys.values())


def test_terminal_cycle_still_deleted_with_unchanged_freed_bytes(tmp_path: Path) -> None:
    """[AC2] Below the bound => deleted exactly as before, same freed_bytes."""
    old = NOW - timedelta(days=20)
    baseline_root = tmp_path / "baseline"
    guarded_root = tmp_path / "guarded"
    baseline_keys = _seed_cycle(baseline_root, old, run=True)
    guarded_keys = _seed_cycle(guarded_root, old, run=True)

    baseline = run_retention(
        object_store_root=baseline_root,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )
    guarded = run_retention(
        object_store_root=guarded_root,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
        # all candidates terminal + outside the window: bound sits after the cycle
        active_lower_bound=NOW - timedelta(days=16),
        active_lower_bound_source="window_floor",
    )

    assert _keys(baseline.deleted) == set(baseline_keys.values())
    assert _keys(guarded.deleted) == set(guarded_keys.values())
    assert guarded.freed_bytes == baseline.freed_bytes
    assert guarded.freed_bytes > 0
    assert not any(entry["reason"] == PIPELINE_FRONTIER_EXEMPT_REASON for entry in guarded.skipped)
    assert guarded.to_dict()["frontier"]["protected_count"] == 0
    for key in guarded_keys.values():
        assert not (guarded_root / key).exists()


def test_steady_state_plan_is_identical_key_for_key(tmp_path: Path) -> None:
    """[AC3] Compliant config (168 + 6 + 2*6 <= 14*24) => zero drift."""
    root = tmp_path / "object-store"
    _seed_cycle(root, NOW - timedelta(days=20), run=True)
    _seed_cycle(root, NOW - timedelta(days=16))
    _seed_cycle(root, datetime(2026, 5, 19, 12, tzinfo=UTC))  # just past the cutoff
    # Inside the window, seeded on BOTH lanes: the run workspace is what makes
    # the run-lane adjudication order observable (an in-window run dir sits above
    # the bound, so a frontier-first order would relabel it).
    _seed_cycle(root, datetime(2026, 6, 1, 0, tzinfo=UTC), run=True)
    cutoff = NOW - timedelta(days=14)

    scheduler = _frontier_scheduler(tmp_path, lookback_hours=168, cycle_lag_hours=6)
    bound, source = _active_lower_bound(scheduler, NOW)
    assert (bound, source) == (datetime(2026, 5, 27, 6, tzinfo=UTC), "window_floor")
    assert bound > cutoff, "compliant config keeps the bound above the cutoff"

    def plan(active_lower_bound):
        return plan_retention(
            object_store_root=root,
            cutoff=cutoff,
            retention_days=14,
            enabled=True,
            dry_run=True,
            active_lower_bound=active_lower_bound,
            active_lower_bound_source=source,
        )

    baseline = plan(None)
    guarded = plan(bound)
    assert guarded.planned == baseline.planned  # key for key, size for size
    assert guarded.skipped == baseline.skipped
    assert guarded.to_dict()["frontier"]["protected_count"] == 0

    # Discriminating control: the parity above is not vacuous. An artificially
    # old bound must change the plan.
    drifted = plan(NOW - timedelta(days=21))
    assert drifted.planned != baseline.planned
    assert any(entry["reason"] == PIPELINE_FRONTIER_EXEMPT_REASON for entry in drifted.skipped)


def test_boundary_config_exempts_only_the_band_between_bound_and_cutoff(tmp_path: Path) -> None:
    """[AC3 boundary] lookback=336 + lag=6 opens a ~6h [bound, cutoff) band."""
    root = tmp_path / "object-store"
    cutoff = NOW - timedelta(days=14)
    scheduler = _frontier_scheduler(tmp_path, lookback_hours=336, cycle_lag_hours=6)
    bound, source = _active_lower_bound(scheduler, NOW)

    assert (bound, source) == (datetime(2026, 5, 20, 6, tzinfo=UTC), "window_floor")
    assert bound < cutoff, "boundary config puts the bound below the cutoff"
    assert cutoff - bound == timedelta(hours=6)

    # `started_at - 338h` floored to a source cycle boundary lands in the band.
    in_band = datetime(2026, 5, 20, 6, tzinfo=UTC)
    assert bound <= in_band < cutoff
    below_band = datetime(2026, 5, 20, 0, tzinfo=UTC)
    assert below_band < bound
    in_band_keys = _seed_cycle(root, in_band, run=True)
    below_band_keys = _seed_cycle(root, below_band, run=True)

    result = plan_retention(
        object_store_root=root,
        cutoff=cutoff,
        retention_days=14,
        enabled=True,
        dry_run=True,
        active_lower_bound=bound,
        active_lower_bound_source=source,
    )

    reasons = _reasons(result.skipped)
    planned = _keys(result.planned)
    # bidirectional: in-band exempt, below-band still collected
    for key in in_band_keys.values():
        assert reasons[key] == PIPELINE_FRONTIER_EXEMPT_REASON
        assert key not in planned
    for key in below_band_keys.values():
        assert key in planned
    assert result.to_dict()["frontier"]["protected_count"] == len(in_band_keys)


def test_failed_run_workspace_preserved_with_readable_shud_logs(tmp_path: Path) -> None:
    """[AC4] An in-flight run workspace survives; SHUD logs stay readable."""
    root = tmp_path / "object-store"
    cycle = datetime(2026, 5, 18, 0, tzinfo=UTC)
    run_key = f"runs/fcst_gfs_{_cycle_name(cycle)}_model_a"
    _write(root, f"{run_key}/shud_stdout.log", b"SHUD start; step 1\n")
    _write(root, f"{run_key}/shud_stderr.log", b"STATE_CHECKPOINTS_MISSING\n")
    _write(root, f"{run_key}/output/out.nc")

    result = run_retention(
        object_store_root=root,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
        active_lower_bound=cycle,
        active_lower_bound_source="skipped_in_flight",
    )

    assert _reasons(result.skipped)[run_key] == PIPELINE_FRONTIER_EXEMPT_REASON
    assert run_key not in _keys(result.planned)
    assert run_key not in _keys(result.deleted)
    # physical post-mortem evidence survives and is readable
    assert (root / run_key).is_dir()
    assert (root / run_key / "shud_stdout.log").read_bytes() == b"SHUD start; step 1\n"
    assert (root / run_key / "shud_stderr.log").read_bytes() == b"STATE_CHECKPOINTS_MISSING\n"


def test_without_bound_retention_stays_pure_wall_clock(tmp_path: Path) -> None:
    """[spec: no active lower bound] Default None => unchanged behaviour."""
    root = tmp_path / "object-store"
    keys = _seed_cycle(root, NOW - timedelta(days=20), run=True)

    result = plan_retention(
        object_store_root=root,
        cutoff=NOW - timedelta(days=14),
        retention_days=14,
        enabled=True,
        dry_run=True,
    )

    assert _keys(result.planned) == set(keys.values())
    assert result.to_dict()["frontier"] == {
        "active_lower_bound": None,
        "source": None,
        "protected_count": 0,
    }


def test_source_label_without_bound_is_not_recorded(tmp_path: Path) -> None:
    """A source label with no bound must not claim a bound was applied."""
    result = plan_retention(
        object_store_root=tmp_path / "missing",
        cutoff=NOW - timedelta(days=14),
        retention_days=14,
        enabled=True,
        dry_run=True,
        active_lower_bound=None,
        active_lower_bound_source="candidates",
    )

    assert result.to_dict()["frontier"]["source"] is None


def test_active_lower_bound_excludes_terminal_skip_reasons(tmp_path: Path) -> None:
    """[helper] Terminal skips do not hold the frontier back; in-flight ones do."""
    scheduler = _frontier_scheduler(tmp_path)
    terminal_reasons = (
        "completed_duplicate_pipeline",
        "terminal_hydro_success",
        "terminal_completed_cycle",
        "terminal_pipeline_success",
        "duplicate_candidate_identity",
    )
    very_old = datetime(2026, 5, 1, 0, tzinfo=UTC)
    in_flight = datetime(2026, 5, 10, 0, tzinfo=UTC)

    terminal_only, terminal_source = _active_lower_bound(
        scheduler,
        NOW,
        skipped_candidates=[_skip_entry(reason, very_old) for reason in terminal_reasons],
    )
    assert (terminal_only, terminal_source) == (datetime(2026, 5, 27, 6, tzinfo=UTC), "window_floor")

    mixed, mixed_source = _active_lower_bound(
        scheduler,
        NOW,
        skipped_candidates=[
            *[_skip_entry(reason, very_old) for reason in terminal_reasons],
            _skip_entry("active_slurm_job", in_flight),
        ],
    )
    assert (mixed, mixed_source) == (in_flight, "skipped_in_flight")


@pytest.mark.parametrize(
    "reason",
    [
        "active_slurm_job",
        "cancel_requested_active_slurm",
        "active_slurm_status_sync_deferred",
        "active_slurm_status_sync_failed",
        "active_duplicate_pipeline",
        "a_reason_invented_after_this_test_was_written",
    ],
)
def test_active_lower_bound_counts_non_terminal_and_unknown_reasons(tmp_path: Path, reason: str) -> None:
    """[helper] Everything outside the terminal set protects, unknown included."""
    scheduler = _frontier_scheduler(tmp_path)
    in_flight = datetime(2026, 5, 10, 0, tzinfo=UTC)

    bound, source = _active_lower_bound(
        scheduler,
        NOW,
        skipped_candidates=[_skip_entry(reason, in_flight)],
    )

    assert (bound, source) == (in_flight, "skipped_in_flight")


def test_active_lower_bound_skips_unparseable_skip_cycle_times(tmp_path: Path) -> None:
    """[helper] A malformed cycle_time string is skipped, never raised."""
    scheduler = _frontier_scheduler(tmp_path)
    in_flight = datetime(2026, 5, 10, 0, tzinfo=UTC)

    bound, source = _active_lower_bound(
        scheduler,
        NOW,
        skipped_candidates=[
            _skip_entry("active_slurm_job", "not-a-timestamp"),
            _skip_entry("active_slurm_job", ""),
            {"reason": "active_slurm_job"},
            _skip_entry("active_slurm_job", in_flight),
        ],
    )
    assert (bound, source) == (in_flight, "skipped_in_flight")

    only_bad, only_bad_source = _active_lower_bound(
        scheduler,
        NOW,
        skipped_candidates=[_skip_entry("active_slurm_job", "not-a-timestamp")],
    )
    assert (only_bad, only_bad_source) == (datetime(2026, 5, 27, 6, tzinfo=UTC), "window_floor")


def test_active_lower_bound_takes_min_across_sources_with_candidate_precedence(tmp_path: Path) -> None:
    """[helper] min() across the three sources; ties record candidates first."""
    scheduler = _frontier_scheduler(tmp_path)
    selected = datetime(2026, 5, 5, 0, tzinfo=UTC)
    blocked = datetime(2026, 5, 2, 0, tzinfo=UTC)
    skipped = datetime(2026, 5, 10, 0, tzinfo=UTC)

    assert _active_lower_bound(
        scheduler,
        NOW,
        candidates=[_selected_candidate(selected)],
        skipped_candidates=[_skip_entry("active_slurm_job", skipped)],
    ) == (selected, "candidates")

    assert _active_lower_bound(
        scheduler,
        NOW,
        candidates=[_selected_candidate(selected)],
        blocked_candidates=[_selected_candidate(blocked)],
        skipped_candidates=[_skip_entry("active_slurm_job", skipped)],
    ) == (blocked, "candidates")

    # tie between candidates and skipped -> candidates wins the label
    assert _active_lower_bound(
        scheduler,
        NOW,
        candidates=[_selected_candidate(skipped)],
        skipped_candidates=[_skip_entry("active_slurm_job", skipped)],
    ) == (skipped, "candidates")


def test_active_lower_bound_falls_back_to_window_floor_then_to_none(tmp_path: Path) -> None:
    """[helper] No candidate state => window floor; no window => None."""
    scheduler = _frontier_scheduler(tmp_path)
    assert _active_lower_bound(scheduler, NOW) == (datetime(2026, 5, 27, 6, tzinfo=UTC), "window_floor")

    sourceless = _frontier_scheduler(tmp_path, sources=())
    assert _active_lower_bound(sourceless, NOW) == (None, None)


def test_no_derivable_bound_degrades_to_the_current_wall_clock_plan(tmp_path: Path) -> None:
    """[helper] The None bound really is the pre-#1307 plan, key for key."""
    root = tmp_path / "object-store"
    _seed_cycle(root, NOW - timedelta(days=20), run=True)
    _seed_cycle(root, datetime(2026, 6, 1, 0, tzinfo=UTC))
    sourceless = _frontier_scheduler(tmp_path, sources=())
    bound, source = _active_lower_bound(sourceless, NOW)
    assert (bound, source) == (None, None)

    def plan(active_lower_bound, active_lower_bound_source):
        return plan_retention(
            object_store_root=root,
            cutoff=NOW - timedelta(days=14),
            retention_days=14,
            enabled=True,
            dry_run=True,
            active_lower_bound=active_lower_bound,
            active_lower_bound_source=active_lower_bound_source,
        )

    assert plan(bound, source).planned == plan(None, None).planned


@pytest.mark.parametrize(
    ("lookback_hours", "allowed_cycle_hours_utc", "expected_bound"),
    [
        # A multiple of the 6h grid: floor(18:00Z - 168h) is already on the grid,
        # so the OUTER floor is an identity here and cannot be observed.
        (168, (0, 6, 12, 18), datetime(2026, 5, 26, 18, tzinfo=UTC)),
        # NOT a multiple of the grid (nothing enforces one -- LOOKBACK_HOURS is a
        # free int): 18:00Z - 170h = 05-26T16:00Z, which only the OUTER floor
        # pulls down to 12:00Z. Dropping that floor would leave a 4h band at the
        # bottom of the discovery window unprotected (deletion-side drift).
        (170, (0, 6, 12, 18), datetime(2026, 5, 26, 12, tzinfo=UTC)),
        # A NON-default grid (the shipped 0,12 one): floor(03:17 - 6h) = 12:00Z,
        # 12:00Z - 170h = 05-26T10:00Z, floored onto {0,12} -> 05-26T00:00Z.
        # Recomputing without the config's allowed_cycle_hours_utc would fall
        # back to the gfs default {0,6,12,18} and yield 05-26T12:00Z -- a bound
        # 12h too high, i.e. a band discovery still reaches into but retention
        # would already consider collectable.
        (170, (0, 12), datetime(2026, 5, 26, 0, tzinfo=UTC)),
    ],
)
def test_window_floor_matches_discovery_double_floor_formula(
    tmp_path: Path,
    lookback_hours: int,
    allowed_cycle_hours_utc: tuple[int, ...],
    expected_bound: datetime,
) -> None:
    """[helper] The floor is discovery's, not the receipt's un-floored value.

    Independent oracle: run the real ``discover_cycles`` and capture the
    ``start_time`` it hands the source-window provider.
    """
    started_at = datetime(2026, 6, 3, 3, 17, tzinfo=UTC)
    scheduler = _frontier_scheduler(
        tmp_path,
        lookback_hours=lookback_hours,
        cycle_lag_hours=6,
        adapters={"gfs": object()},
        allowed_cycle_hours_utc=allowed_cycle_hours_utc,
    )
    captured: dict[str, datetime] = {}

    def capture_window(adapter, *, source_id: str, start_time: datetime, end_time: datetime):
        captured["start_time"] = start_time
        return []

    scheduler._discover_source_window = capture_window
    scheduler._discover_cycles(started_at, models=[])

    bound, source = _active_lower_bound(scheduler, started_at)

    # hand-computed per case above: floor(started_at - lag) on the case grid,
    # then floor(that - lookback) on the same grid
    assert bound == expected_bound
    assert source == "window_floor"
    # and it is exactly what discovery itself used
    assert bound == captured["start_time"]
    # the un-floored evidence value (started_at - lag - lookback) sits later
    # here: using it would leave that band unprotected.
    assert bound < started_at - timedelta(hours=6) - timedelta(hours=lookback_hours)


def test_rounding_band_cycle_without_candidates_is_exempt(tmp_path: Path) -> None:
    """[helper/plan] The band the un-floored evidence value would expose.

    Retention window 7d puts the cutoff (05-27T03:17Z) above the discovery
    floor (05-26T18:00Z), so a candidate-less cycle at 05-26T18:00Z is aged out
    yet still inside the discovery window — and it sits in the rounding band
    [discovery floor, un-floored evidence floor) = [18:00Z, 21:17Z).
    """
    started_at = datetime(2026, 6, 3, 3, 17, tzinfo=UTC)
    root = tmp_path / "object-store"
    banded = datetime(2026, 5, 26, 18, tzinfo=UTC)
    keys = _seed_cycle(root, banded, run=True)
    scheduler = _frontier_scheduler(tmp_path, lookback_hours=168, cycle_lag_hours=6)
    bound, source = _active_lower_bound(scheduler, started_at)
    evidence_style_floor = started_at - timedelta(hours=6) - timedelta(hours=168)
    assert bound <= banded < evidence_style_floor
    assert banded < started_at - timedelta(days=7), "the banded cycle is aged out"

    def run(active_lower_bound):
        return run_retention(
            object_store_root=root,
            now=started_at,
            config=RetentionConfig(enabled=True, dry_run=True, retention_days=7),
            active_lower_bound=active_lower_bound,
            active_lower_bound_source=source,
        )

    guarded = run(bound)
    reasons = _reasons(guarded.skipped)
    for key in keys.values():
        assert reasons[key] == PIPELINE_FRONTIER_EXEMPT_REASON
        assert key not in _keys(guarded.planned)

    # Control: had the bound been the un-floored evidence value, this same
    # cycle would have been planned for deletion.
    unfloored = run(evidence_style_floor)
    assert _keys(unfloored.planned) == set(keys.values())
