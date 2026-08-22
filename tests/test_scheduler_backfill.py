from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import state_snapshot_id
from services.orchestrator import cli
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from tests.lineage_state_index_fixtures import index_entry as _lineage_index_entry
from tests.lineage_state_index_fixtures import index_repository as _lineage_index_repository

# Reuse the project's existing journal-seeding pattern verbatim.
from tests.test_file_orchestration_journal import _latest_view as _journal_latest_view
from tests.test_file_orchestration_journal import _write_json as _journal_write_json

# Reuse the project's existing fixtures/builders rather than re-inventing them.
from tests.test_production_scheduler import (
    FakeActiveRepository,
    FakeAdapter,
    FakeRegistry,
    ProductionScheduler,
    _config,
    _dt,
    _model,
    _set_db_free_scheduler_env,
    _write_db_free_raw_manifest_fixture,
    _write_db_free_state_index_fixture,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time


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


class _ExplodingDiscoveryAdapter:
    def discover_cycles(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        raise AssertionError("NFS raw manifest discovery must not call the source adapter")


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


def test_nfs_raw_manifest_required_discovery_does_not_probe_source_adapter(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    object_store_root = tmp_path / "object-store"
    roots = {"object_store_root": object_store_root}
    ready_cycle = _dt("2026-06-27T00:00:00Z")
    missing_cycle = _dt("2026-06-27T12:00:00Z")
    _write_db_free_raw_manifest_fixture(roots, source_id="IFS", cycle_time=ready_cycle)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(object_store_root))
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX", "s3://nhms")
    config = _config(
        tmp_path,
        now=_dt("2026-06-27T18:00:00Z"),
        sources=("IFS",),
        allowed_cycle_hours_utc=(0, 12),
    )
    scheduler = ProductionScheduler(
        config,
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"IFS": _ExplodingDiscoveryAdapter()},
    )

    discoveries = scheduler._discover_source_window(
        _ExplodingDiscoveryAdapter(),
        source_id="IFS",
        start_time=ready_cycle,
        end_time=missing_cycle,
    )

    assert [(item.cycle_hour, item.available, item.status, item.reason) for item in discoveries] == [
        (0, True, "discovered", None),
        (12, False, "missing", "nfs_raw_manifest_manifest_not_found"),
    ]
    assert discoveries[0].evidence["source"] == "node27_nfs_raw_manifest"
    assert discoveries[0].evidence["manifest_key"] == "raw/IFS/2026062700/manifest.json"


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


def test_allowed_cycle_hours_filter_before_legacy_single_slot_latest_collapse(
    tmp_path: Path,
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
        backfill_enabled=False,
        max_cycles_per_source=1,
        allowed_cycle_hours_utc=(0, 12),
    )

    cycles, evidence = scheduler._discover_cycles(now, models=())

    assert [scheduler_module._format_utc(cycle.discovery.cycle_time) for cycle in cycles] == [
        "2026-05-21T12:00:00Z",
    ]
    excluded = [
        item
        for item in evidence
        if item.get("selection_reason") == "cycle_hour_not_allowed"
    ]
    assert [(item["cycle_time_utc"], item["cycle_hour"], item["selection_status"]) for item in excluded] == [
        ("2026-05-21T06:00:00Z", 6, "excluded"),
        ("2026-05-21T18:00:00Z", 18, "excluded"),
    ]


def test_disallowed_cycle_hour_evidence_uses_gate_cycle_time_hour(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    now = _dt("2026-05-22T00:00:00Z")
    scheduler = _build_scheduler(
        tmp_path,
        now=now,
        cycle_times=[],
        backfill_enabled=False,
        max_cycles_per_source=1,
        allowed_cycle_hours_utc=(0, 12),
    )
    cycle_time = _dt("2026-05-21T06:00:00Z")

    def _fake_discover_source_window(
        adapter: Any,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Any]:
        del adapter, start_time, end_time
        return [
            scheduler_module.CycleDiscovery(
                cycle_id=scheduler_module.cycle_id_for(source_id, cycle_time),
                source_id=source_id,
                cycle_time=cycle_time,
                cycle_hour=0,
                available=True,
                status="discovered",
            )
        ]

    monkeypatch.setattr(scheduler, "_discover_source_window", _fake_discover_source_window)

    cycles, evidence = scheduler._discover_cycles(now, models=())

    assert cycles == []
    excluded = [
        item
        for item in evidence
        if item.get("selection_reason") == "cycle_hour_not_allowed"
    ]
    assert [(item["cycle_time_utc"], item["cycle_hour"], item["selection_status"]) for item in excluded] == [
        ("2026-05-21T06:00:00Z", 6, "excluded"),
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


# ---------------------------------------------------------------------------
# §8.7 / #1107: journal-recorded predecessor identity quarantine.
#
# Seam: ``cycle_completion_status`` (completed-provider-only branch) ->
# ``_select_backfill_source_cycles``.  A completed cycle-T journal entry whose
# recorded ``init_state_id`` shares T's expected base key but carries a
# different lineage suffix must NOT count as complete and must NOT suppress
# backfill, while the entry itself stays byte-identical on disk.
# ---------------------------------------------------------------------------

_IDENTITY_NOW = "2026-05-21T18:00:00Z"
_IDENTITY_CYCLE_TIMES = (
    "2026-05-21T12:00:00Z",
    "2026-05-21T06:00:00Z",
    "2026-05-21T00:00:00Z",
)
#: Oldest discovered cycle — the one seeded as "completed" in these tests.
_IDENTITY_TARGET_CYCLE = "2026-05-21T00:00:00Z"
#: Cadence of ``allowed_cycle_hours_utc=(0, 6, 12, 18)`` -> lead 6h for T.
_IDENTITY_REQUIRED_LEAD_HOURS = 6


def _init_state_id_for(
    cycle_time: str,
    *,
    lead_hours: int,
    model_id: str = "model_a",
    source_id: str = "gfs",
) -> str:
    """Compose an ``init_state_id`` exactly the way the write side does.

    Independent source of truth for the expected token: this mirrors
    ``packages.common.state_cli`` -> ``state_manager.save_state_snapshot``
    (``state_snapshot_id`` + ``cycle_id_for(source, valid_time - lead)``)
    rather than calling the scheduler helper under test.
    """
    valid_time = _dt(cycle_time)
    return state_snapshot_id(
        model_id,
        valid_time,
        source_id=source_id,
        cycle_id=cycle_id_for(source_id, valid_time - timedelta(hours=lead_hours)),
        lead_hours=lead_hours,
    )


def _seed_completed_journal_cycle(
    journal_root: Path,
    *,
    cycle_time: str,
    init_state_id: str | None,
    model_id: str = "model_a",
    jobs: list[dict[str, Any]] | None = None,
) -> Path:
    """Write a completed latest-view entry, optionally pinning ``init_state_id``."""
    parsed = _dt(cycle_time)
    latest = _journal_latest_view(
        cycle_time=parsed,
        model_id=model_id,
        hydro_status="complete",
        jobs=list(jobs or []),
    )
    if init_state_id is not None:
        latest["hydro_run"]["init_state_id"] = init_state_id
    path = journal_root / "latest" / "gfs" / format_cycle_time(parsed) / f"{model_id}.json"
    _journal_write_json(path, latest)
    return path


def _completed_submission_master(
    cycle_time: str,
    init_state_id: str,
    *,
    job_suffix: str = "",
    model_id: str = "model_a",
    quarantine_rerun_model_ids: list[str] | None = None,
) -> dict[str, Any]:
    """One terminal-success cohort master that recorded ``init_state_id``.

    One master row per completed forecast submission (#1183).  The §8.7 breaker
    counts only those stamped as quarantine reruns (#1157 D3 R1);
    ``quarantine_rerun_model_ids=None`` omits the field, the shape of an
    unrelated whitelisted replacement and of any pre-#1157 journal.
    """
    parsed = _dt(cycle_time)
    run_id = f"cycle_gfs_{format_cycle_time(parsed)}"
    row: dict[str, Any] = {
        "job_id": f"job_{run_id}_forecast{job_suffix}",
        "run_id": run_id,
        "cycle_id": cycle_id_for("gfs", parsed),
        "candidate_id": run_id,
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "status": "succeeded",
        "model_id": None,
        "init_state_identities": [
            {"array_task_id": 0, "model_id": model_id, "init_state_id": init_state_id}
        ],
    }
    if quarantine_rerun_model_ids is not None:
        row["journal_predecessor_quarantine_rerun_model_ids"] = list(quarantine_rerun_model_ids)
    return row


def _breaker_engaged_masters(
    cycle_time: str,
    init_state_id: str,
    *,
    model_id: str = "model_a",
) -> list[dict[str, Any]]:
    """The original defect run plus the stamped rerun that failed to converge."""
    return [
        _completed_submission_master(cycle_time, init_state_id, model_id=model_id),
        _completed_submission_master(
            cycle_time,
            init_state_id,
            job_suffix="_retry_1",
            model_id=model_id,
            quarantine_rerun_model_ids=[model_id],
        ),
    ]


def _journal_backed_scheduler(tmp_path: Path, journal_root: Path) -> ProductionScheduler:
    return _build_scheduler(
        tmp_path,
        now=_dt(_IDENTITY_NOW),
        cycle_times=list(_IDENTITY_CYCLE_TIMES),
        backfill_enabled=True,
        max_cycles_per_source=len(_IDENTITY_CYCLE_TIMES),
        active_repository=FileOrchestrationJournalRepository(journal_root),
    )


def _completion_status(scheduler: ProductionScheduler, cycle_time: str) -> str:
    adapter = scheduler.adapters["gfs"]
    discovery = _discovery_for_time(adapter, cycle_time)
    return scheduler._cycle_completion_status(
        discovery,
        scheduler._discover_models()[0],
        horizon=scheduler_module._source_horizon_metadata(discovery, adapter),
    )


def _selected_backfill_cycles(scheduler: ProductionScheduler) -> list[str]:
    return _source_cycle_times(scheduler, _dt(_IDENTITY_NOW), scheduler._discover_models()[0])


#: Package checksum shared by the db-free state index entries and the
#: registered model's resource profile (the successor strict-warm-start probe
#: fails closed without a candidate-side checksum).
_DB_FREE_PACKAGE_CHECKSUM = "sha256:" + "a" * 64


def _db_free_state_index_entry(
    roots: Mapping[str, Path],
    *,
    valid_time: datetime,
    producer_cycle_time: datetime,
    model_id: str = "model_a",
) -> dict[str, Any]:
    """One published state-snapshot-index row, composed like the write side."""
    store = LocalObjectStore(roots["object_store_root"], "s3://nhms")
    content = f"state-fixture-{format_cycle_time(valid_time)}\n".encode()
    state_uri = store.write_bytes_atomic(
        f"states/gfs/{model_id}/{format_cycle_time(valid_time)}/state.cfg.ic",
        content,
    )
    producer_cycle_id = cycle_id_for("gfs", producer_cycle_time)
    lead_hours = int(round((valid_time - producer_cycle_time).total_seconds() / 3600.0))
    return {
        "state_id": (
            f"state_gfs_{model_id}_{format_cycle_time(valid_time)}"
            f"_{producer_cycle_id}_f{lead_hours:03d}"
        ),
        "model_id": model_id,
        "run_id": f"analysis_{producer_cycle_id}_{model_id}",
        "source_id": "gfs",
        "valid_time": valid_time.isoformat().replace("+00:00", "Z"),
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "cycle_id": producer_cycle_id,
        "lead_hours": lead_hours,
        "model_package_version": f"s3://nhms/models/{model_id}/package/",
        "model_package_checksum": _DB_FREE_PACKAGE_CHECKSUM,
    }


def _db_free_strict_branch_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    journal_root: Path,
) -> ProductionScheduler:
    """Scheduler in the PRODUCTION db-free shape for cycle T.

    ``NHMS_SCHEDULER_DB_FREE_REQUIRED=true`` + ``NHMS_REQUIRE_FORECAST_WARM_START
    =false`` is the regime where ``cycle_completion_status``'s strict/successor
    branch preempts the completed-provider branch: the D8.9 compat leg nulls
    the strict warm start for a journal-completed cycle, while the successor
    checkpoint at T+6h is ready, so that branch reaches its OWN
    ``return "complete"``.  The post-#1107 identity gate has to cover that exit
    too, which is what the two tests below pin.
    """
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=_dt(_IDENTITY_TARGET_CYCLE),
        package_checksum=_DB_FREE_PACKAGE_CHECKSUM,
        generated_at=_dt(_IDENTITY_NOW),
        entries=[
            _db_free_state_index_entry(
                roots,
                valid_time=_dt(_IDENTITY_TARGET_CYCLE),
                producer_cycle_time=_dt("2026-05-20T18:00:00Z"),
            ),
            # Successor checkpoint at T+6h -> the successor probe is ready, so
            # the strict/successor branch does not bail out early.
            _db_free_state_index_entry(
                roots,
                valid_time=_dt("2026-05-21T06:00:00Z"),
                producer_cycle_time=_dt(_IDENTITY_TARGET_CYCLE),
            ),
        ],
    )
    scheduler = _build_scheduler(
        tmp_path,
        now=_dt(_IDENTITY_NOW),
        cycle_times=list(_IDENTITY_CYCLE_TIMES),
        backfill_enabled=True,
        max_cycles_per_source=len(_IDENTITY_CYCLE_TIMES),
        active_repository=FileOrchestrationJournalRepository(journal_root),
        models=[
            _model(
                "model_a",
                "basin_a",
                resource_profile={
                    "runnable": True,
                    "memory_gb": 8,
                    "display_capabilities": {"tiles": True},
                    "package_checksum": _DB_FREE_PACKAGE_CHECKSUM,
                },
            )
        ],
    )
    _assert_strict_branch_decides_completion(scheduler)
    return scheduler


def _assert_strict_branch_decides_completion(scheduler: ProductionScheduler) -> None:
    """Fixture guard: the strict/successor branch — not the completed-provider
    branch — is the one that reaches ``return "complete"`` for cycle T."""
    assert scheduler.config.db_free_required is True
    adapter = scheduler.adapters["gfs"]
    discovery = _discovery_for_time(adapter, _IDENTITY_TARGET_CYCLE)
    horizon = dict(scheduler_module._source_horizon_metadata(discovery, adapter))
    model = scheduler._discover_models()[0][0]
    candidate = scheduler_module._candidate_for(discovery=discovery, model=model, horizon=horizon)
    cycle = scheduler_module.SchedulerSourceCycle(discovery=discovery, horizon=horizon)
    assert scheduler._strict_warm_start_for_candidate(candidate, cycle) is None
    successor = scheduler._successor_warm_start_state_for_candidate(candidate, cycle)
    assert successor is not None
    assert successor["ready"] is True


def test_stale_lineage_journal_entry_does_not_suppress_backfill(tmp_path: Path) -> None:
    """§8.7: same base key + wrong lineage suffix -> not complete, backfill stands."""
    journal_root = tmp_path / "journal"
    stale_id = _init_state_id_for(_IDENTITY_TARGET_CYCLE, lead_hours=12)
    expected_id = _init_state_id_for(
        _IDENTITY_TARGET_CYCLE, lead_hours=_IDENTITY_REQUIRED_LEAD_HOURS
    )
    # Same base key (source/model/valid_time=T), different lineage suffix.
    assert stale_id != expected_id
    assert stale_id.startswith("state_gfs_model_a_2026052100_")
    entry_path = _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=stale_id,
    )
    before = entry_path.read_bytes()
    scheduler = _journal_backed_scheduler(tmp_path, journal_root)

    status = _completion_status(scheduler, _IDENTITY_TARGET_CYCLE)
    selected = _selected_backfill_cycles(scheduler)

    assert status == "gap"
    # T is head-of-line in the gap set, i.e. backfill is not suppressed.
    assert selected == [_IDENTITY_TARGET_CYCLE]
    # Immutable audit entry: the scoring pass never rewrites the journal.
    assert entry_path.read_bytes() == before


def test_matching_lineage_journal_entry_still_skips_completed_cycle(tmp_path: Path) -> None:
    """The expected token keeps the pre-#1107 completed skip exactly as before."""
    journal_root = tmp_path / "journal"
    entry_path = _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=_init_state_id_for(
            _IDENTITY_TARGET_CYCLE, lead_hours=_IDENTITY_REQUIRED_LEAD_HOURS
        ),
    )
    before = entry_path.read_bytes()
    scheduler = _journal_backed_scheduler(tmp_path, journal_root)

    status = _completion_status(scheduler, _IDENTITY_TARGET_CYCLE)
    selected = _selected_backfill_cycles(scheduler)

    assert status == "complete"
    assert selected == ["2026-05-21T06:00:00Z"]
    assert entry_path.read_bytes() == before


def test_db_free_strict_branch_stale_lineage_does_not_suppress_backfill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """§8.7 under PRODUCTION db-free config: the strict/successor branch of
    ``cycle_completion_status`` preempts the completed-provider branch, so the
    identity gate must sit on that exit too (fix round 1, task 6.1)."""
    journal_root = tmp_path / "journal"
    entry_path = _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=_init_state_id_for(_IDENTITY_TARGET_CYCLE, lead_hours=12),
    )
    before = entry_path.read_bytes()
    scheduler = _db_free_strict_branch_scheduler(monkeypatch, tmp_path, journal_root)

    status = _completion_status(scheduler, _IDENTITY_TARGET_CYCLE)
    selected = _selected_backfill_cycles(scheduler)

    assert status == "gap"
    assert selected == [_IDENTITY_TARGET_CYCLE]
    assert entry_path.read_bytes() == before


def test_db_free_strict_branch_matching_lineage_still_reports_complete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Control for task 6.1: the hoisted gate is additive tightening only —
    the expected token still completes on the strict/successor exit."""
    journal_root = tmp_path / "journal"
    entry_path = _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=_init_state_id_for(
            _IDENTITY_TARGET_CYCLE, lead_hours=_IDENTITY_REQUIRED_LEAD_HOURS
        ),
    )
    before = entry_path.read_bytes()
    scheduler = _db_free_strict_branch_scheduler(monkeypatch, tmp_path, journal_root)

    status = _completion_status(scheduler, _IDENTITY_TARGET_CYCLE)
    selected = _selected_backfill_cycles(scheduler)

    assert status == "complete"
    assert selected == ["2026-05-21T06:00:00Z"]
    assert entry_path.read_bytes() == before


@pytest.mark.parametrize(
    ("leg", "recorded_init_state_id"),
    [
        # (a) no recorded identity at all.
        ("absent", None),
        # (b) suffix-less legacy id equal to the expected base prefix.
        ("suffix_less_legacy", "state_gfs_model_a_2026052100"),
        # (c) DIFFERENT base key: an earlier-valid_time fallback warm start,
        # legally selected under NHMS_REQUIRE_FORECAST_WARM_START=false.
        ("earlier_valid_time_fallback", None),
    ],
)
def test_no_judgement_shapes_preserve_completed_skip(
    tmp_path: Path,
    leg: str,
    recorded_init_state_id: str | None,
) -> None:
    """Every non-mismatch shape keeps legacy skip behavior (P1-3 false-positive pin)."""
    journal_root = tmp_path / "journal"
    if leg == "earlier_valid_time_fallback":
        recorded_init_state_id = _init_state_id_for("2026-05-20T18:00:00Z", lead_hours=6)
        assert not recorded_init_state_id.startswith("state_gfs_model_a_2026052100")
    if leg == "suffix_less_legacy":
        assert recorded_init_state_id == "state_gfs_model_a_2026052100"
    _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=recorded_init_state_id,
    )
    scheduler = _journal_backed_scheduler(tmp_path, journal_root)

    assert _completion_status(scheduler, _IDENTITY_TARGET_CYCLE) == "complete"
    assert _selected_backfill_cycles(scheduler) == ["2026-05-21T06:00:00Z"]


def test_quarantined_cycle_exits_quarantine_after_rerun_records_expected_identity(
    tmp_path: Path,
) -> None:
    """3.5(a): one re-run that records the expected token leaves the quarantine."""
    journal_root = tmp_path / "journal"
    _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=_init_state_id_for(_IDENTITY_TARGET_CYCLE, lead_hours=12),
    )
    scheduler = _journal_backed_scheduler(tmp_path, journal_root)
    assert _completion_status(scheduler, _IDENTITY_TARGET_CYCLE) == "gap"
    assert _selected_backfill_cycles(scheduler) == [_IDENTITY_TARGET_CYCLE]

    # The re-run rewrites the latest view with the correct predecessor.
    _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=_init_state_id_for(
            _IDENTITY_TARGET_CYCLE, lead_hours=_IDENTITY_REQUIRED_LEAD_HOURS
        ),
    )
    # Fresh repository + scheduler for the second probe: the next scheduler
    # pass really re-reads the journal, so this must not depend on the
    # per-repository memo being invalidated by mtime_ns granularity.
    scheduler = _journal_backed_scheduler(tmp_path, journal_root)

    assert _completion_status(scheduler, _IDENTITY_TARGET_CYCLE) == "complete"
    assert _selected_backfill_cycles(scheduler) == ["2026-05-21T06:00:00Z"]


def test_rerun_reselecting_same_wrong_suffix_state_stays_quarantined(
    tmp_path: Path,
) -> None:
    """3.5(b) a re-run that re-selects the same wrong-suffix state stays
    quarantined and keeps occupying the source's single oldest-first backfill
    slot -- as long as no provenance-stamped quarantine rerun master has
    recorded that token even once.  This fixture writes no forecast cohort
    master at all, so the #1157 breaker counts zero occurrences and stays
    disengaged; the slot-release behaviour once a STAMPED master carries the
    token is pinned separately below, next to its mirror leg pinning that
    unstamped same-token masters keep the slot however many there are.

    This is the deterministic env=false base-key re-selection class
    (``chain_forecast_state.py`` exact lookup with no cycle_id/lead_hours ->
    ``state_manager`` ``min(state_id)`` over the base key).  It is pinned here
    on purpose: the loop is operator-visible via the typed retry reason and is
    bounded to one slot per source (see the proposal's Design decisions), so
    the shape is accepted rather than silently discovered in production.
    """
    journal_root = tmp_path / "journal"
    wrong_suffix_id = _init_state_id_for(_IDENTITY_TARGET_CYCLE, lead_hours=12)
    _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=wrong_suffix_id,
    )
    scheduler = _journal_backed_scheduler(tmp_path, journal_root)
    assert _completion_status(scheduler, _IDENTITY_TARGET_CYCLE) == "gap"
    assert _selected_backfill_cycles(scheduler) == [_IDENTITY_TARGET_CYCLE]

    # Re-run re-selects the SAME wrong-suffix state.
    _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=wrong_suffix_id,
    )
    # Fresh repository + scheduler: the second probe is a real second read.
    scheduler = _journal_backed_scheduler(tmp_path, journal_root)

    assert _completion_status(scheduler, _IDENTITY_TARGET_CYCLE) == "gap"
    # Still head-of-line (``available_gaps[:1]``) -> resubmitted once per round.
    assert _selected_backfill_cycles(scheduler) == [_IDENTITY_TARGET_CYCLE]


# ---------------------------------------------------------------------------
# §8.7 quarantine breaker (#1157 D4): a cycle whose ONLY reason for being a gap
# is a quarantine that a provenance-stamped rerun already re-recorded stops
# holding the source's single oldest-first execution slot — while still being
# reported as a gap, never as complete.
# ---------------------------------------------------------------------------


def _discover_cycles_with_evidence(
    scheduler: ProductionScheduler,
) -> tuple[list[str], list[dict[str, Any]]]:
    cycles, evidence = scheduler._discover_cycles(
        _dt(_IDENTITY_NOW), models=scheduler._discover_models()[0]
    )
    return (
        [scheduler_module._format_utc(cycle.discovery.cycle_time) for cycle in cycles],
        list(evidence),
    )


def test_breaker_engaged_gap_releases_the_backfill_execution_slot(tmp_path: Path) -> None:
    """E4: the next gap executes; the released cycle is evidence-only and still a gap."""
    journal_root = tmp_path / "journal"
    stale_id = _init_state_id_for(_IDENTITY_TARGET_CYCLE, lead_hours=12)
    expected_id = _init_state_id_for(
        _IDENTITY_TARGET_CYCLE, lead_hours=_IDENTITY_REQUIRED_LEAD_HOURS
    )
    entry_path = _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=stale_id,
        jobs=_breaker_engaged_masters(_IDENTITY_TARGET_CYCLE, stale_id),
    )
    before = entry_path.read_bytes()
    scheduler = _journal_backed_scheduler(tmp_path, journal_root)

    selected, evidence = _discover_cycles_with_evidence(scheduler)

    # The later gap takes the slot; the breaker-engaged cycle does not.
    assert selected == ["2026-05-21T06:00:00Z"]
    # No ADMIT: the released cycle is still scored as a gap.
    assert _completion_status(scheduler, _IDENTITY_TARGET_CYCLE) == "gap"

    released = [
        item
        for item in evidence
        if item.get("selection_reason") == "journal_predecessor_identity_quarantine_breaker_engaged"
    ]
    assert len(released) == 1
    assert released[0]["selection_status"] == "not_selected"
    assert released[0]["cycle_time_utc"] == _IDENTITY_TARGET_CYCLE
    quarantine = released[0]["journal_predecessor_identity_quarantine"]
    assert quarantine["occurrence_threshold"] == 1
    assert quarantine["models"] == [
        {
            "model_id": "model_a",
            "recorded_init_state_id": stale_id,
            "expected_init_state_id": expected_id,
            "occurrences": 1,
        }
    ]
    audit = next(item for item in evidence if item.get("type") == "backfill_audit")
    assert audit["breaker_released_gap_count"] == 1
    assert audit["selected_count"] == 1
    # Read-only invariant: the breaker never rewrites the journal.
    assert entry_path.read_bytes() == before


@pytest.mark.parametrize("leg", ["defect_run_only", "unstamped_replacement"])
def test_unstamped_recordings_keep_the_backfill_execution_slot(
    tmp_path: Path,
    leg: str,
) -> None:
    """E4 (R1) control: without a stamped rerun the cycle keeps its slot.

    ``unstamped_replacement`` is the discovery-side face of the Class-B
    defect: two same-token masters exist, but the second came from an
    unrelated whitelisted resubmit.  Releasing the slot here would strand a
    cycle whose quarantine rerun has not even been attempted.
    """
    journal_root = tmp_path / "journal"
    stale_id = _init_state_id_for(_IDENTITY_TARGET_CYCLE, lead_hours=12)
    jobs = [_completed_submission_master(_IDENTITY_TARGET_CYCLE, stale_id)]
    if leg == "unstamped_replacement":
        jobs.append(
            _completed_submission_master(
                _IDENTITY_TARGET_CYCLE, stale_id, job_suffix="_retry_1"
            )
        )
    _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=stale_id,
        jobs=jobs,
    )
    scheduler = _journal_backed_scheduler(tmp_path, journal_root)

    selected, evidence = _discover_cycles_with_evidence(scheduler)

    assert selected == [_IDENTITY_TARGET_CYCLE]
    assert not any(
        item.get("selection_reason") == "journal_predecessor_identity_quarantine_breaker_engaged"
        for item in evidence
    )


def test_mixed_gap_cycle_keeps_the_slot_for_its_incomplete_model(tmp_path: Path) -> None:
    """E4 mixed leg: one breaker-engaged model must not starve a real one.

    ``model_b`` has no journal entry at all for cycle T, so the cycle is a gap
    for a genuine reason as well.  Releasing the slot here would leave
    ``model_b``'s real work permanently unexecuted, which is the opposite of
    the fail-toward-liveness the breaker is for.
    """
    journal_root = tmp_path / "journal"
    stale_id = _init_state_id_for(_IDENTITY_TARGET_CYCLE, lead_hours=12)
    _seed_completed_journal_cycle(
        journal_root,
        cycle_time=_IDENTITY_TARGET_CYCLE,
        init_state_id=stale_id,
        jobs=_breaker_engaged_masters(_IDENTITY_TARGET_CYCLE, stale_id),
    )
    scheduler = _build_scheduler(
        tmp_path,
        now=_dt(_IDENTITY_NOW),
        cycle_times=list(_IDENTITY_CYCLE_TIMES),
        backfill_enabled=True,
        max_cycles_per_source=len(_IDENTITY_CYCLE_TIMES),
        active_repository=FileOrchestrationJournalRepository(journal_root),
        models=[_model("model_a", "basin_a"), _model("model_b", "basin_b")],
    )

    selected, evidence = _discover_cycles_with_evidence(scheduler)

    assert selected == [_IDENTITY_TARGET_CYCLE]
    assert not any(
        item.get("selection_reason") == "journal_predecessor_identity_quarantine_breaker_engaged"
        for item in evidence
    )


def test_partially_engaged_gap_cycle_keeps_the_slot(tmp_path: Path) -> None:
    """E4 (R1 / C1) partial engagement: a second model still owed a rerun keeps the slot.

    Both models are journal-completed with their OWN wrong-suffix token, so the
    cycle is a gap purely from §8.7 — but only ``model_a`` has a stamped rerun
    behind it.  ``model_b``'s quarantine has never been retried, so the cycle
    still has work the scheduler can progress and must keep the execution slot.

    This is the discriminating leg for the per-model conjunct: turning
    ``_breaker_engaged_gap_identities``' "any non-engaged stale model keeps the
    slot" return into a skip releases this cycle, which the assertions below
    catch.
    """
    journal_root = tmp_path / "journal"
    stale_a = _init_state_id_for(_IDENTITY_TARGET_CYCLE, lead_hours=12)
    stale_b = _init_state_id_for(_IDENTITY_TARGET_CYCLE, lead_hours=12, model_id="model_b")
    parsed = _dt(_IDENTITY_TARGET_CYCLE)
    run_id = f"cycle_gfs_{format_cycle_time(parsed)}"

    def master(job_suffix: str, identities: list[dict[str, Any]], stamp: list[str] | None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "job_id": f"job_{run_id}_forecast{job_suffix}",
            "run_id": run_id,
            "cycle_id": cycle_id_for("gfs", parsed),
            "candidate_id": run_id,
            "job_type": "run_shud_forecast_array",
            "stage": "forecast",
            "status": "succeeded",
            "model_id": None,
            "init_state_identities": identities,
        }
        if stamp is not None:
            row["journal_predecessor_quarantine_rerun_model_ids"] = list(stamp)
        return row

    jobs = [
        master(
            "",
            [
                {"array_task_id": 0, "model_id": "model_a", "init_state_id": stale_a},
                {"array_task_id": 1, "model_id": "model_b", "init_state_id": stale_b},
            ],
            None,
        ),
        # model_a's quarantine rerun came back with the same stale lineage;
        # model_b's has never run, so model_b is NOT breaker-engaged.
        master(
            "_retry_1",
            [{"array_task_id": 0, "model_id": "model_a", "init_state_id": stale_a}],
            ["model_a"],
        ),
    ]
    # The cohort masters are cycle-scoped, so each model's own latest view must
    # carry them for that model's journal read to see them.
    for model_id, stale_id in (("model_a", stale_a), ("model_b", stale_b)):
        _seed_completed_journal_cycle(
            journal_root,
            cycle_time=_IDENTITY_TARGET_CYCLE,
            init_state_id=stale_id,
            model_id=model_id,
            jobs=jobs,
        )
    scheduler = _build_scheduler(
        tmp_path,
        now=_dt(_IDENTITY_NOW),
        cycle_times=list(_IDENTITY_CYCLE_TIMES),
        backfill_enabled=True,
        max_cycles_per_source=len(_IDENTITY_CYCLE_TIMES),
        active_repository=FileOrchestrationJournalRepository(journal_root),
        models=[_model("model_a", "basin_a"), _model("model_b", "basin_b")],
    )

    # Fixture guard: model_a IS engaged, model_b is not — otherwise this leg
    # would pass for the trivial reason that nothing was engaged at all.
    repository = FileOrchestrationJournalRepository(journal_root)
    assert (
        repository.completed_pipeline_init_state_id_occurrences(
            source_id="gfs", cycle_time=parsed, model_id="model_a", init_state_id=stale_a
        )
        == 1
    )
    assert (
        repository.completed_pipeline_init_state_id_occurrences(
            source_id="gfs", cycle_time=parsed, model_id="model_b", init_state_id=stale_b
        )
        == 0
    )

    selected, evidence = _discover_cycles_with_evidence(scheduler)

    assert selected == [_IDENTITY_TARGET_CYCLE]
    assert not any(
        item.get("selection_reason") == "journal_predecessor_identity_quarantine_breaker_engaged"
        for item in evidence
    )


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


# ---------------------------------------------------------------------------
# #1735 lineage-scoped cycle completion: a recalibrated model must not gap the
# cycles that predate its own existence.  A recalibration mints a new
# content-derived ``model_id``; every completeness predicate is keyed strictly
# by ``model_id``, so without scoping every cycle in the lookback flips
# ``complete`` -> ``gap`` and the backfill lane pins itself on a cycle that can
# never close.
# ---------------------------------------------------------------------------

#: ``t*`` for the recalibrated model in these fixtures — the middle cycle of
#: ``_IDENTITY_CYCLE_TIMES``, so the window carries one pre-``t*`` cycle, the
#: cutover cycle itself, and one post-``t*`` cycle.
_LINEAGE_CUTOVER = "2026-05-21T06:00:00Z"
_LINEAGE_PRE_CUTOVER_CYCLE = "2026-05-21T00:00:00Z"
_LINEAGE_POST_CUTOVER_CYCLE = "2026-05-21T12:00:00Z"


class PerModelCompletionRepository(FakeActiveRepository):
    """``has_completed_pipeline`` keyed per ``(cycle_time, model_id)``.

    The production predicate is per-model — the db-free journal explicitly
    excludes another model's job rows (#1302) — so a lineage fixture keyed by
    cycle alone would score ``complete`` for the wrong reason.
    """

    def __init__(self, completed: set[tuple[str, str]]) -> None:
        super().__init__(active=False, completed=False)
        self._completed = {
            (scheduler_module._format_utc(_dt(cycle_time)), model_id)
            for cycle_time, model_id in completed
        }
        self.completion_queries: list[tuple[str, str]] = []

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id
        key = (scheduler_module._format_utc(cycle_time), model_id)
        self.completion_queries.append(key)
        return key in self._completed


class PredecessorJournalIdentityRepository(PerModelCompletionRepository):
    """Per-model completion PLUS a journal whose identity tokens are the predecessor's.

    Models the §8.7 trap: after the cutover the journal still holds the retired
    predecessor ``M``'s rows for the pre-``t*`` cycles, carrying ``M``'s
    ``init_state_id`` tokens.  The breaker must not re-flip a cycle that now
    scores ``complete`` by lineage scoping.
    """

    def __init__(
        self,
        completed: set[tuple[str, str]],
        *,
        recorded_init_state_id: str,
        occurrences: int,
    ) -> None:
        super().__init__(completed)
        self._recorded_init_state_id = recorded_init_state_id
        self._occurrences = occurrences
        self.identity_queries: list[tuple[str, str]] = []

    def completed_pipeline_init_state_id(
        self, *, source_id: str, cycle_time: datetime, model_id: str
    ) -> str | None:
        del source_id
        self.identity_queries.append((scheduler_module._format_utc(cycle_time), model_id))
        return self._recorded_init_state_id

    def completed_pipeline_init_state_id_occurrences(
        self, *, source_id: str, cycle_time: datetime, model_id: str, init_state_id: str
    ) -> int:
        del source_id, cycle_time, model_id, init_state_id
        return self._occurrences


def _lineage_clone_entry(
    tmp_path: Path,
    *,
    model_id: str,
    valid_time: str,
    cloned_from_model_id: str,
    source_id: str = "gfs",
    clone_gate_kind: str | None = "state_compatibility",
) -> dict[str, Any]:
    return _lineage_index_entry(
        object_root=tmp_path / "lineage-index" / "objects",
        model_id=model_id,
        valid_time=valid_time,
        source_id=source_id,
        cloned_from_model_id=cloned_from_model_id,
        clone_gate_kind=clone_gate_kind,
    )


def _lineage_scheduler(
    tmp_path: Path,
    *,
    models: Sequence[Mapping[str, Any]],
    active_repository: Any,
    clone_entries: Sequence[Mapping[str, Any]],
    cycle_times: Sequence[str] = _IDENTITY_CYCLE_TIMES,
) -> ProductionScheduler:
    """A backfill scheduler whose lineage resolver reads a REAL state index.

    Only the provider construction is bypassed (the db-free path builds it from
    env); the resolution itself runs through the production
    ``clone_lineage_signal`` -> ``resolve_lineage_cutover`` seam, so the
    earliest-row and no-ancestry-walk rules are genuinely exercised.
    """

    scheduler = _build_scheduler(
        tmp_path,
        now=_dt(_IDENTITY_NOW),
        cycle_times=list(cycle_times),
        backfill_enabled=True,
        max_cycles_per_source=len(cycle_times),
        active_repository=active_repository,
        models=list(models),
    )
    if clone_entries:
        scheduler._lineage_provider_cache = _lineage_index_repository(
            tmp_path / "lineage-index",
            [dict(entry) for entry in clone_entries],
            generated_at=_IDENTITY_NOW,
            now=_IDENTITY_NOW,
        )
    return scheduler


def test_pre_cutover_cycle_scores_complete_for_a_lineage_bearing_model(tmp_path: Path) -> None:
    """5.1: the cycles that predate ``M'`` are decided by the remaining models."""
    models = [_model("model_a", "basin_a"), _model("model_a_prime", "basin_b")]
    completed = {
        (_LINEAGE_PRE_CUTOVER_CYCLE, "model_a"),
        (_LINEAGE_CUTOVER, "model_a"),
        (_LINEAGE_CUTOVER, "model_a_prime"),
    }
    clone_entries = [
        _lineage_clone_entry(
            tmp_path,
            model_id="model_a_prime",
            valid_time=_LINEAGE_CUTOVER,
            cloned_from_model_id="model_a_legacy",
        )
    ]

    scheduler = _lineage_scheduler(
        tmp_path,
        models=models,
        active_repository=PerModelCompletionRepository(completed),
        clone_entries=clone_entries,
    )
    # Fixture guard: without the lineage resolver this very cycle is the
    # unclosable gap the change exists to remove.
    unscoped = _lineage_scheduler(
        tmp_path,
        models=models,
        active_repository=PerModelCompletionRepository(completed),
        clone_entries=[],
    )

    assert _completion_status(unscoped, _LINEAGE_PRE_CUTOVER_CYCLE) == "gap"
    assert _completion_status(scheduler, _LINEAGE_PRE_CUTOVER_CYCLE) == "complete"
    # The frontier is no longer pinned on a cycle that predates ``M'``.
    assert _selected_backfill_cycles(scheduler) == [_LINEAGE_POST_CUTOVER_CYCLE]


def test_pre_cutover_cycle_records_the_lineage_exclusion_in_evidence(tmp_path: Path) -> None:
    """4.1: the annotation names the model, its predecessor, and ``t*``."""
    scheduler = _lineage_scheduler(
        tmp_path,
        models=[_model("model_a", "basin_a"), _model("model_a_prime", "basin_b")],
        active_repository=PerModelCompletionRepository(
            {
                (_LINEAGE_PRE_CUTOVER_CYCLE, "model_a"),
                (_LINEAGE_CUTOVER, "model_a"),
                (_LINEAGE_CUTOVER, "model_a_prime"),
            }
        ),
        clone_entries=[
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime",
                valid_time=_LINEAGE_CUTOVER,
                cloned_from_model_id="model_a_legacy",
            )
        ],
    )

    _selected, evidence = _discover_cycles_with_evidence(scheduler)

    scoped_out = [
        item for item in evidence if item.get("type") == "lineage_scoped_out_pre_cutover"
    ]
    assert [item["cycle_time_utc"] for item in scoped_out] == [_LINEAGE_PRE_CUTOVER_CYCLE]
    assert scoped_out[0]["model_id"] == "model_a_prime"
    assert scoped_out[0]["predecessor_model_id"] == "model_a_legacy"
    assert scoped_out[0]["cutover_valid_time"] == _LINEAGE_CUTOVER
    assert scoped_out[0]["reason"] == "lineage_scoped_out_pre_cutover"


def test_cutover_cycle_itself_still_requires_genuine_completion(tmp_path: Path) -> None:
    """5.2: the boundary is STRICT — ``C == t*`` is scored exactly as any cycle."""
    models = [_model("model_a", "basin_a"), _model("model_a_prime", "basin_b")]
    clone_entries = [
        _lineage_clone_entry(
            tmp_path,
            model_id="model_a_prime",
            valid_time=_LINEAGE_CUTOVER,
            cloned_from_model_id="model_a_legacy",
        )
    ]
    incomplete = _lineage_scheduler(
        tmp_path,
        models=models,
        active_repository=PerModelCompletionRepository(
            {(_LINEAGE_PRE_CUTOVER_CYCLE, "model_a"), (_LINEAGE_CUTOVER, "model_a")}
        ),
        clone_entries=clone_entries,
    )
    complete = _lineage_scheduler(
        tmp_path,
        models=models,
        active_repository=PerModelCompletionRepository(
            {
                (_LINEAGE_PRE_CUTOVER_CYCLE, "model_a"),
                (_LINEAGE_CUTOVER, "model_a"),
                (_LINEAGE_CUTOVER, "model_a_prime"),
            }
        ),
        clone_entries=clone_entries,
    )

    assert _completion_status(incomplete, _LINEAGE_CUTOVER) == "gap"
    assert _completion_status(complete, _LINEAGE_CUTOVER) == "complete"
    # And the cutover cycle is the frontier while it is genuinely incomplete.
    assert _selected_backfill_cycles(incomplete) == [_LINEAGE_CUTOVER]


def test_model_without_lineage_is_scored_exactly_as_before(tmp_path: Path) -> None:
    """5.3 (scoring surface): no clone row ⇒ in scope for every cycle."""
    models = [_model("model_a", "basin_a"), _model("model_b", "basin_b")]
    completed = {
        (_LINEAGE_PRE_CUTOVER_CYCLE, "model_a"),
        (_LINEAGE_CUTOVER, "model_a"),
        (_LINEAGE_CUTOVER, "model_b"),
    }
    # An index that carries a clone row for a DIFFERENT model: the resolver is
    # wired and reads real data, and still says "no lineage" for these two.
    scheduler = _lineage_scheduler(
        tmp_path,
        models=models,
        active_repository=PerModelCompletionRepository(completed),
        clone_entries=[
            _lineage_clone_entry(
                tmp_path,
                model_id="model_z_prime",
                valid_time=_LINEAGE_CUTOVER,
                cloned_from_model_id="model_z",
            )
        ],
    )

    assert (
        scheduler._lineage_cutover_for_model_source("model_b", "gfs") is None
    )
    assert _completion_status(scheduler, _LINEAGE_PRE_CUTOVER_CYCLE) == "gap"
    assert _completion_status(scheduler, _LINEAGE_CUTOVER) == "complete"
    assert _selected_backfill_cycles(scheduler) == [_LINEAGE_PRE_CUTOVER_CYCLE]


def test_scope_emptied_by_the_lineage_filter_is_not_a_gap(tmp_path: Path) -> None:
    """5.4 leg 1 (D5): every model scoped out ⇒ not a gap, not selected."""
    scheduler = _lineage_scheduler(
        tmp_path,
        models=[_model("model_a_prime", "basin_a")],
        active_repository=PerModelCompletionRepository(
            {
                (_LINEAGE_CUTOVER, "model_a_prime"),
                (_LINEAGE_POST_CUTOVER_CYCLE, "model_a_prime"),
            }
        ),
        clone_entries=[
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime",
                valid_time=_LINEAGE_CUTOVER,
                cloned_from_model_id="model_a_legacy",
            )
        ],
    )

    assert _completion_status(scheduler, _LINEAGE_PRE_CUTOVER_CYCLE) == "complete"
    assert _selected_backfill_cycles(scheduler) == []


def test_scope_empty_before_the_lineage_filter_is_still_a_gap(tmp_path: Path) -> None:
    """5.4 leg 2 (D5): the unconfigured-models misconfiguration guard is untouched.

    Same scheduler, same wired resolver — only the model set is empty.  The
    two empty causes must not share a verdict.
    """
    scheduler = _lineage_scheduler(
        tmp_path,
        models=[_model("model_a_prime", "basin_a")],
        active_repository=PerModelCompletionRepository(set()),
        clone_entries=[
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime",
                valid_time=_LINEAGE_CUTOVER,
                cloned_from_model_id="model_a_legacy",
            )
        ],
    )
    adapter = scheduler.adapters["gfs"]
    discovery = _discovery_for_time(adapter, _LINEAGE_PRE_CUTOVER_CYCLE)

    assert (
        scheduler._cycle_completion_status(
            discovery,
            (),
            horizon=scheduler_module._source_horizon_metadata(discovery, adapter),
        )
        == "gap"
    )


def test_predecessor_journal_identity_does_not_reengage_the_breaker(tmp_path: Path) -> None:
    """5.5 / trap 2: ``M``'s stale tokens must not re-flip a lineage-scoped cycle."""
    # ``M``'s token for the pre-``t*`` cycle: a different base key from any
    # expectation composed for ``model_a`` / ``model_a_prime``.
    predecessor_token = _init_state_id_for(
        _LINEAGE_PRE_CUTOVER_CYCLE, lead_hours=6, model_id="model_a_legacy"
    )
    repository = PredecessorJournalIdentityRepository(
        {(_LINEAGE_PRE_CUTOVER_CYCLE, "model_a"), (_LINEAGE_CUTOVER, "model_a")},
        recorded_init_state_id=predecessor_token,
        # Far above the breaker threshold: if the breaker were reachable it
        # would engage, so "not engaged" cannot pass for a trivial reason.
        occurrences=99,
    )
    scheduler = _lineage_scheduler(
        tmp_path,
        models=[_model("model_a", "basin_a"), _model("model_a_prime", "basin_b")],
        active_repository=repository,
        clone_entries=[
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime",
                valid_time=_LINEAGE_CUTOVER,
                cloned_from_model_id="model_a_legacy",
            )
        ],
    )

    status = _completion_status(scheduler, _LINEAGE_PRE_CUTOVER_CYCLE)
    selected, evidence = _discover_cycles_with_evidence(scheduler)

    assert status == "complete"
    assert selected == [_LINEAGE_CUTOVER]
    assert not any(
        item.get("selection_reason") == "journal_predecessor_identity_quarantine_breaker_engaged"
        for item in evidence
    )
    # The scoped-out model never reaches the identity gate for that cycle.
    assert (_LINEAGE_PRE_CUTOVER_CYCLE, "model_a_prime") not in repository.identity_queries


def test_twice_recalibrated_model_is_scoped_by_its_own_cutover(tmp_path: Path) -> None:
    """5.7: ``M -> M' @ t1 -> M'' @ t2`` — the boundary is ``t2``, never ``t1``.

    Scoping on the chain's earliest ``t*`` would leave ``M''`` in scope for the
    ``[t1, t2)`` cycles that ``M'`` ran — an unclosable gap identical in shape
    to the incident this change fixes.
    """
    t1 = _LINEAGE_PRE_CUTOVER_CYCLE
    t2 = _LINEAGE_POST_CUTOVER_CYCLE
    in_between = _LINEAGE_CUTOVER
    scheduler = _lineage_scheduler(
        tmp_path,
        models=[_model("model_a", "basin_a"), _model("model_a_prime_prime", "basin_b")],
        active_repository=PerModelCompletionRepository(
            {(t1, "model_a"), (in_between, "model_a"), (t2, "model_a")}
        ),
        clone_entries=[
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime",
                valid_time=t1,
                cloned_from_model_id="model_a_legacy",
            ),
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime_prime",
                valid_time=t2,
                cloned_from_model_id="model_a_prime",
            ),
        ],
    )

    cutover = scheduler._lineage_cutover_for_model_source("model_a_prime_prime", "gfs")
    assert cutover is not None
    assert cutover.cutover_time == _dt(t2)
    assert cutover.predecessor_model_id == "model_a_prime"
    # ``[t1, t2)`` — the cycles ``M'`` ran — are scoped out for ``M''`` too.
    assert _completion_status(scheduler, t1) == "complete"
    assert _completion_status(scheduler, in_between) == "complete"
    # At its own ``t*`` it is in scope and has not completed.
    assert _completion_status(scheduler, t2) == "gap"
    assert _selected_backfill_cycles(scheduler) == [t2]


def test_backdated_reactivation_does_not_scope_out_cycles_the_identity_ran(
    tmp_path: Path,
) -> None:
    """5.8: two clone rows under one ``model_id`` ⇒ the boundary is the earliest."""
    t_a = _LINEAGE_CUTOVER
    t_b = _LINEAGE_POST_CUTOVER_CYCLE
    scheduler = _lineage_scheduler(
        tmp_path,
        models=[_model("model_a", "basin_a"), _model("model_a_prime", "basin_b")],
        active_repository=PerModelCompletionRepository(
            {
                (_LINEAGE_PRE_CUTOVER_CYCLE, "model_a"),
                (t_a, "model_a"),
                (t_b, "model_a"),
            }
        ),
        clone_entries=[
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime",
                valid_time=t_a,
                cloned_from_model_id="model_a_legacy",
            ),
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime",
                valid_time=t_b,
                cloned_from_model_id="model_a_legacy",
            ),
        ],
    )

    cutover = scheduler._lineage_cutover_for_model_source("model_a_prime", "gfs")
    assert cutover is not None
    assert cutover.cutover_time == _dt(t_a)
    # Before the earliest clone row: scoped out.
    assert _completion_status(scheduler, _LINEAGE_PRE_CUTOVER_CYCLE) == "complete"
    # The cycles the identity actually ran stay in scope and must complete.
    assert _completion_status(scheduler, t_a) == "gap"
    assert _selected_backfill_cycles(scheduler) == [t_a]


def test_lineage_cutover_is_scoped_per_source(tmp_path: Path) -> None:
    """5.9 / D3: GFS and IFS cut over independently."""
    gfs_cutover = _LINEAGE_CUTOVER
    ifs_cutover = _LINEAGE_POST_CUTOVER_CYCLE
    scheduler = _lineage_scheduler(
        tmp_path,
        models=[_model("model_a_prime", "basin_a")],
        active_repository=PerModelCompletionRepository(set()),
        clone_entries=[
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime",
                valid_time=gfs_cutover,
                cloned_from_model_id="model_a_legacy",
                source_id="gfs",
            ),
            _lineage_clone_entry(
                tmp_path,
                model_id="model_a_prime",
                valid_time=ifs_cutover,
                cloned_from_model_id="model_a_legacy",
                source_id="IFS",
            ),
        ],
    )
    context = scheduler._discovery_context()
    models = scheduler._discover_models()[0]
    gfs_discovery = _discovery_for_time(scheduler.adapters["gfs"], gfs_cutover)
    ifs_discovery = scheduler_module.CycleDiscovery(
        cycle_id=cycle_id_for("IFS", _dt(gfs_cutover)),
        source_id="IFS",
        cycle_time=_dt(gfs_cutover),
        cycle_hour=6,
        available=True,
        status="discovered",
    )
    scope = scheduler_module._scheduler_discovery._models_in_completion_scope

    # Same instant, two sources: in scope for GFS (``C == t*``), scoped out
    # for IFS (``C < t*``).
    assert [model.model_id for model in scope(context, gfs_discovery, models)] == [
        "model_a_prime"
    ]
    assert scope(context, ifs_discovery, models) == ()


def test_db_free_lineage_resolver_reads_the_published_state_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """1.2: production wiring — the db-free plane resolves from the loaded index.

    No fake provider: the scheduler builds its own
    ``FileStateSnapshotIndexRepository`` from env exactly as a node-22 pass
    does.  Also pins that the per-pass memo is cleared with the other file
    providers, so a recalibration landing mid-run is not invisible until
    process restart.
    """
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    clone_entry = _db_free_state_index_entry(
        roots,
        valid_time=_dt(_LINEAGE_CUTOVER),
        producer_cycle_time=_dt(_LINEAGE_PRE_CUTOVER_CYCLE),
        model_id="model_a_prime",
    )
    clone_entry["cloned_from_model_id"] = "model_a_legacy"
    clone_entry["cloned_from_state_id"] = "state_of_model_a_legacy"
    clone_entry["clone_gate_fingerprint"] = "sha256:" + "d" * 64
    clone_entry["clone_gate_kind"] = "state_compatibility"
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=_dt(_LINEAGE_CUTOVER),
        package_checksum=_DB_FREE_PACKAGE_CHECKSUM,
        generated_at=_dt(_IDENTITY_NOW),
        entries=[clone_entry],
    )
    scheduler = _build_scheduler(
        tmp_path,
        now=_dt(_IDENTITY_NOW),
        cycle_times=list(_IDENTITY_CYCLE_TIMES),
        backfill_enabled=True,
        max_cycles_per_source=len(_IDENTITY_CYCLE_TIMES),
        active_repository=PerModelCompletionRepository(set()),
        models=[_model("model_a_prime", "basin_a")],
    )

    cutover = scheduler._lineage_cutover_for_model_source("model_a_prime", "gfs")

    assert cutover is not None
    assert cutover.predecessor_model_id == "model_a_legacy"
    assert cutover.cutover_time == _dt(_LINEAGE_CUTOVER)
    assert scheduler._lineage_cutover_for_model_source("model_a", "gfs") is None
    assert ("model_a_prime", "gfs") in scheduler._lineage_cutover_cache
    scheduler._refresh_db_free_file_providers()
    assert scheduler._lineage_cutover_cache == {}
