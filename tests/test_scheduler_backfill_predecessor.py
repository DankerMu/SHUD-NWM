"""End-to-end tests for §8.6 predecessor emission (Issue #1081, R2-A1 fix).

Round-2 review R2-A1 found that ``services.orchestrator.scheduler_backfill_
predecessor.emit_predecessor_candidates`` had ZERO test coverage — the whole
415-line module and the §8.6 spec Scenario "predecessor selected before
successor" have no oracle.  This module fills that gap by driving
``emit_predecessor_candidates`` directly against realistic-shape blocked
entries and asserting the observable AC5 behavior:

- The emitted predecessor candidate is *prepended* before the successor
  (§8.6 ordering).
- Emission gates on raw-manifest readiness with distinguishable reasons
  (env-unwired vs manifest-not-ready — R2-B4).
- Duplicate predecessor emission is suppressed.
- The predecessor's own §8 gate can block emission (Backfill respects
  generation identity).
- Emission is bounded by ``MAX_PREDECESSOR_EMISSIONS = 256``.
- The R2-B3 ``max_candidates`` cap is enforced (predecessor prepend cannot
  bypass the fail-closed governance limit).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_backfill_predecessor as _bf
from workers.data_adapters.base import CycleDiscovery

SchedulerCandidate = scheduler_module.SchedulerCandidate


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _candidate(
    *,
    candidate_id: str,
    cycle_id: str,
    cycle_time: datetime,
    model_id: str = "model_a",
    source_id: str = "gfs",
    status: str = "blocked",
    reason: str | None = None,
    state_evidence: dict[str, Any] | None = None,
) -> SchedulerCandidate:
    """Construct a minimal SchedulerCandidate for use in blocked / candidates."""
    return SchedulerCandidate(
        candidate_id=candidate_id,
        source_id=source_id,
        cycle_id=cycle_id,
        cycle_time_utc=cycle_time,
        model_id=model_id,
        basin_id="basin_a",
        basin_version_id="v1",
        river_network_version_id="v1",
        segment_count=1,
        output_segment_count=1,
        model_package_uri="s3://nhms/models/model_a/package.tar.gz",
        resource_profile={"package_checksum": "b" * 64},
        display_capabilities={},
        horizon={},
        scenario_id="scenario",
        run_id="run",
        forcing_version_id="forcing",
        status=status,
        reason=reason,
        state_evidence=state_evidence or {},
    )


def _predecessor_pending_evidence(
    *,
    predecessor_cycle_time: datetime = _dt("2026-07-06T00:00:00Z"),
    source_id: str = "gfs",
    lead_hours: int = 12,
    generation: str = "manifest-newgen",
) -> dict[str, Any]:
    """Build a ``registry_cutover_transition`` state evidence pointing at a
    §8.6 pending predecessor (the shape emit_predecessor_candidates scans)."""
    return {
        "registry_cutover_transition": {
            "decision": "block_predecessor_pending",
            "generation": generation,
            "selected_predecessor": {
                "source_id": source_id,
                "valid_time": predecessor_cycle_time.isoformat(),
                "lead_hours": lead_hours,
                "generation": generation,
                "cycle_id": f"{source_id}_" + predecessor_cycle_time.strftime("%Y%m%d%H"),
            },
        }
    }


class _FakeModel:
    def __init__(self, model_id: str = "model_a") -> None:
        self.model_id = model_id


def _blocked_candidate_factory(
    candidate: SchedulerCandidate,
    reason: str,
    *,
    state_evidence: dict[str, Any] | None = None,
) -> SchedulerCandidate:
    from dataclasses import replace

    merged = dict(candidate.state_evidence or {})
    merged.update(state_evidence or {})
    return replace(candidate, status="blocked", reason=reason, state_evidence=merged)


def _candidate_factory(
    *,
    discovery: CycleDiscovery,
    model: Any,
    horizon: Any,
) -> SchedulerCandidate:
    """Emit a fresh candidate for a synthesized predecessor discovery."""
    from dataclasses import replace

    return replace(
        _candidate(
            candidate_id=f"cand_{discovery.cycle_id}_{model.model_id}",
            cycle_id=discovery.cycle_id,
            cycle_time=discovery.cycle_time,
            model_id=model.model_id,
            source_id=discovery.source_id,
            status="pending",
        ),
        horizon=dict(horizon or {}),
    )


def _wire_manifest_ready(monkeypatch: Any, status: str = "ready") -> None:
    """Force the raw-manifest readiness probe to return the given status.

    The default behaviour of ``nfs_raw_manifest_readiness_from_env`` returns
    ``None`` (env-unset) which R2-B4 now distinguishes from not-ready.  Tests
    that want to drive the "ready" branch monkeypatch the helper.
    """
    from services.orchestrator import source_cycle_raw_manifest

    def _readiness(source_id: str, cycle_time: datetime) -> dict[str, Any]:
        return {
            "status": status,
            "source_id": source_id,
            "cycle_time": cycle_time.isoformat(),
        }

    monkeypatch.setattr(
        source_cycle_raw_manifest,
        "nfs_raw_manifest_readiness_from_env",
        _readiness,
    )


def _gate_ready(candidate: SchedulerCandidate, cycle: Any) -> dict[str, Any]:
    return {
        "ready": True,
        "status": "ready",
        "reason": None,
        "mode": "db_free_cold_new_model",
    }


def _gate_blocked(reason: str) -> Any:
    def _fn(candidate: SchedulerCandidate, cycle: Any) -> dict[str, Any]:
        return {
            "ready": False,
            "status": "blocked",
            "reason": reason,
            "mode": "db_free_state_continuity",
        }

    return _fn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_emit_predecessor_prepends_before_successor(monkeypatch: Any) -> None:
    """AC5: predecessor cycle T-12h must land at index 0, successor stays deferred."""
    _wire_manifest_ready(monkeypatch)
    successor = _candidate(
        candidate_id="cand_gfs_2026070612_model_a",
        cycle_id="gfs_2026070612",
        cycle_time=_dt("2026-07-06T12:00:00Z"),
        state_evidence=_predecessor_pending_evidence(),
    )
    other_admitted = _candidate(
        candidate_id="cand_ifs_2026070612_model_a",
        cycle_id="ifs_2026070612",
        cycle_time=_dt("2026-07-06T12:00:00Z"),
        source_id="ifs",
        status="pending",
    )
    candidates: list[SchedulerCandidate] = [other_admitted]
    blocked: list[SchedulerCandidate] = [successor]
    evidence = _bf.emit_predecessor_candidates(
        models=[_FakeModel()],
        cycles=[],
        candidates=candidates,
        blocked=blocked,
        candidate_factory=_candidate_factory,
        strict_warm_start_for_candidate=_gate_ready,
        blocked_candidate_factory=_blocked_candidate_factory,
    )
    # Prepended: predecessor first, then the previously-admitted candidate.
    assert len(candidates) == 2
    assert candidates[0].cycle_time_utc == _dt("2026-07-06T00:00:00Z")
    assert candidates[0].source_id == "gfs"
    assert candidates[0].model_id == "model_a"
    assert candidates[1].candidate_id == "cand_ifs_2026070612_model_a"
    # Successor stays deferred; its blocked entry is unchanged.
    assert len(blocked) == 1
    assert blocked[0].candidate_id == "cand_gfs_2026070612_model_a"
    # Emission evidence records the "emitted" status.
    assert any(record.get("status") == "emitted" for record in evidence)
    # The emitted predecessor carries the backfill marker in state_evidence.
    predecessor = candidates[0]
    marker = predecessor.state_evidence.get("predecessor_backfill_marker")
    assert marker is not None
    assert marker["predecessor_backfill"] is True
    assert marker["successor_candidate_id"] == "cand_gfs_2026070612_model_a"


def test_emit_predecessor_gates_on_raw_manifest_readiness(monkeypatch: Any) -> None:
    """Env wired + manifest NOT ready → skipped w/ predecessor_raw_manifest_not_ready."""
    _wire_manifest_ready(monkeypatch, status="not_ready")
    successor = _candidate(
        candidate_id="cand_gfs_2026070612_model_a",
        cycle_id="gfs_2026070612",
        cycle_time=_dt("2026-07-06T12:00:00Z"),
        state_evidence=_predecessor_pending_evidence(),
    )
    candidates: list[SchedulerCandidate] = []
    blocked: list[SchedulerCandidate] = [successor]
    evidence = _bf.emit_predecessor_candidates(
        models=[_FakeModel()],
        cycles=[],
        candidates=candidates,
        blocked=blocked,
        candidate_factory=_candidate_factory,
        strict_warm_start_for_candidate=_gate_ready,
        blocked_candidate_factory=_blocked_candidate_factory,
    )
    assert candidates == []
    assert len(blocked) == 1  # successor untouched
    reasons = [record.get("reason") for record in evidence]
    assert "predecessor_raw_manifest_not_ready" in reasons
    assert "predecessor_raw_manifest_env_unwired" not in reasons


def test_emit_predecessor_env_unwired_reports_distinct_reason() -> None:
    """R2-B4: env-unset returns None → distinct env_unwired reason, not the
    same enum as legitimately not-ready.  No monkeypatch of the readiness
    helper — its default returns None when both env vars are unset."""
    successor = _candidate(
        candidate_id="cand_gfs_2026070612_model_a",
        cycle_id="gfs_2026070612",
        cycle_time=_dt("2026-07-06T12:00:00Z"),
        state_evidence=_predecessor_pending_evidence(),
    )
    candidates: list[SchedulerCandidate] = []
    blocked: list[SchedulerCandidate] = [successor]
    evidence = _bf.emit_predecessor_candidates(
        models=[_FakeModel()],
        cycles=[],
        candidates=candidates,
        blocked=blocked,
        candidate_factory=_candidate_factory,
        strict_warm_start_for_candidate=_gate_ready,
        blocked_candidate_factory=_blocked_candidate_factory,
    )
    assert candidates == []
    reasons = [record.get("reason") for record in evidence]
    assert "predecessor_raw_manifest_env_unwired" in reasons


def test_emit_predecessor_skips_duplicate(monkeypatch: Any) -> None:
    """A pre-existing candidate at the predecessor key → skipped w/
    predecessor_already_present; no double-emit."""
    _wire_manifest_ready(monkeypatch)
    successor = _candidate(
        candidate_id="cand_gfs_2026070612_model_a",
        cycle_id="gfs_2026070612",
        cycle_time=_dt("2026-07-06T12:00:00Z"),
        state_evidence=_predecessor_pending_evidence(),
    )
    already_present_predecessor = _candidate(
        candidate_id="cand_gfs_2026070600_model_a",
        cycle_id="gfs_2026070600",
        cycle_time=_dt("2026-07-06T00:00:00Z"),
        status="pending",
    )
    candidates: list[SchedulerCandidate] = [already_present_predecessor]
    blocked: list[SchedulerCandidate] = [successor]
    evidence = _bf.emit_predecessor_candidates(
        models=[_FakeModel()],
        cycles=[],
        candidates=candidates,
        blocked=blocked,
        candidate_factory=_candidate_factory,
        strict_warm_start_for_candidate=_gate_ready,
        blocked_candidate_factory=_blocked_candidate_factory,
    )
    # No new prepend — length is unchanged.
    assert len(candidates) == 1
    assert candidates[0].candidate_id == "cand_gfs_2026070600_model_a"
    reasons = [record.get("reason") for record in evidence]
    assert "predecessor_already_present" in reasons


def test_emit_predecessor_own_gate_blocks_prepend(monkeypatch: Any) -> None:
    """The predecessor's own §8 gate blocks (e.g. its own declaration is
    stale) → predecessor is appended to blocked with the gating reason,
    NOT prepended to admitted candidates."""
    _wire_manifest_ready(monkeypatch)
    successor = _candidate(
        candidate_id="cand_gfs_2026070612_model_a",
        cycle_id="gfs_2026070612",
        cycle_time=_dt("2026-07-06T12:00:00Z"),
        state_evidence=_predecessor_pending_evidence(),
    )
    candidates: list[SchedulerCandidate] = []
    blocked: list[SchedulerCandidate] = [successor]
    evidence = _bf.emit_predecessor_candidates(
        models=[_FakeModel()],
        cycles=[],
        candidates=candidates,
        blocked=blocked,
        candidate_factory=_candidate_factory,
        strict_warm_start_for_candidate=_gate_blocked("registry_cutover_declaration_stale"),
        blocked_candidate_factory=_blocked_candidate_factory,
    )
    assert candidates == []
    # successor + newly blocked predecessor
    assert len(blocked) == 2
    predecessor_blocked = blocked[-1]
    assert predecessor_blocked.reason == "registry_cutover_declaration_stale"
    # The predecessor's blocked entry carries the marker so audit can trace.
    marker = predecessor_blocked.state_evidence.get("predecessor_backfill_marker")
    assert marker is not None
    assert marker["successor_candidate_id"] == "cand_gfs_2026070612_model_a"
    # Emission evidence records the block with the gate's reason.
    blocks = [record for record in evidence if record.get("status") == "blocked"]
    assert len(blocks) == 1
    assert blocks[0]["reason"] == "registry_cutover_declaration_stale"


def test_emit_predecessor_truncates_at_max_emissions(monkeypatch: Any) -> None:
    """Fixture with more than MAX_PREDECESSOR_EMISSIONS pending predecessors →
    emitter caps at MAX and surfaces the truncation evidence entry.

    We build 300 successors — each with a UNIQUE ``(source_id, cycle_time,
    model_id)`` predecessor key so ``existing_blocked_keys`` doesn't dedup
    them away.  Same predecessor cycle_time (2026-07-06T00Z) works when the
    ``model_id`` differs per successor.
    """
    _wire_manifest_ready(monkeypatch)
    successors: list[SchedulerCandidate] = []
    for i in range(300):
        pred_time = _dt("2026-07-06T00:00:00Z")
        succ_time = _dt("2026-07-06T12:00:00Z")
        model_id = f"model_{i:04d}"
        succ = _candidate(
            candidate_id=f"cand_gfs_{succ_time.strftime('%Y%m%d%H')}_{model_id}",
            cycle_id=f"gfs_{succ_time.strftime('%Y%m%d%H')}",
            cycle_time=succ_time,
            model_id=model_id,
            state_evidence={
                "registry_cutover_transition": {
                    "decision": "block_predecessor_pending",
                    "generation": "manifest-newgen",
                    "selected_predecessor": {
                        "source_id": "gfs",
                        "valid_time": pred_time.isoformat(),
                        "lead_hours": 12,
                        "generation": "manifest-newgen",
                    },
                }
            },
        )
        successors.append(succ)
    blocked: list[SchedulerCandidate] = list(successors)
    candidates: list[SchedulerCandidate] = []
    evidence = _bf.emit_predecessor_candidates(
        models=[_FakeModel(model_id=f"model_{i:04d}") for i in range(300)],
        cycles=[],
        candidates=candidates,
        blocked=blocked,
        candidate_factory=_candidate_factory,
        strict_warm_start_for_candidate=_gate_ready,
        blocked_candidate_factory=_blocked_candidate_factory,
    )
    # Emitter capped at MAX_PREDECESSOR_EMISSIONS admitted predecessors.
    admitted_events = [record for record in evidence if record.get("status") == "emitted"]
    assert len(admitted_events) == _bf.MAX_PREDECESSOR_EMISSIONS
    assert len(candidates) == _bf.MAX_PREDECESSOR_EMISSIONS
    # Truncated record surfaces the cap.
    truncated_events = [record for record in evidence if record.get("status") == "truncated"]
    assert len(truncated_events) == 1
    assert truncated_events[0]["reason"] == "predecessor_emission_cap_reached"
    assert truncated_events[0]["cap"] == _bf.MAX_PREDECESSOR_EMISSIONS


def test_emit_predecessor_prepend_respects_max_candidates(monkeypatch: Any) -> None:
    """R2-B3: predecessor prepend must not bypass the fail-closed max_candidates
    cap.  Fixture: 4 pre-emission candidates + 3 pending predecessors, cap=5 →
    the 2nd prepend attempt raises SchedulerResourceLimitError before appending."""
    _wire_manifest_ready(monkeypatch)
    # 3 distinct successor blocks whose predecessors are all admittable.
    blocked: list[SchedulerCandidate] = []
    for hour_shift in (12, 24, 36):
        succ_time = _dt("2026-07-06T00:00:00Z") + timedelta(hours=hour_shift)
        pred_time = succ_time - timedelta(hours=12)
        blocked.append(
            _candidate(
                candidate_id=f"cand_gfs_{succ_time.strftime('%Y%m%d%H')}_model_a",
                cycle_id=f"gfs_{succ_time.strftime('%Y%m%d%H')}",
                cycle_time=succ_time,
                state_evidence={
                    "registry_cutover_transition": {
                        "decision": "block_predecessor_pending",
                        "generation": "manifest-newgen",
                        "selected_predecessor": {
                            "source_id": "gfs",
                            "valid_time": pred_time.isoformat(),
                            "lead_hours": 12,
                            "generation": "manifest-newgen",
                        },
                    }
                },
            )
        )
    # 4 pre-emission candidates that are unrelated to the pending predecessors.
    candidates: list[SchedulerCandidate] = [
        _candidate(
            candidate_id=f"cand_pre_{i}",
            cycle_id=f"gfs_2026070500_{i}",
            cycle_time=_dt("2026-07-05T00:00:00Z"),
            model_id=f"model_pre_{i}",
            status="pending",
        )
        for i in range(4)
    ]
    # Cap = 5.  Pre = 4 candidates + 3 blocked = 7, but blocked is included in
    # the sum.  After admitting the 1st predecessor: candidates=5, blocked=3,
    # admitted=1 → sum = 4 + 3 + 1 = 8 > 5 → the FIRST prepend already
    # exceeds the cap.  Assert a SchedulerResourceLimitError is raised.
    with pytest.raises(_bf.SchedulerResourceLimitError):
        _bf.emit_predecessor_candidates(
            models=[_FakeModel(model_id=f"model_pre_{i}") for i in range(4)]
            + [_FakeModel("model_a")],
            cycles=[],
            candidates=candidates,
            blocked=blocked,
            candidate_factory=_candidate_factory,
            strict_warm_start_for_candidate=_gate_ready,
            blocked_candidate_factory=_blocked_candidate_factory,
            max_candidates=5,
        )


def test_emit_predecessor_skips_active_pipeline(monkeypatch: Any) -> None:
    """R2-C3: a predecessor cycle with an active pipeline in the repository
    is skipped and the successor block gains a diagnostic marker."""
    _wire_manifest_ready(monkeypatch)
    successor = _candidate(
        candidate_id="cand_gfs_2026070612_model_a",
        cycle_id="gfs_2026070612",
        cycle_time=_dt("2026-07-06T12:00:00Z"),
        state_evidence=_predecessor_pending_evidence(),
    )
    candidates: list[SchedulerCandidate] = []
    blocked: list[SchedulerCandidate] = [successor]

    class _ActiveRepo:
        def has_active_pipeline(self, **kwargs: Any) -> bool:
            return True

    evidence = _bf.emit_predecessor_candidates(
        models=[_FakeModel()],
        cycles=[],
        candidates=candidates,
        blocked=blocked,
        candidate_factory=_candidate_factory,
        strict_warm_start_for_candidate=_gate_ready,
        blocked_candidate_factory=_blocked_candidate_factory,
        active_repository=_ActiveRepo(),
    )
    assert candidates == []
    assert len(blocked) == 1
    successor_after = blocked[0]
    marker = successor_after.state_evidence.get("predecessor_backfill_marker") or {}
    assert marker.get("predecessor_backfill_active_pipeline") is True
    reasons = [record.get("reason") for record in evidence]
    assert "predecessor_backfill_active_pipeline" in reasons
