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


class CandidateStateCompletionRepository(FakeActiveRepository):
    """Repository with incomplete completion rows and per-cycle candidate state fallback."""

    def __init__(
        self,
        states: Mapping[tuple[str, str], Mapping[str, Any] | None],
        *,
        completion: bool = False,
    ) -> None:
        super().__init__(active=False, completed=False)
        self._states = {(cycle_time, model_id): state for (cycle_time, model_id), state in states.items()}
        self._completion = completion
        self.queries: list[dict[str, Any]] = []

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return self._completion

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        del source_id
        self.queries.append(
            {
                "cycle_time": cycle_time,
                "model_id": model_id,
                "run_id": run_id,
                "forcing_version_id": forcing_version_id,
                "candidate_id": candidate_id,
            }
        )
        state = self._states.get((scheduler_module._format_utc(cycle_time), model_id))
        if state is None:
            return None
        return _candidate_state_with_identity(
            state,
            model_id=model_id,
            run_id=run_id,
            forcing_version_id=forcing_version_id,
            candidate_id=candidate_id,
        )


class CandidateStateOnlyRepository:
    """Repository without has_completed_pipeline, used for absent-provider fallback coverage."""

    def __init__(self, states: Mapping[tuple[str, str], Mapping[str, Any] | None]) -> None:
        self._states = {(cycle_time, model_id): state for (cycle_time, model_id), state in states.items()}
        self.queries: list[dict[str, Any]] = []

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        del source_id, cycle_time
        return False

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return False

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        del source_id
        self.queries.append(
            {
                "cycle_time": cycle_time,
                "model_id": model_id,
                "run_id": run_id,
                "forcing_version_id": forcing_version_id,
                "candidate_id": candidate_id,
            }
        )
        state = self._states.get((scheduler_module._format_utc(cycle_time), model_id))
        if state is None:
            return None
        return _candidate_state_with_identity(
            state,
            model_id=model_id,
            run_id=run_id,
            forcing_version_id=forcing_version_id,
            candidate_id=candidate_id,
        )


def _candidate_state_with_identity(
    state: Mapping[str, Any],
    *,
    model_id: str,
    run_id: str,
    forcing_version_id: str,
    candidate_id: str,
) -> dict[str, Any]:
    payload = {
        **dict(state),
        "model_id": model_id,
        "run_id": run_id,
        "forcing_version_id": forcing_version_id,
        "candidate_id": candidate_id,
    }
    jobs = payload.get("pipeline_jobs")
    if isinstance(jobs, Sequence) and not isinstance(jobs, str | bytes | bytearray):
        identity_jobs: list[Any] = []
        for job in jobs:
            if not isinstance(job, Mapping):
                identity_jobs.append(job)
                continue
            job_payload = dict(job)
            job_payload.setdefault("model_id", model_id)
            job_payload.setdefault("run_id", run_id)
            job_payload.setdefault("forcing_version_id", forcing_version_id)
            job_payload.setdefault("candidate_id", candidate_id)
            identity_jobs.append(job_payload)
        payload["pipeline_jobs"] = identity_jobs
    return payload


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _source_cycle_times(scheduler: Any, started_at: datetime, models: Sequence[Any]) -> list[str]:
    cycles, _evidence = scheduler._discover_cycles(started_at, models=models)
    return [scheduler_module._format_utc(cycle.discovery.cycle_time) for cycle in cycles]


def _discovery_for_time(adapter: Any, cycle_time: str) -> Any:
    parsed = _dt(cycle_time)
    return next(
        discovery
        for discovery in adapter.discover_cycles(parsed)
        if scheduler_module._format_utc(discovery.cycle_time) == cycle_time
    )


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
    allowed_cycle_hours_utc: Sequence[int] = (0, 6, 12, 18),
) -> ProductionScheduler:
    config = _config(
        tmp_path,
        now=now,
        sources=("gfs",),
        lookback_hours=lookback_hours,
        max_cycles_per_source=max_cycles_per_source,
        backfill_enabled=backfill_enabled,
        allowed_cycle_hours_utc=tuple(allowed_cycle_hours_utc),
    )
    registry_models = list(models) if models is not None else [_model("model_a", "basin_a")]
    return ProductionScheduler(
        config,
        registry=FakeRegistry(registry_models),
        adapters={"gfs": _gfs_adapter(cycle_times)},
        active_repository=active_repository,
    )


class _LegacyTypeErrorFallbackAdapter:
    def __init__(self, source_id: str, cycle_time: str) -> None:
        self.source_id = source_id
        self.cycle_time = _dt(cycle_time)
        self.two_arg_calls: list[tuple[Any, Any]] = []

    def discover_cycles(
        self,
        cycle_date: Any,
        end_date: Any,
    ) -> list[Any]:
        requested_date = cycle_date.date() if isinstance(cycle_date, datetime) else cycle_date
        self.two_arg_calls.append((requested_date, end_date))
        if requested_date != self.cycle_time.date():
            return []
        return [
            scheduler_module.CycleDiscovery(
                cycle_id=scheduler_module.cycle_id_for(self.source_id, self.cycle_time),
                source_id=self.source_id,
                cycle_time=self.cycle_time,
                cycle_hour=self.cycle_time.hour,
                available=True,
                status="discovered",
            )
        ]


# ---------------------------------------------------------------------------
# Requirement: extracted discovery still honors old private-method monkeypatches.
# ---------------------------------------------------------------------------
def test_discover_cycles_honors_instance_monkeypatch_discover_source_window(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=["2026-05-21T06:00:00Z"],
        backfill_enabled=False,
    )
    fake_cycle_time = _dt("2026-05-21T00:00:00Z")
    called = False

    def _fake_discover_source_window(
        adapter: Any,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Any]:
        nonlocal called
        del adapter
        called = True
        assert source_id == "gfs"
        assert start_time <= fake_cycle_time <= end_time
        return [
            scheduler_module.CycleDiscovery(
                cycle_id=scheduler_module.cycle_id_for(source_id, fake_cycle_time),
                source_id=source_id,
                cycle_time=fake_cycle_time,
                cycle_hour=fake_cycle_time.hour,
                available=True,
                status="discovered",
            )
        ]

    monkeypatch.setattr(scheduler, "_discover_source_window", _fake_discover_source_window)

    cycles, _evidence = scheduler._discover_cycles(now, models=())

    assert called is True
    assert [scheduler_module._format_utc(cycle.discovery.cycle_time) for cycle in cycles] == [
        "2026-05-21T00:00:00Z"
    ]


def test_discover_cycles_honors_instance_monkeypatch_cycle_completion_status(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=["2026-05-21T06:00:00Z"],
        backfill_enabled=True,
        active_repository=None,
    )
    selected_models = scheduler._discover_models()[0]
    calls: list[str] = []

    def _fake_cycle_completion_status(
        discovery: Any,
        models: Sequence[Any],
        *,
        horizon: Mapping[str, Any] | None = None,
    ) -> str:
        assert models == selected_models
        assert horizon is not None
        calls.append(scheduler_module._format_utc(discovery.cycle_time))
        return "complete"

    monkeypatch.setattr(scheduler, "_cycle_completion_status", _fake_cycle_completion_status)

    cycles, evidence = scheduler._discover_cycles(now, models=selected_models)

    assert calls == ["2026-05-21T06:00:00Z"]
    assert cycles == []
    audit = next(item for item in evidence if item.get("type") == "backfill_audit")
    assert audit["complete_count"] == 1
    assert audit["gap_count"] == 0
    assert audit["selected_count"] == 0


def test_legacy_adapter_typeerror_fallback_selects_row_and_source_cycle_evidence(
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    adapter = _LegacyTypeErrorFallbackAdapter("gfs", "2026-05-21T06:00:00Z")
    config = _config(
        tmp_path,
        now=now,
        sources=("gfs",),
        lookback_hours=6,
        max_cycles_per_source=1,
        backfill_enabled=False,
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": adapter},
        active_repository=CompletionByCycleRepository(set()),
    )

    cycles, evidence = scheduler._discover_cycles(now, models=())

    assert [scheduler_module._format_utc(cycle.discovery.cycle_time) for cycle in cycles] == [
        "2026-05-21T06:00:00Z"
    ]
    assert adapter.two_arg_calls == [(adapter.cycle_time.date(), None)]
    source_cycle_evidence = [
        item
        for item in evidence
        if item.get("source_id") == "gfs" and item.get("cycle_time_utc") is not None
    ]
    assert [
        (item["source_id"], item["cycle_time_utc"], item["status"])
        for item in source_cycle_evidence
    ] == [("gfs", "2026-05-21T06:00:00Z", "discovered")]


def test_discover_cycles_filters_wrong_source_and_out_of_window_before_selection_and_evidence(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = _dt("2026-05-21T12:00:00Z")
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=[],
        lookback_hours=6,
        backfill_enabled=False,
    )
    valid_time = _dt("2026-05-21T06:00:00Z")
    out_of_window_time = _dt("2026-05-21T00:00:00Z")

    def _row(source_id: str, cycle_time: datetime) -> Any:
        return scheduler_module.CycleDiscovery(
            cycle_id=scheduler_module.cycle_id_for(source_id, cycle_time),
            source_id=source_id,
            cycle_time=cycle_time,
            cycle_hour=cycle_time.hour,
            available=True,
            status="discovered",
        )

    def _fake_discover_source_window(
        adapter: Any,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Any]:
        del adapter
        assert source_id == "gfs"
        assert start_time <= valid_time <= end_time
        assert out_of_window_time < start_time
        return [
            _row("gfs", valid_time),
            _row("IFS", valid_time),
            _row("gfs", out_of_window_time),
        ]

    monkeypatch.setattr(scheduler, "_discover_source_window", _fake_discover_source_window)

    cycles, evidence = scheduler._discover_cycles(now, models=())

    selected_rows = [
        (cycle.discovery.source_id, scheduler_module._format_utc(cycle.discovery.cycle_time))
        for cycle in cycles
    ]
    evidence_rows = [
        (item.get("source_id"), item.get("cycle_time_utc"))
        for item in evidence
        if item.get("cycle_time_utc") is not None
    ]
    assert selected_rows == [("gfs", "2026-05-21T06:00:00Z")]
    assert evidence_rows == [("gfs", "2026-05-21T06:00:00Z")]
    assert ("IFS", "2026-05-21T06:00:00Z") not in selected_rows
    assert ("IFS", "2026-05-21T06:00:00Z") not in evidence_rows
    assert ("gfs", "2026-05-21T00:00:00Z") not in selected_rows
    assert ("gfs", "2026-05-21T00:00:00Z") not in evidence_rows


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


def test_allowed_cycle_hours_filter_before_completion_and_gap_accounting(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = _dt("2026-05-22T00:00:00Z")
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=[
            "2026-05-21T00:00:00Z",
            "2026-05-21T06:00:00Z",
            "2026-05-21T12:00:00Z",
            "2026-05-21T18:00:00Z",
        ],
        backfill_enabled=True,
        max_cycles_per_source=8,
        active_repository=CompletionByCycleRepository(set()),
        allowed_cycle_hours_utc=(0, 12),
    )
    selected_models = scheduler._discover_models()[0]
    completion_calls: list[str] = []

    def _fake_cycle_completion_status(
        discovery: Any,
        models: Sequence[Any],
        *,
        horizon: Mapping[str, Any] | None = None,
    ) -> str:
        del models, horizon
        cycle_time = scheduler_module._format_utc(discovery.cycle_time)
        completion_calls.append(cycle_time)
        assert discovery.cycle_time.hour in {0, 12}
        return "gap"

    monkeypatch.setattr(scheduler, "_cycle_completion_status", _fake_cycle_completion_status)

    cycles, evidence = scheduler._discover_cycles(now, models=selected_models)

    assert [scheduler_module._format_utc(cycle.discovery.cycle_time) for cycle in cycles] == [
        "2026-05-21T00:00:00Z"
    ]
    assert completion_calls == ["2026-05-21T00:00:00Z", "2026-05-21T12:00:00Z"]
    excluded = [
        item
        for item in evidence
        if item.get("selection_reason") == "cycle_hour_not_allowed"
    ]
    assert [(item["cycle_time_utc"], item["selection_status"], item["status"]) for item in excluded] == [
        ("2026-05-21T06:00:00Z", "excluded", "excluded"),
        ("2026-05-21T18:00:00Z", "excluded", "excluded"),
    ]
    audit = next(item for item in evidence if item.get("type") == "backfill_audit")
    assert audit["discovered_count"] == 2
    assert audit["complete_count"] == 0
    assert audit["gap_count"] == 2
    assert audit["available_gap_count"] == 2
    assert audit["unavailable_gap_count"] == 0
    assert audit["selected_count"] == 1
    assert audit["deferred_count"] == 1


def test_allowed_cycle_hours_filter_before_dedupe_latest_source_collapse(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = _dt("2026-05-22T00:00:00Z")
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=[],
        backfill_enabled=False,
        max_cycles_per_source=8,
        allowed_cycle_hours_utc=(0, 12),
    )
    cycle_00 = _dt("2026-05-21T00:00:00Z")
    cycle_12 = _dt("2026-05-21T12:00:00Z")
    cycle_06 = _dt("2026-05-21T06:00:00Z")
    cycle_18 = _dt("2026-05-21T18:00:00Z")

    def _row(cycle_time: datetime) -> Any:
        return scheduler_module.CycleDiscovery(
            cycle_id=scheduler_module.cycle_id_for("gfs", cycle_time),
            source_id="gfs",
            cycle_time=cycle_time,
            cycle_hour=cycle_time.hour,
            available=True,
            status="discovered",
        )

    def _fake_discover_source_window(
        adapter: Any,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Any]:
        del adapter, start_time, end_time
        assert source_id == "gfs"
        return [
            _row(cycle_06),
            _row(cycle_12),
            _row(cycle_12),
            _row(cycle_18),
            _row(cycle_00),
            _row(cycle_00),
        ]

    monkeypatch.setattr(scheduler, "_discover_source_window", _fake_discover_source_window)

    cycles, evidence = scheduler._discover_cycles(now, models=())

    assert [scheduler_module._format_utc(cycle.discovery.cycle_time) for cycle in cycles] == [
        "2026-05-21T00:00:00Z",
        "2026-05-21T12:00:00Z",
    ]
    duplicate_exclusions = [
        item for item in evidence if item.get("reason") == "duplicate_source_cycle"
    ]
    assert [item["cycle_time_utc"] for item in duplicate_exclusions] == [
        "2026-05-21T12:00:00Z",
        "2026-05-21T00:00:00Z",
    ]
    cycle_hour_exclusions = [
        item
        for item in evidence
        if item.get("selection_reason") == "cycle_hour_not_allowed"
    ]
    assert [item["cycle_time_utc"] for item in cycle_hour_exclusions] == [
        "2026-05-21T06:00:00Z",
        "2026-05-21T18:00:00Z",
    ]


def test_allowed_cycle_hours_explicit_four_cycle_compatibility(tmp_path: Path) -> None:
    now = _dt("2026-05-21T18:00:00Z")
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=[
            "2026-05-21T00:00:00Z",
            "2026-05-21T06:00:00Z",
            "2026-05-21T12:00:00Z",
            "2026-05-21T18:00:00Z",
        ],
        backfill_enabled=False,
        max_cycles_per_source=4,
        allowed_cycle_hours_utc=(0, 6, 12, 18),
    )

    cycles, evidence = scheduler._discover_cycles(now, models=())

    assert [scheduler_module._format_utc(cycle.discovery.cycle_time) for cycle in cycles] == [
        "2026-05-21T00:00:00Z",
        "2026-05-21T06:00:00Z",
        "2026-05-21T12:00:00Z",
        "2026-05-21T18:00:00Z",
    ]
    assert not any(item.get("selection_reason") == "cycle_hour_not_allowed" for item in evidence)


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


@pytest.mark.parametrize("completion_provider", ["false", "absent"])
def test_candidate_state_completion_fallback_skips_complete_older_gap(
    tmp_path: Path,
    completion_provider: str,
) -> None:
    now = _dt("2026-05-21T18:00:00Z")
    cycle_times = [
        "2026-05-21T12:00:00Z",
        "2026-05-21T06:00:00Z",
        "2026-05-21T00:00:00Z",
    ]
    models = [
        _model("model_a", "basin_a"),
        _model("model_b", "basin_b"),
    ]
    repo = CandidateStateCompletionRepository(
        {
            ("2026-05-21T00:00:00Z", "model_a"): {
                "hydro_status": "succeeded",
                "output_uri": "s3://nhms/runs/model-a/output/",
            },
            ("2026-05-21T00:00:00Z", "model_b"): {
                "pipeline_status": "published",
                "pipeline_jobs": [
                    {
                        "job_id": "job_publish_success",
                        "model_id": "model_b",
                        "status": "published",
                        "stage": "publish",
                        "updated_at": "2026-05-21T00:30:00Z",
                    }
                ],
            },
        },
        completion=False,
    )
    if completion_provider == "absent":
        repo = CandidateStateOnlyRepository(repo._states)
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=cycle_times,
        backfill_enabled=True,
        max_cycles_per_source=3,
        active_repository=repo,
        models=models,
    )
    selected_models = scheduler._discover_models()[0]
    adapter = scheduler.adapters["gfs"]
    oldest = _discovery_for_time(adapter, "2026-05-21T00:00:00Z")

    status = scheduler._cycle_completion_status(
        oldest,
        selected_models,
        horizon=scheduler_module._source_horizon_metadata(oldest, adapter),
    )
    cycles, evidence = scheduler._discover_cycles(now, models=selected_models)
    selected = [scheduler_module._format_utc(c.discovery.cycle_time) for c in cycles]
    audit = next(item for item in evidence if item.get("type") == "backfill_audit")

    assert status == "complete"
    assert selected == ["2026-05-21T06:00:00Z"]
    assert audit["complete_count"] == 1
    assert audit["gap_count"] == 2
    assert audit["selected_count"] == 1


def test_candidate_state_completion_fallback_mixed_state_keeps_oldest_gap_first(
    tmp_path: Path,
) -> None:
    now = _dt("2026-05-21T18:00:00Z")
    cycle_times = [
        "2026-05-21T12:00:00Z",
        "2026-05-21T06:00:00Z",
        "2026-05-21T00:00:00Z",
    ]
    models = [
        _model("model_a", "basin_a"),
        _model("model_b", "basin_b"),
    ]
    repo = CandidateStateCompletionRepository(
        {
            ("2026-05-21T00:00:00Z", "model_a"): {
                "hydro_status": "succeeded",
                "output_uri": "s3://nhms/runs/model-a/output/",
            },
            ("2026-05-21T00:00:00Z", "model_b"): {
                "pipeline_status": "running",
                "pipeline_jobs": [
                    {
                        "job_id": "job_forcing_running",
                        "model_id": "model_b",
                        "status": "running",
                        "stage": "forcing",
                        "slurm_job_id": "12345",
                    }
                ],
            },
            ("2026-05-21T06:00:00Z", "model_a"): {
                "hydro_status": "succeeded",
                "output_uri": "s3://nhms/runs/model-a/output/",
            },
        },
        completion=False,
    )
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=cycle_times,
        backfill_enabled=True,
        max_cycles_per_source=3,
        active_repository=repo,
        models=models,
    )
    selected_models = scheduler._discover_models()[0]
    adapter = scheduler.adapters["gfs"]
    oldest = _discovery_for_time(adapter, "2026-05-21T00:00:00Z")
    missing_model_state_cycle = _discovery_for_time(adapter, "2026-05-21T06:00:00Z")

    status = scheduler._cycle_completion_status(
        oldest,
        selected_models,
        horizon=scheduler_module._source_horizon_metadata(oldest, adapter),
    )
    missing_model_state_status = scheduler._cycle_completion_status(
        missing_model_state_cycle,
        selected_models,
        horizon=scheduler_module._source_horizon_metadata(missing_model_state_cycle, adapter),
    )
    cycles, evidence = scheduler._discover_cycles(now, models=selected_models)
    selected = [scheduler_module._format_utc(c.discovery.cycle_time) for c in cycles]
    deferred = [item for item in evidence if item.get("type") == "backfill_deferred"]
    audit = next(item for item in evidence if item.get("type") == "backfill_audit")

    assert status == "gap"
    assert missing_model_state_status == "gap"
    assert selected == ["2026-05-21T00:00:00Z"]
    assert [item["cycle_time_utc"] for item in deferred] == [
        "2026-05-21T06:00:00Z",
        "2026-05-21T12:00:00Z",
    ]
    assert {item["reason"] for item in deferred} == {"backfill_deferred_waiting_for_prior_cycle"}
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
