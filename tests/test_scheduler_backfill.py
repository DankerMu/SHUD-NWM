from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator import scheduler as scheduler_module

# Reuse the project's existing fixtures/builders rather than re-inventing them.
from tests.test_production_scheduler import (
    FakeActiveRepository,
    FakeAdapter,
    FakeRegistry,
    ProductionScheduler,
    _config,
    _dt,
    _model,
)


class CompletionByCycleRepository(FakeActiveRepository):
    """Active repository whose pipeline completion is keyed per (source, cycle_time, model)."""

    def __init__(self, completed_cycles: set[tuple[str, datetime]]) -> None:
        super().__init__(active=False, completed=False)
        # Normalise to UTC for stable comparison.
        self._completed = {
            (source_id, _ensure_utc(cycle_time)) for source_id, cycle_time in completed_cycles
        }

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del model_id
        return (source_id, _ensure_utc(cycle_time)) in self._completed


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _source_cycle_times(scheduler: Any, started_at: datetime, models: Sequence[Any]) -> list[str]:
    cycles, _evidence = scheduler._discover_cycles(started_at, models=models)
    return [scheduler_module._format_utc(cycle.discovery.cycle_time) for cycle in cycles]


def _gfs_adapter(cycle_times: Sequence[str]) -> FakeAdapter:
    return FakeAdapter("gfs", [(ct, True) for ct in cycle_times])


def _build_scheduler(
    tmp_path: Path,
    *,
    now: datetime,
    cycle_times: Sequence[str],
    backfill_enabled: bool,
    max_cycles_per_source: int = 1,
    lookback_hours: int = 24,
    active_repository: Any | None = None,
    models: Sequence[Mapping[str, Any]] | None = None,
) -> ProductionScheduler:
    config = _config(
        tmp_path,
        now=now,
        sources=("gfs",),
        lookback_hours=lookback_hours,
        max_cycles_per_source=max_cycles_per_source,
        backfill_enabled=backfill_enabled,
    )
    registry_models = list(models) if models is not None else [_model("model_a", "basin_a")]
    return ProductionScheduler(
        config,
        registry=FakeRegistry(registry_models),
        adapters={"gfs": _gfs_adapter(cycle_times)},
        active_repository=active_repository,
    )


# ---------------------------------------------------------------------------
# Requirement: gap-first selection, completed cycles do not consume budget.
# ---------------------------------------------------------------------------
def test_backfill_selects_older_gap_over_newest_completed(tmp_path: Path) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    cycle_times = [
        "2026-05-21T06:00:00Z",  # newest, completed
        "2026-05-21T00:00:00Z",  # older, gap
    ]
    repo = CompletionByCycleRepository({("gfs", _dt("2026-05-21T06:00:00Z"))})
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=cycle_times,
        backfill_enabled=True,
        max_cycles_per_source=1,
        active_repository=repo,
    )
    models = scheduler._discover_models()[0]

    selected = _source_cycle_times(scheduler, now, models)

    assert selected == ["2026-05-21T00:00:00Z"]


# ---------------------------------------------------------------------------
# Requirement: production backfill advances the oldest gap first so warm-start
# state dependencies stay ordered; later gaps wait for the prior cycle.
# ---------------------------------------------------------------------------
def test_backfill_budget_cap_defers_excess_gaps(tmp_path: Path) -> None:
    now = _dt("2026-05-21T18:00:00Z")
    cycle_times = [
        "2026-05-21T12:00:00Z",
        "2026-05-21T06:00:00Z",
        "2026-05-21T00:00:00Z",
    ]
    # No completion -> all three are gaps.
    repo = CompletionByCycleRepository(set())
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=cycle_times,
        backfill_enabled=True,
        max_cycles_per_source=2,
        active_repository=repo,
    )
    models = scheduler._discover_models()[0]

    cycles, evidence = scheduler._discover_cycles(now, models=models)
    selected = [scheduler_module._format_utc(c.discovery.cycle_time) for c in cycles]
    assert selected == ["2026-05-21T00:00:00Z"]

    deferred = [item for item in evidence if item.get("type") == "backfill_deferred"]
    assert [item["cycle_time_utc"] for item in deferred] == [
        "2026-05-21T06:00:00Z",
        "2026-05-21T12:00:00Z",
    ]
    assert {item["reason"] for item in deferred} == {"backfill_deferred_waiting_for_prior_cycle"}
    assert {item["status"] for item in deferred} == {"gap"}


# ---------------------------------------------------------------------------
# Requirement: legacy mode unchanged -> newest-N, no classification.
# ---------------------------------------------------------------------------
def test_legacy_mode_keeps_newest_even_when_completed(tmp_path: Path) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    cycle_times = [
        "2026-05-21T06:00:00Z",  # newest, completed
        "2026-05-21T00:00:00Z",  # older, gap
    ]
    repo = CompletionByCycleRepository({("gfs", _dt("2026-05-21T06:00:00Z"))})
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=cycle_times,
        backfill_enabled=False,
        max_cycles_per_source=1,
        active_repository=repo,
    )
    models = scheduler._discover_models()[0]

    cycles, evidence = scheduler._discover_cycles(now, models=models)
    selected = [scheduler_module._format_utc(c.discovery.cycle_time) for c in cycles]

    assert selected == ["2026-05-21T06:00:00Z"]
    assert not any(item.get("type") == "backfill_audit" for item in evidence)
    assert not any(item.get("type") == "backfill_deferred" for item in evidence)


# ---------------------------------------------------------------------------
# Requirement: backfill_mode = bool(backfill_enabled and models). Empty models
# short-circuits to legacy (newest-N) even when backfill is enabled.
# ---------------------------------------------------------------------------
def test_backfill_enabled_with_empty_models_falls_back_to_legacy(tmp_path: Path) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    cycle_times = [
        "2026-05-21T06:00:00Z",  # newest, completed
        "2026-05-21T00:00:00Z",  # older, gap
    ]
    repo = CompletionByCycleRepository({("gfs", _dt("2026-05-21T06:00:00Z"))})
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=cycle_times,
        backfill_enabled=True,
        max_cycles_per_source=1,
        active_repository=repo,
    )

    # No models -> backfill_mode is False (the `and models` short-circuit).
    cycles, evidence = scheduler._discover_cycles(now, models=())
    selected = [scheduler_module._format_utc(c.discovery.cycle_time) for c in cycles]

    # Legacy newest-N: newest completed cycle is kept, no gap-first reordering.
    assert selected == ["2026-05-21T06:00:00Z"]
    assert not any(item.get("type") == "backfill_audit" for item in evidence)
    assert not any(item.get("type") == "backfill_deferred" for item in evidence)


# ---------------------------------------------------------------------------
# Requirement: audit evidence counts + run_once pass-level evidence.
# ---------------------------------------------------------------------------
def test_backfill_audit_counts_and_pass_evidence(tmp_path: Path) -> None:
    now = _dt("2026-05-21T18:00:00Z")
    cycle_times = [
        "2026-05-21T12:00:00Z",  # completed
        "2026-05-21T06:00:00Z",  # gap (selected)
        "2026-05-21T00:00:00Z",  # gap (deferred)
    ]
    repo = CompletionByCycleRepository({("gfs", _dt("2026-05-21T12:00:00Z"))})
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=cycle_times,
        backfill_enabled=True,
        max_cycles_per_source=1,
        lookback_hours=168,
        active_repository=repo,
    )
    models = scheduler._discover_models()[0]

    _cycles, evidence = scheduler._discover_cycles(now, models=models)
    audit = next(item for item in evidence if item.get("type") == "backfill_audit")
    assert audit["source_id"] == "gfs"
    assert audit["discovered_count"] == 3
    assert audit["complete_count"] == 1
    assert audit["gap_count"] == 2
    assert audit["selected_count"] == 1
    assert audit["deferred_count"] == 1

    result = scheduler.run_once()
    backfill = result.evidence["backfill"]
    assert backfill["enabled"] is True
    assert backfill["lookback_hours"] == 168
    assert len(backfill["audit"]) == 1
    assert backfill["audit"][0]["gap_count"] == 2


def test_run_once_backfill_disabled_evidence(tmp_path: Path) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=["2026-05-21T06:00:00Z"],
        backfill_enabled=False,
        active_repository=CompletionByCycleRepository(set()),
    )
    result = scheduler.run_once()
    assert result.evidence["backfill"] == {"enabled": False}


# ---------------------------------------------------------------------------
# Requirement: no provider -> all treated as gap, still oldest-first, no exception.
# ---------------------------------------------------------------------------
def test_backfill_without_completion_provider_treats_all_as_gap(tmp_path: Path) -> None:
    now = _dt("2026-05-21T18:00:00Z")
    cycle_times = [
        "2026-05-21T12:00:00Z",
        "2026-05-21T06:00:00Z",
        "2026-05-21T00:00:00Z",
    ]
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=cycle_times,
        backfill_enabled=True,
        max_cycles_per_source=2,
        active_repository=None,  # no has_completed_pipeline
    )
    models = scheduler._discover_models()[0]

    cycles, evidence = scheduler._discover_cycles(now, models=models)
    selected = [scheduler_module._format_utc(c.discovery.cycle_time) for c in cycles]
    assert selected == ["2026-05-21T00:00:00Z"]

    audit = next(item for item in evidence if item.get("type") == "backfill_audit")
    assert audit["complete_count"] == 0
    assert audit["gap_count"] == 3
    assert audit["selected_count"] == 1
    assert audit["deferred_count"] == 2


def test_cycle_completion_status_without_models_is_gap(tmp_path: Path) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=["2026-05-21T06:00:00Z"],
        backfill_enabled=True,
        active_repository=CompletionByCycleRepository({("gfs", _dt("2026-05-21T06:00:00Z"))}),
    )
    adapter = scheduler.adapters["gfs"]
    discovery = adapter.discover_cycles(_dt("2026-05-21T06:00:00Z"))[0]
    assert scheduler._cycle_completion_status(discovery, ()) == "gap"


# ---------------------------------------------------------------------------
# Requirement: 7-day window (lookback=168) spans multi-day discoveries but
# submits only the earliest gap until warm-start dependencies advance.
# ---------------------------------------------------------------------------
def test_backfill_seven_day_window_spans_multiple_days(tmp_path: Path) -> None:
    now = _dt("2026-05-21T00:00:00Z")
    cycle_times = [
        "2026-05-20T12:00:00Z",  # 0.5 day old -> in window
        "2026-05-17T00:00:00Z",  # 4 days old -> in window
        "2026-05-15T00:00:00Z",  # 6 days old -> in window
        "2026-05-13T00:00:00Z",  # 8 days old -> outside 168h window
    ]
    repo = CompletionByCycleRepository(set())
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=cycle_times,
        backfill_enabled=True,
        max_cycles_per_source=8,
        lookback_hours=168,
        active_repository=repo,
    )
    models = scheduler._discover_models()[0]

    cycles, evidence = scheduler._discover_cycles(now, models=models)
    selected = {scheduler_module._format_utc(c.discovery.cycle_time) for c in cycles}

    assert selected == {"2026-05-15T00:00:00Z"}
    assert "2026-05-13T00:00:00Z" not in selected
    deferred = [item for item in evidence if item.get("type") == "backfill_deferred"]
    assert [item["cycle_time_utc"] for item in deferred] == [
        "2026-05-17T00:00:00Z",
        "2026-05-20T12:00:00Z",
    ]
    assert {item["reason"] for item in deferred} == {"backfill_deferred_waiting_for_prior_cycle"}
    audit = next(item for item in evidence if item.get("type") == "backfill_audit")
    assert audit["discovered_count"] == 3
    assert audit["selected_count"] == 1
    assert audit["deferred_count"] == 2


# ---------------------------------------------------------------------------
# Requirement: CLI lookback env fallback.
# ---------------------------------------------------------------------------
def test_plan_production_lookback_env_fallback(tmp_path: Path, monkeypatch: Any) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NHMS_SCHEDULER_LOOKBACK_HOURS", "168")
    monkeypatch.delenv("NHMS_SCHEDULER_BACKFILL_ENABLED", raising=False)

    captured: dict[str, Any] = {}

    real_config = scheduler_module.ProductionSchedulerConfig

    def _capture_config(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_config(**kwargs)

    monkeypatch.setattr(cli, "ProductionSchedulerConfig", _capture_config)
    monkeypatch.setattr(
        cli.ProductionScheduler,
        "from_env",
        classmethod(lambda cls, config: _StubScheduler(config)),
    )

    cli._plan_production(
        sources=("gfs",),
        lookback_hours=None,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=str(workspace),
        lock_path=None,
        evidence_dir=None,
    )

    assert captured["lookback_hours"] == 168


def test_plan_production_lookback_cli_arg_overrides_env(tmp_path: Path, monkeypatch: Any) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NHMS_SCHEDULER_LOOKBACK_HOURS", "168")

    captured: dict[str, Any] = {}
    real_config = scheduler_module.ProductionSchedulerConfig

    def _capture_config(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_config(**kwargs)

    monkeypatch.setattr(cli, "ProductionSchedulerConfig", _capture_config)
    monkeypatch.setattr(
        cli.ProductionScheduler,
        "from_env",
        classmethod(lambda cls, config: _StubScheduler(config)),
    )

    cli._plan_production(
        sources=("gfs",),
        lookback_hours=12,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=("model_a",),
        basin_ids=(),
        dry_run=True,
        continuous=False,
        interval_seconds=300.0,
        max_passes=None,
        workspace_root=str(workspace),
        lock_path=None,
        evidence_dir=None,
    )

    assert captured["lookback_hours"] == 12


class _StubScheduler:
    def __init__(self, config: Any) -> None:
        self.config = config

    def run_once(self) -> Any:
        return _StubPassResult()


class _StubPassResult:
    def __init__(self) -> None:
        self.pass_id = "stub"
        self.status = "planned"
        self.evidence = {"status": "planned"}
        self.artifact_path = None

    def to_dict(self) -> dict[str, Any]:
        return {"pass_id": self.pass_id, "status": self.status}


# Ensure env isolation for backfill flag in module-level scheduler tests.
@pytest.fixture(autouse=True)
def _clear_backfill_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("NHMS_SCHEDULER_BACKFILL_ENABLED", raising=False)
    monkeypatch.delenv("NHMS_SCHEDULER_LOOKBACK_HOURS", raising=False)
    monkeypatch.delenv("NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE", raising=False)
    yield
    os.environ.pop("NHMS_SCHEDULER_BACKFILL_ENABLED", None)
