"""Forecast-restart guards around the terminal-skip dispatch (#2396, #2407, #2408).

Every test drives the REAL ``build_candidates`` dispatch (through
``ProductionScheduler``), except the unit-level predicate pins of 2.4, which
call ``_apply_explicit_missing_forcing_repair_policy`` directly because the
predicates are that function's contract.

- #2396: the None-lane ``terminal_run_manifest_missing`` leg restarts at
  ``forecast`` and must consult the candidate's own forcing witness (#1843).
- #2408: on the strict lane an UNCONFIRMED §8.7 quarantine retry that lands on
  the stable missing-forcing blocker must not be reclassified by the explicit
  single-cycle repair authorization (owner decision (b)).
- #2407: on the strict lane the quarantine literal survives the strict
  warm-start upgrade helper (test only).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.scheduler_candidates import (
    _apply_explicit_missing_forcing_repair_policy,
    _decision_is_stable_missing_forcing_blocker,
)
from services.orchestrator.scheduler_state_types import CandidateStateDecision

# The scheduler-suite helpers (and the scheduler facade they wrap) are imported
# function-locally, the same way ``tests/test_scheduler_generation.py`` reuses
# them: a module-scope import would join this file to those suites' pinned
# importer closures in ``scripts/select_ci_tests.py``.  This file's CI route is
# the orchestrator directory rule, via the two module-scope imports above.

_MISSING_FORCING_REASONS = {"missing_forcing_package_uri", "forcing_version_row_absent"}
_CYCLE_TIME = "2026-05-21T06:00:00Z"


def _decision_of(entry: Any) -> CandidateStateDecision:
    """Rebuild the decision a blocked/candidate entry carries, for the #1846 predicate."""

    if isinstance(entry, Mapping):
        return CandidateStateDecision("blocked", str(entry["reason"]), dict(entry["state_evidence"]))
    return CandidateStateDecision("blocked", str(entry.reason), dict(entry.state_evidence))


# ---------------------------------------------------------------------------
# #2396 — None-lane ``terminal_run_manifest_missing`` consults the witness
# ---------------------------------------------------------------------------


def _assert_none_lane(state_evidence: Mapping[str, Any]) -> None:
    # 1.4 lane proof: the D8.9 compat regime nulls the strict warm start, so the
    # strict evidence (merged top-level as ``ready`` / ``candidate_state`` on the
    # strict lane) never reaches the candidate's evidence.
    assert "ready" not in state_evidence
    assert "candidate_state" not in state_evidence


def test_none_lane_terminal_run_manifest_missing_without_own_forcing_blocks(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """1.1: a doomed ``forecast`` restart lands on the stable missing-forcing blocker."""
    from tests.test_scheduler_generation import _run_wiring_a_build_candidates

    candidates, blocked, skipped, _duplicates, _sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=None,
        seed_forcing_package=False,
        write_run_manifest=False,
    )

    assert candidates == []
    assert skipped == []
    (blocker,) = blocked
    assert blocker.reason in _MISSING_FORCING_REASONS
    evidence = blocker.state_evidence
    _assert_none_lane(evidence)
    # The guard was handed the run-manifest-missing retry as its planned retry.
    assert evidence["artifact_guard"]["planned_retry_reason"] == "terminal_run_manifest_missing"
    assert evidence["artifact_guard"]["planned_retry_decision"] == "retry_terminal_run_manifest_missing"
    assert _decision_is_stable_missing_forcing_blocker(_decision_of(blocker))


def test_none_lane_terminal_run_manifest_missing_with_own_forcing_is_unchanged(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """1.3: with the model's own forcing witnessed the retry is kept and annotated."""
    from tests.test_scheduler_generation import _run_wiring_a_build_candidates

    candidates, blocked, skipped, _duplicates, _sync = _run_wiring_a_build_candidates(
        monkeypatch,
        tmp_path,
        recorded_init_state_id=None,
        write_run_manifest=False,
    )

    assert blocked == []
    assert skipped == []
    (candidate,) = candidates
    evidence = candidate.state_evidence
    _assert_none_lane(evidence)
    assert evidence["decision"] == "retry_terminal_run_manifest_missing"
    assert evidence["reason"] == "terminal_run_manifest_missing"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"
    assert isinstance(evidence.get("forcing_provenance"), Mapping)
    assert evidence["forcing_provenance"]


# ---------------------------------------------------------------------------
# #2407 / #2408 — strict-lane ``terminal_completed_cycle`` quarantine
# ---------------------------------------------------------------------------


def _db_free_strict_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    own_forcing: bool,
    quarantine: bool = True,
    repair_missing_forcing: bool = True,
) -> tuple[list[Any], list[Any], list[dict[str, Any]], dict[str, str]]:
    """2.0: the db-free STRICT lane driven through the real ``_build_candidates``.

    ``NHMS_SCHEDULER_DB_FREE_REQUIRED=true`` with ``NHMS_REQUIRE_FORECAST_WARM_START``
    left unset, and a state-index checkpoint valid AT cycle T, so the strict
    warm-start evidence is real and ``ready`` (warm admission and
    ``_verified_repair_warm_state`` both pass).  The candidate is direct-grid,
    and the NFS raw manifest for (source, T) is on disk and recorded ready.

    ``quarantine=True``: the terminal state is a ``complete`` ``forecast_cycle``
    (hydro ``created``, outside the durable-success set), so it skips as
    ``terminal_completed_cycle`` -- a reason no strict branch routes -- and the
    else-leg quarantine fires on the stale token served by the injected journal
    identity accessor.  The raw state carries no ``run_manifest_initial_state``,
    so the run manifest is ABSENT against the strict evidence (design D3): the
    upgrade helper's early return is all that keeps the quarantine literal.

    ``quarantine=False``: an ordinary succeeded-forcing resume state, the
    non-quarantine missing-forcing shape the repair policy already authorizes.
    """
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.scheduler import ProductionSchedulerConfig
    from tests.test_production_scheduler import (
        FakeRegistry,
        ProductionScheduler,
        RawCandidateStateRepository,
        _completed_forecast_cycle_quarantine_state,
        _dt,
        _gfs_default_forecast_hours,
        _JournalIdentityRawCandidateStateRepository,
        _missing_forcing_repair_direct_grid_profile,
        _missing_forcing_retry_state,
        _model,
        _set_db_free_scheduler_env,
        _stale_journal_identity_tokens,
        _write_db_free_file_provider_fixtures,
        _write_db_free_state_index_fixture,
        _write_missing_forcing_repair_raw_manifest,
    )
    from tests.test_scheduler_generation import _state_index_entry
    from workers.data_adapters.base import CycleDiscovery

    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    monkeypatch.delenv("NHMS_REQUIRE_FORECAST_WARM_START", raising=False)
    cycle_time = _dt(_CYCLE_TIME)
    generated_at = _dt("2026-05-21T12:00:00Z")
    fixture = _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=_gfs_default_forecast_hours(),
        generated_at=generated_at,
        model=_model("model_a", "basin_a", resource_profile=_missing_forcing_repair_direct_grid_profile()),
        seed_forcing_package=own_forcing,
    )
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=cycle_time,
        package_checksum=fixture["package_checksum"],
        generated_at=generated_at,
        entries=[
            _state_index_entry(
                roots,
                valid_time=cycle_time,
                producer_cycle_time=_dt("2026-05-21T00:00:00Z"),
                package_checksum=fixture["package_checksum"],
            )
        ],
    )
    object_store_root = Path(roots["object_store_root"])
    readiness = _write_missing_forcing_repair_raw_manifest(object_store_root, cycle_time=cycle_time)
    model = scheduler_module._coerce_registered_model(
        {
            **fixture["model"],
            "resource_profile": {
                **dict(fixture["model"]["resource_profile"]),
                "package_checksum": fixture["package_checksum"],
            },
        }
    )
    discovery = CycleDiscovery(
        cycle_id="gfs_2026052106",
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_hour=6,
        available=True,
        status="discovered",
    )
    candidate = scheduler_module._candidate_for(discovery=discovery, model=model, horizon={})
    stale, expected = _stale_journal_identity_tokens(candidate)
    if quarantine:
        state = _completed_forecast_cycle_quarantine_state(candidate, forcing_package_uri=None)
        repository: Any = _JournalIdentityRawCandidateStateRepository(state, recorded_init_state_id=stale)
    else:
        state = _missing_forcing_retry_state(candidate, raw_manifest=None)
        repository = RawCandidateStateRepository(state)
    state["nfs_raw_manifest"] = dict(readiness)
    assert "run_manifest_initial_state" not in state
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            now=generated_at,
            allowed_cycle_hours_utc=(0, 6, 12, 18),
            # The repair window's own admission: exact, single-cycle, no backfill.
            lookback_hours=0,
            cycle_lag_hours=6,
            max_cycles_per_source=1,
            backfill_enabled=False,
            require_direct_grid=True,
            repair_missing_forcing=repair_missing_forcing,
            repair_missing_forcing_cycle_time=cycle_time if repair_missing_forcing else None,
            nfs_raw_manifest_root=object_store_root,
        ),
        registry=FakeRegistry([fixture["model"]]),
        adapters={},
        active_repository=repository,
        orchestrator_factory=lambda _source_id: pytest.fail("this seam must not build an orchestrator"),
    )
    candidates, blocked, skipped, _duplicates, _sync = scheduler._build_candidates(
        models=[model],
        cycles=[scheduler_module.SchedulerSourceCycle(discovery=discovery, horizon={})],
    )
    assert skipped == []
    return candidates, blocked, skipped, {"stale": stale, "expected": expected}


def _assert_strict_lane(state_evidence: Mapping[str, Any]) -> None:
    # Lane proof: the real db-free strict warm-start evidence (``ready`` plus the
    # checkpoint valid AT T) is merged into the candidate's evidence.
    assert state_evidence["ready"] is True
    assert state_evidence["status"] == "ready"
    assert state_evidence["candidate_state"]["valid_time"] == _CYCLE_TIME
    assert state_evidence["state_snapshot_index"]["status"] == "ready"


def _assert_quarantine_identity(state_evidence: Mapping[str, Any], stale: str, expected: str) -> None:
    assert state_evidence["journal_predecessor_identity"] == {
        "recorded_init_state_id": stale,
        "expected_init_state_id": expected,
        "required_lead_hours": 6,
        "quarantined_skip_reason": "terminal_completed_cycle",
    }


def test_strict_lane_quarantine_literal_survives_the_upgrade_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3.1 (#2407): the quarantine retry keeps its literal through the strict upgrade."""

    candidates, blocked, _skipped, tokens = _db_free_strict_build(tmp_path, monkeypatch, own_forcing=True)

    assert blocked == []
    (candidate,) = candidates
    state_evidence = candidate.state_evidence
    _assert_strict_lane(state_evidence)
    assert state_evidence["decision"] == "retry_journal_predecessor_identity_mismatch"
    assert state_evidence["reason"] == "journal_predecessor_identity_mismatch"
    assert state_evidence["restart_stage"] == "forecast"
    _assert_quarantine_identity(state_evidence, tokens["stale"], tokens["expected"])
    # The post-upgrade consultation witnessed THIS model's own package.
    assert state_evidence["forcing_provenance"]


def test_strict_lane_quarantine_without_own_forcing_and_no_repair_window_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline: with no repair window the quarantine lands on the stable blocker."""

    candidates, blocked, _skipped, tokens = _db_free_strict_build(
        tmp_path, monkeypatch, own_forcing=False, repair_missing_forcing=False
    )

    assert candidates == []
    (blocker,) = blocked
    assert blocker.reason in _MISSING_FORCING_REASONS
    assert "missing_forcing_repair" not in blocker.state_evidence
    _assert_strict_lane(blocker.state_evidence)
    _assert_quarantine_identity(blocker.state_evidence, tokens["stale"], tokens["expected"])
    assert _decision_is_stable_missing_forcing_blocker(_decision_of(blocker))


def test_strict_lane_unconfirmed_quarantine_blocker_is_refused_by_the_repair_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2.1/2.2 (#2408): the repair window must not reclassify a quarantine-descended blocker."""

    candidates, blocked, _skipped, tokens = _db_free_strict_build(tmp_path, monkeypatch, own_forcing=False)

    # No re-run of the stale lineage is emitted at all.
    assert candidates == []
    (blocker,) = blocked
    state_evidence = blocker.state_evidence
    _assert_strict_lane(state_evidence)
    assert "operator_reentry_confirmation" not in state_evidence
    assert blocker.reason in _MISSING_FORCING_REASONS
    assert state_evidence.get("decision") != "retry_repair_missing_forcing"
    repair = state_evidence["missing_forcing_repair"]
    assert repair["status"] == "rejected"
    assert repair["reason"] == "journal_predecessor_quarantine_present"
    assert repair["recorded_init_state_id"] == tokens["stale"]
    assert repair["expected_init_state_id"] == tokens["expected"]
    _assert_quarantine_identity(state_evidence, tokens["stale"], tokens["expected"])
    # Still drainable by the forcing backfill (#1846 contract).
    assert _decision_is_stable_missing_forcing_blocker(_decision_of(blocker))


def test_ordinary_missing_forcing_blocker_is_still_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2.3 must-preserve: a non-quarantine blocker with the same preconditions reclassifies."""

    candidates, blocked, _skipped, _tokens = _db_free_strict_build(
        tmp_path, monkeypatch, own_forcing=False, quarantine=False
    )

    assert blocked == []
    (candidate,) = candidates
    state_evidence = candidate.state_evidence
    _assert_strict_lane(state_evidence)
    assert "journal_predecessor_identity" not in state_evidence
    assert state_evidence["decision"] == "retry_repair_missing_forcing"
    assert state_evidence["restart_stage"] == "forcing"
    assert state_evidence["missing_forcing_repair"]["status"] == "authorized"


# ---------------------------------------------------------------------------
# 2.3 / 2.4 — unit-level predicate and ordering pins
# ---------------------------------------------------------------------------


def _stable_blocker_decision(**extra: Any) -> CandidateStateDecision:
    artifact_guard: dict[str, Any] = {
        "artifact_type": "forcing_package_uri",
        "stable_classifier": "FORCING_VERSION_ROW_ABSENT",
        "artifact_exists": False,
        "unsafe_reason": None,
        "planned_retry_decision": "retry_terminal_run_manifest_missing",
        "planned_retry_reason": "terminal_run_manifest_missing",
        **dict(extra.pop("artifact_guard", {})),
    }
    evidence = {
        "classifier": "missing_upstream_artifact",
        "restart_stage": "forecast",
        "artifact_guard": artifact_guard,
        **extra,
    }
    decision = CandidateStateDecision("blocked", "forcing_version_row_absent", evidence)
    assert _decision_is_stable_missing_forcing_blocker(decision)
    return decision


_IDENTITY_BLOCK = {
    "recorded_init_state_id": "state_recorded",
    "expected_init_state_id": "state_expected",
    "required_lead_hours": 6,
    "quarantined_skip_reason": "terminal_completed_cycle",
}


def _apply_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: CandidateStateDecision,
) -> CandidateStateDecision:
    from tests.test_production_scheduler import (
        _complete_missing_forcing_repair_warm_state,
        _dt,
        _missing_forcing_repair_config,
        _missing_forcing_repair_direct_grid_profile,
        _scheduler_candidate_fixture,
        _write_missing_forcing_repair_raw_manifest,
    )

    cycle_time = _dt(_CYCLE_TIME)
    raw_root = tmp_path / "nfs-raw"
    readiness = _write_missing_forcing_repair_raw_manifest(raw_root, cycle_time=cycle_time)
    config = _missing_forcing_repair_config(
        tmp_path, monkeypatch, cycle_time=cycle_time, dry_run=True, raw_root=raw_root
    )
    candidate = replace(
        _scheduler_candidate_fixture(),
        resource_profile=_missing_forcing_repair_direct_grid_profile(),
    )
    result = _apply_explicit_missing_forcing_repair_policy(
        config,
        candidate,
        {"nfs_raw_manifest": dict(readiness)},
        decision,
        strict_warm_start=_complete_missing_forcing_repair_warm_state(),
    )
    assert result is not None
    return result


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"journal_predecessor_identity": dict(_IDENTITY_BLOCK)}, id="identity_block_only"),
        pytest.param(
            {"artifact_guard": {"planned_retry_decision": "retry_journal_predecessor_identity_mismatch"}},
            id="planned_retry_decision_only",
        ),
    ],
)
def test_each_quarantine_descent_predicate_alone_refuses_the_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, Any],
) -> None:
    """2.4: either predicate is sufficient."""

    result = _apply_policy(tmp_path, monkeypatch, _stable_blocker_decision(**extra))

    assert result.action == "blocked"
    assert result.evidence["missing_forcing_repair"]["status"] == "rejected"
    assert result.evidence["missing_forcing_repair"]["reason"] == "journal_predecessor_quarantine_present"
    assert _decision_is_stable_missing_forcing_blocker(result)


def test_repair_policy_without_either_predicate_is_not_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2.4 control: neither predicate -> the repair authorizes, as before."""

    result = _apply_policy(tmp_path, monkeypatch, _stable_blocker_decision())

    assert result.action == "retry"
    assert result.evidence["decision"] == "retry_repair_missing_forcing"
    assert result.evidence["missing_forcing_repair"]["status"] == "authorized"


def test_confirmed_quarantine_blocker_keeps_the_r2_01_refusal_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2.3: a confirmed candidate is refused with the r2-01 reason, evaluated first."""

    decision = _stable_blocker_decision(
        journal_predecessor_identity=dict(_IDENTITY_BLOCK),
        artifact_guard={"planned_retry_decision": "retry_journal_predecessor_identity_mismatch"},
        operator_reentry_confirmation={"decision": "breaker", "request_id": "req-1"},
    )

    result = _apply_policy(tmp_path, monkeypatch, decision)

    assert result.action == "blocked"
    assert result.evidence["missing_forcing_repair"]["reason"] == "operator_reentry_confirmation_present"
