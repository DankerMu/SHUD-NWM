"""Evidence-size decidability of ``list-operator-actions`` (#1905, #2402).

Two writer tiers and one reader consequence are pinned here:

* #1905 (D1) the NON-BLOCKING SUMMARY tier — an oversized pass whose candidate
  detail is the reason it overflows keeps its TRUE status, summarizes the three
  candidate lists to the bounded summary rows and records
  ``evidence_compaction.mode = non_blocking_summary``.  Such a pass is read
  exactly like the same pass written in full, which is what the equivalence
  table below proves with WRITER-produced rows (``candidate.to_dict()`` and a
  real ``run_once()`` pass), never hand-written dicts.
* #2402 (D2) the bounded fallback keeps a capped projection of the
  breaker-released ``source_cycles`` entries plus the ``limit.source_cycles``
  marker, and the reader lists those models and reports the pass with reason
  ``size_fallback_source_cycles_summarized``.

Every ``services.*`` / ``tests.*`` import is function-local on purpose: the new
file must not join the frozen top-level importer set of
``tests.test_production_scheduler`` (``tests/test_select_ci_tests.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

_PASS_ID = "scheduler_2026052112_aaaaaaaaaaaa"
_BREAKER_SELECTION_REASON = "journal_predecessor_identity_quarantine_breaker_engaged"
_BREAKER_DECISION = "blocked_journal_predecessor_identity_quarantine"


# ---------------------------------------------------------------------------
# Writer-side helpers (no hand-written evidence rows)
# ---------------------------------------------------------------------------


def _write_evidence(root: Path, payload: dict[str, Any], *, max_evidence_bytes: int | None = None) -> Path:
    """Write ``payload`` through the REAL ``write_evidence`` under its own root."""

    from services.orchestrator import scheduler_evidence
    from tests.test_production_scheduler import _config, _dt, _scheduler_evidence_test_context

    root.mkdir(parents=True, exist_ok=True)
    config = _config(root, now=_dt("2026-05-21T12:00:00Z"))
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    context = (
        _scheduler_evidence_test_context(config)
        if max_evidence_bytes is None
        else _scheduler_evidence_test_context(config, max_evidence_bytes=max_evidence_bytes)
    )
    scheduler_evidence.write_evidence(context, str(payload["pass_id"]), payload)
    return evidence_dir


def _summary_tier_limit(payload: dict[str, Any]) -> int:
    """A byte bound the summarized payload fits and the full payload does not.

    Built from the PRE-EXISTING bounded summary helper, so the bound is known
    before the new tier exists (that is what keeps the red run honest).
    """

    from services.orchestrator import scheduler_evidence_payload

    summarized = dict(payload)
    for field_name in ("candidates", "blocked_candidates", "skipped_candidates"):
        if field_name in summarized:
            summarized[field_name] = scheduler_evidence_payload._bounded_candidate_summary_rows(summarized[field_name])
    # Slack for the ``evidence_compaction`` record the tier adds.
    return len(scheduler_evidence_payload._serialize_evidence_json(summarized).encode("utf-8")) + 800


def _candidate(model_id: str = "model_a") -> Any:
    from services.orchestrator import scheduler as scheduler_module
    from tests.test_production_scheduler import _dt
    from workers.data_adapters.base import CycleDiscovery

    return scheduler_module._candidate_for(
        discovery=CycleDiscovery(
            cycle_id="gfs_2026052106",
            source_id="gfs",
            cycle_time=_dt("2026-05-21T06:00:00Z"),
            cycle_hour=6,
            available=True,
            status="discovered",
        ),
        model=scheduler_module.RegisteredSchedulerModel(
            model_id=model_id,
            basin_id=f"basin_{model_id}",
            basin_version_id=f"basin_{model_id}_v1",
            river_network_version_id=f"basin_{model_id}_rivnet_v1",
            segment_count=3,
            output_segment_count=3,
            model_package_uri=f"s3://nhms/models/{model_id}/package/",
            shud_code_version="2.0",
            resource_profile={},
            resource_profile_summary={},
            display_capabilities={},
        ),
        horizon={},
    )


def _blocked_row(model_id: str, reason: str, state_evidence: dict[str, Any]) -> dict[str, Any]:
    """One ``blocked_candidates`` row exactly as the scheduler publishes it."""

    from dataclasses import replace

    return replace(
        _candidate(model_id),
        status="blocked",
        reason=reason,
        state_evidence=state_evidence,
    ).to_dict()


def _writer_blocked_rows() -> dict[str, dict[str, Any]]:
    """One writer-produced row per listed decision class, with 0/False retry values.

    Every ``state_evidence`` here is the producer's own output, not a literal:
    ``scheduler_state_failure`` for the permanent/cancelled legs,
    ``scheduler_candidates`` for the budget, breaker and sink-refusal legs.
    """

    from dataclasses import replace

    from services.orchestrator.scheduler_candidates import (
        _journal_predecessor_identity_blocked_evidence,
        _refuse_confirmed_candidates_off_forecast,
        _strict_warm_start_terminal_blocked_evidence,
    )
    from services.orchestrator.scheduler_state_failure import (
        _cancelled_state_evidence,
        _permanent_failure_evidence,
    )

    permanent_state = {
        "pipeline_status": "failed",
        "error_code": "SLURM_TIMEOUT",
        "failed_stage": "forecast",
        "retry_count": 0,
        "retry_limit": 0,
    }
    permanent_evidence = _permanent_failure_evidence(_candidate("model_a"), permanent_state, {})
    assert permanent_evidence is not None
    cancelled_evidence = _cancelled_state_evidence(_candidate("model_b"), {"pipeline_status": "cancelled"}, {})
    assert cancelled_evidence is not None

    confirmed = replace(
        _candidate("model_e"),
        state_evidence={
            "operator_reentry_confirmation": {"decision": "recorded", "request_id": "req-1"},
            "restart_stage": "convert",
            "restart_from_stage": "convert",
        },
    )
    selected = [confirmed]
    refused: list[Any] = []
    _refuse_confirmed_candidates_off_forecast(selected, refused)
    assert selected == [] and len(refused) == 1

    return {
        "permanent_failure": _blocked_row("model_a", "retry_limit_exhausted", permanent_evidence),
        "cancelled_manual_retry_required": _blocked_row(
            "model_b", "manual_retry_required_after_cancelled", cancelled_evidence
        ),
        "blocked_strict_warm_start_init_state_mismatch": _blocked_row(
            "model_c",
            "strict_warm_start_retry_budget_exhausted",
            _strict_warm_start_terminal_blocked_evidence({}, {"ready": False}, attempt=0, retry_limit=0),
        ),
        _BREAKER_DECISION: _blocked_row(
            "model_d",
            _BREAKER_SELECTION_REASON,
            _journal_predecessor_identity_blocked_evidence(
                {},
                recorded_init_state_id="state_gfs_model_d_2026052106_gfs_2026052018_f012",
                expected_init_state_id="state_gfs_model_d_2026052106_gfs_2026052100_f006",
                required_lead_hours=6,
                skipped_reason="journal_predecessor_identity_quarantined",
                occurrences=0,
            ),
        ),
        "blocked_operator_reentry_restart_stage_refused": refused[0].to_dict(),
    }


def _bulk_unrelated_row() -> dict[str, Any]:
    """A writer row of a NON-listed decision carrying the bulk that overflows the bound."""

    return _blocked_row(
        "model_z",
        "canonical_forcing_not_ready",
        {
            "decision": "blocked_retryable",
            "canonical_readiness": {"missing_leads": [f"lead_{index:04d}" for index in range(2_000)]},
        },
    )


@pytest.fixture(scope="module")
def breaker_pass_evidence(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """A REAL ``run_once()`` pass whose ``source_cycles`` carries a breaker release.

    ``sources`` is widened to the whole production set afterwards: the fixture's
    scheduler is a single-source geometry, and without that the pass would be
    read ``scope_narrowed`` and could never answer ``exit 0``.  Everything the
    tier touches (the candidate lists, the source cycles) stays as the writer
    produced it.
    """

    from tests.test_operator_reentry_confirmation import breaker_scheduler, seed_breaker_journal
    from tests.test_production_scheduler import FakeProductionOrchestrator

    tmp_path = tmp_path_factory.mktemp("breaker_pass").resolve()
    with pytest.MonkeyPatch.context() as monkeypatch:
        root = seed_breaker_journal(tmp_path, monkeypatch)
        result = breaker_scheduler(tmp_path, root, FakeProductionOrchestrator()).run_once()
    evidence = json.loads(json.dumps(result.evidence))
    evidence["pass_id"] = _PASS_ID
    evidence["sources"] = ["gfs", "IFS"]
    released = [
        item
        for item in evidence["source_cycles"]
        if item.get("selection_reason") == _BREAKER_SELECTION_REASON
    ]
    assert len(released) == 1, evidence["source_cycles"]
    return evidence


def _listing(evidence_root: Path) -> tuple[dict[str, Any], int]:
    from services.orchestrator.operator_action_listing import list_operator_actions

    return list_operator_actions(evidence_root=str(evidence_root), passes=6)


# ---------------------------------------------------------------------------
# 1.1 / 1.2 / 1.3 -- the tier itself (#1905)
# ---------------------------------------------------------------------------


def test_non_blocking_summary_keeps_the_true_status_in_result_artifact_and_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate-heavy pass over the bound keeps its computed status, not ``resource_limit_blocked``."""

    from services.orchestrator import cli
    from tests.test_production_scheduler import (
        FakeAdapter,
        FakeRegistry,
        ProductionScheduler,
        ProductionSchedulerConfig,
        SchedulerPassResult,
        _config,
        _dt,
        _model,
    )

    def _pass(root: Path) -> Any:
        root.mkdir(parents=True, exist_ok=True)
        return ProductionScheduler(
            _config(root, now=_dt("2026-05-21T12:00:00Z")),
            registry=FakeRegistry([_model("model_a", "basin_a"), _model("model_b", "basin_b")]),
            adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        ).run_once()

    reference = _pass(tmp_path / "reference")
    full_bytes = len(Path(reference.artifact_path or "").read_bytes())
    max_evidence_bytes = _summary_tier_limit(reference.evidence)
    assert max_evidence_bytes < full_bytes

    monkeypatch.setattr("services.orchestrator.scheduler.MAX_EVIDENCE_BYTES", max_evidence_bytes)
    result = _pass(tmp_path / "bounded")
    serialized = Path(result.artifact_path or "").read_bytes()
    persisted = json.loads(serialized.decode("utf-8"))

    assert len(serialized) <= max_evidence_bytes
    assert result.status == "planned"
    assert result.evidence["status"] == "planned"
    assert persisted["status"] == "planned"
    assert "limit" not in persisted
    assert persisted["evidence_compaction"] == {
        "status": "applied",
        "reason": "evidence_size_limit_exceeded",
        "mode": "non_blocking_summary",
        "max_evidence_bytes": max_evidence_bytes,
        "pre_compaction_status": "planned",
        "summarized_fields": ["candidates", "blocked_candidates", "skipped_candidates"],
    }
    assert all("state_evidence" not in row for row in persisted["candidates"])
    assert [row["candidate_id"] for row in persisted["candidates"]] == [
        row["candidate_id"] for row in reference.evidence["candidates"]
    ]
    # Verbatim, on purpose: the readiness reader validates these rows.
    assert persisted["source_cycles"] == result.evidence["source_cycles"]

    class FakeScheduler:
        def __init__(self, config: ProductionSchedulerConfig) -> None:
            self.config = config

        @classmethod
        def from_env(cls, config: ProductionSchedulerConfig) -> FakeScheduler:
            return cls(config)

        def run_continuous(self, *, max_passes: int | None = None) -> list[SchedulerPassResult]:
            assert max_passes == 1
            return [result]

    monkeypatch.setattr(cli, "ProductionScheduler", FakeScheduler)
    payload = cli._plan_production(
        sources=("gfs",),
        lookback_hours=24,
        cycle_lag_hours=0,
        max_cycles_per_source=1,
        model_ids=(),
        basin_ids=(),
        dry_run=True,
        continuous=True,
        interval_seconds=300.0,
        max_passes=1,
        workspace_root=str(tmp_path),
        lock_path=None,
        evidence_dir=None,
    )

    assert payload["status"] == "planned"
    assert payload["passes"][0]["status"] == "planned"


def test_a_summary_that_still_exceeds_the_bound_falls_back_fail_closed(tmp_path: Path) -> None:
    """The new tier is not a bypass: below its own fit the #1168 fallback still applies."""

    from services.orchestrator import scheduler_evidence
    from services.orchestrator.scheduler_evidence import SchedulerEvidenceWriteError
    from tests.test_production_scheduler import (
        _config,
        _dt,
        _incident_scheduler_evidence_payload,
        _scheduler_evidence_test_context,
    )

    root = tmp_path / "fallback"
    root.mkdir()
    config = _config(root, now=_dt("2026-05-21T12:00:00Z"))
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    evidence = _incident_scheduler_evidence_payload(_PASS_ID)

    artifact_path = scheduler_evidence.write_evidence(
        _scheduler_evidence_test_context(config, max_evidence_bytes=2_600),
        _PASS_ID,
        evidence,
    )
    persisted = json.loads(Path(artifact_path or "").read_text(encoding="utf-8"))

    assert persisted["status"] == "resource_limit_blocked"
    assert persisted["limit"]["reason"] == "evidence_size_limit_exceeded"
    assert persisted["limit"]["pre_limit_status"] == "submission_failed"

    hard = _incident_scheduler_evidence_payload("scheduler_2026052112_bbbbbbbbbbbb")
    with pytest.raises(SchedulerEvidenceWriteError) as error:
        scheduler_evidence.write_evidence(
            _scheduler_evidence_test_context(config, max_evidence_bytes=1_100),
            "scheduler_2026052112_bbbbbbbbbbbb",
            hard,
        )
    assert error.value.reason == "evidence_size_limit_exceeded"


def test_within_limit_evidence_carries_no_compaction_marker(tmp_path: Path) -> None:
    from tests.test_production_scheduler import _incident_scheduler_evidence_payload

    evidence = _incident_scheduler_evidence_payload(_PASS_ID)
    evidence_dir = _write_evidence(tmp_path / "within", evidence)
    persisted = json.loads((evidence_dir / f"{_PASS_ID}.json").read_text(encoding="utf-8"))

    assert "limit" not in persisted
    assert "evidence_compaction" not in persisted
    assert persisted["candidates"][0]["state_evidence"]["missing_forcing_repair"]["status"] == "requested"


def test_the_summary_tier_nests_the_admission_record_it_ran_after(tmp_path: Path) -> None:
    """Both projections on one pass: the admission record is nested, never overwritten."""

    from services.orchestrator import scheduler_evidence
    from tests.test_production_scheduler import (
        _config,
        _dt,
        _incident_scheduler_evidence_payload,
        _scheduler_evidence_test_context,
    )

    evidence = _incident_scheduler_evidence_payload(_PASS_ID)
    evidence["status"] = "submitted"
    evidence["skipped_candidates"] = [
        {**_bulk_unrelated_row(), "status": "skipped", "reason": "terminal_success"},
    ]
    root = tmp_path / "admission"
    root.mkdir()
    config = _config(root, now=_dt("2026-05-21T12:00:00Z"))
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True)
    max_evidence_bytes = _summary_tier_limit(evidence)

    artifact_path = scheduler_evidence.write_evidence(
        _scheduler_evidence_test_context(config, max_evidence_bytes=max_evidence_bytes),
        _PASS_ID,
        evidence,
    )
    persisted = json.loads(Path(artifact_path or "").read_text(encoding="utf-8"))

    assert persisted["status"] == "submitted"
    compaction = persisted["evidence_compaction"]
    assert compaction["mode"] == "non_blocking_summary"
    assert compaction["admission"] == {
        "status": "applied",
        "reason": "pre_write_size_pressure",
        "terminal_skipped_candidates_compacted": 1,
    }


def test_a_summary_tier_submitted_pass_still_passes_the_readiness_reader(tmp_path: Path) -> None:
    """#1905 1.4b: ``model_run_evidence`` stays verbatim, so readiness reads the pass unchanged."""

    from services.production_closure import readiness_scheduler_evidence
    from tests.test_production_readiness_validation import _live_scheduler_payload

    payload = _live_scheduler_payload()
    payload["pass_id"] = _PASS_ID
    payload["schema_version"] = payload.pop("schema")
    payload["candidates"][0]["state_evidence"] = {
        "decision": "select",
        "canonical_readiness": {"missing_leads": [f"lead_{index:04d}" for index in range(2_000)]},
    }
    full_errors = readiness_scheduler_evidence._scheduler_evidence_errors(payload)

    evidence_dir = _write_evidence(
        tmp_path / "readiness",
        json.loads(json.dumps(payload)),
        max_evidence_bytes=_summary_tier_limit(payload),
    )
    persisted = json.loads((evidence_dir / f"{_PASS_ID}.json").read_text(encoding="utf-8"))

    assert persisted["evidence_compaction"]["mode"] == "non_blocking_summary"
    assert persisted["model_run_evidence"] == payload["model_run_evidence"]
    assert readiness_scheduler_evidence._scheduler_evidence_errors(persisted) == full_errors
    assert readiness_scheduler_evidence._scheduler_readiness_status(persisted, errors=full_errors) == "passed"


# ---------------------------------------------------------------------------
# 1.4 -- the equivalence table (the coupling)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("leg", "expected_exit"),
    [
        ("permanent_failure", 1),
        ("cancelled_manual_retry_required", 1),
        ("blocked_strict_warm_start_init_state_mismatch", 1),
        (_BREAKER_DECISION, 1),
        ("blocked_operator_reentry_restart_stage_refused", 1),
        ("breaker_released_source_cycle", 1),
        ("no_action", 0),
    ],
)
def test_a_summary_tier_pass_lists_exactly_what_the_full_pass_lists(
    tmp_path: Path,
    breaker_pass_evidence: dict[str, Any],
    leg: str,
    expected_exit: int,
) -> None:
    payload = json.loads(json.dumps(breaker_pass_evidence))
    rows = [_bulk_unrelated_row()]
    if leg not in {"breaker_released_source_cycle", "no_action"}:
        rows.insert(0, _writer_blocked_rows()[leg])
    payload["blocked_candidates"] = rows
    payload["counts"]["blocked_candidate_count"] = len(rows)
    if leg != "breaker_released_source_cycle":
        payload["source_cycles"] = [
            item
            for item in payload["source_cycles"]
            if item.get("selection_reason") != _BREAKER_SELECTION_REASON
        ]

    full_root = _write_evidence(tmp_path / "full", json.loads(json.dumps(payload)))
    max_evidence_bytes = _summary_tier_limit(payload)
    tier_root = _write_evidence(
        tmp_path / "tier",
        json.loads(json.dumps(payload)),
        max_evidence_bytes=max_evidence_bytes,
    )
    tier_payload = json.loads((tier_root / f"{_PASS_ID}.json").read_text(encoding="utf-8"))
    assert tier_payload["evidence_compaction"]["mode"] == "non_blocking_summary"
    assert "limit" not in tier_payload

    full_receipt, full_exit = _listing(full_root)
    tier_receipt, tier_exit = _listing(tier_root)

    assert (full_exit, tier_exit) == (expected_exit, expected_exit)
    assert full_receipt["operator_actions"] == tier_receipt["operator_actions"]
    assert full_receipt["non_evaluating_passes"] == tier_receipt["non_evaluating_passes"] == []
    if expected_exit == 1:
        (action,) = full_receipt["operator_actions"]
        expected_decision = _BREAKER_DECISION if leg == "breaker_released_source_cycle" else leg
        assert action["decision"] == expected_decision


# ---------------------------------------------------------------------------
# 2.x -- the bounded source-cycle projection and its marker (#2402)
# ---------------------------------------------------------------------------


def _breaker_released_cycle(index: int) -> dict[str, Any]:
    from services.orchestrator.scheduler_discovery import _source_cycle_evidence
    from tests.test_production_scheduler import _dt
    from workers.data_adapters.base import CycleDiscovery

    item = _source_cycle_evidence(
        CycleDiscovery(
            cycle_id=f"gfs_20260521{index:02d}",
            source_id="gfs",
            cycle_time=_dt("2026-05-21T00:00:00Z"),
            cycle_hour=0,
            available=True,
            status="discovered",
        ),
        horizon={},
    )
    item["selection_status"] = "not_selected"
    item["selection_reason"] = _BREAKER_SELECTION_REASON
    item["journal_predecessor_identity_quarantine"] = {
        "models": [
            {
                "model_id": f"model_{index:03d}",
                "recorded_init_state_id": f"state_gfs_model_{index:03d}_2026052100",
                "expected_init_state_id": f"state_gfs_model_{index:03d}_2026052018",
                "occurrences": 1,
            }
        ],
        "occurrence_threshold": 1,
    }
    return item


def test_the_bounded_fallback_keeps_a_capped_marked_breaker_released_projection() -> None:
    from services.orchestrator import scheduler_evidence
    from services.orchestrator.scheduler_evidence import _BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT
    from tests.test_production_scheduler import _incident_scheduler_evidence_payload

    released_count = _BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT + 6
    payload = _incident_scheduler_evidence_payload(_PASS_ID)
    payload["source_cycles"] = [
        {"source_id": "gfs", "cycle_time_utc": "2026-05-21T06:00:00Z", "selection_status": "selected"},
        *[_breaker_released_cycle(index) for index in range(released_count)],
        {
            "type": "backfill_audit",
            "source_id": "gfs",
            "discovered_count": released_count,
            "breaker_released_gap_count": released_count,
        },
    ]

    bounded = scheduler_evidence.bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=60_000,
    )

    assert bounded["limit"]["source_cycles"] == {
        "status": "summarized",
        "breaker_released_total": released_count,
        "retained": _BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT,
    }
    assert len(bounded["source_cycles"]) == _BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT
    assert all(row["selection_reason"] == _BREAKER_SELECTION_REASON for row in bounded["source_cycles"])
    assert bounded["source_cycles"][0] == {
        "source_id": "gfs",
        "cycle_time_utc": "2026-05-21T00:00:00Z",
        "selection_status": "not_selected",
        "selection_reason": _BREAKER_SELECTION_REASON,
        "journal_predecessor_identity_quarantine": {
            "models": [
                {
                    "model_id": "model_000",
                    "recorded_init_state_id": "state_gfs_model_000_2026052100",
                    "occurrences": 1,
                }
            ]
        },
    }
    assert len(json.dumps(bounded, separators=(",", ":"), sort_keys=True).encode("utf-8")) <= 60_000


def test_a_fit_tier_that_clears_a_non_empty_source_cycle_list_marks_it_dropped() -> None:
    from services.orchestrator import scheduler_evidence
    from tests.test_production_scheduler import _incident_scheduler_evidence_payload

    payload = _incident_scheduler_evidence_payload(_PASS_ID)
    payload["source_cycles"] = [_breaker_released_cycle(index) for index in range(8)]

    dropped = scheduler_evidence.bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=2_600,
    )

    assert dropped["limit"]["source_cycles"]["status"] == "dropped"
    assert dropped.get("source_cycles", []) == []

    # Never downgraded: summarizing an already-dropped product keeps ``dropped``.
    again = scheduler_evidence.bounded_evidence_payload(
        dropped,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=60_000,
    )
    assert again["limit"]["source_cycles"]["status"] == "dropped"


def test_the_terminal_limit_floor_may_drop_the_source_cycle_marker() -> None:
    from services.orchestrator import scheduler_evidence_payload
    from tests.test_production_scheduler import _incident_scheduler_evidence_payload

    payload = _incident_scheduler_evidence_payload(_PASS_ID)
    payload["source_cycles"] = [_breaker_released_cycle(index) for index in range(4)]
    bounded = scheduler_evidence_payload.bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=60_000,
    )
    assert bounded["limit"]["source_cycles"]["status"] == "summarized"

    floored = scheduler_evidence_payload._fit_bounded_evidence_payload(bounded, max_evidence_bytes=600)

    assert floored["limit"] == {"reason": "evidence_size_limit_exceeded"}


def test_a_summarized_size_fallback_lists_its_breaker_released_models(
    tmp_path: Path,
    breaker_pass_evidence: dict[str, Any],
) -> None:
    """#2402 2.2b/2.3: a WRITER-produced projection, read back through the listing."""

    from services.orchestrator import scheduler_evidence

    payload = json.loads(json.dumps(breaker_pass_evidence))
    full_root = _write_evidence(tmp_path / "full", json.loads(json.dumps(payload)))
    full_receipt, full_exit = _listing(full_root)

    bounded = scheduler_evidence.bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=60_000,
    )
    assert bounded["limit"]["source_cycles"]["status"] == "summarized"
    root = tmp_path / "bounded" / "evidence"
    root.mkdir(parents=True)
    (root / f"{_PASS_ID}.json").write_text(json.dumps(bounded), encoding="utf-8")

    receipt, exit_code = _listing(root)

    assert (full_exit, exit_code) == (1, 1)
    assert receipt["operator_actions"] == full_receipt["operator_actions"]
    (action,) = receipt["operator_actions"]
    assert action["decision"] == _BREAKER_DECISION
    assert action["model_id"] == "model_a"
    assert action["recorded_init_state_id"] == "state_gfs_model_a_2026052100_gfs_2026052012_f012"
    assert receipt["non_evaluating_passes"] == [
        {"pass": f"{_PASS_ID}.json", "status": "planned", "reason": "size_fallback_source_cycles_summarized"}
    ]


def test_a_summarized_size_fallback_without_an_action_is_undecidable(tmp_path: Path) -> None:
    from services.orchestrator import scheduler_evidence
    from tests.test_production_scheduler import _incident_scheduler_evidence_payload

    payload = _incident_scheduler_evidence_payload(_PASS_ID)
    payload["blocked_candidates"] = []
    payload["source_cycles"] = []
    bounded = scheduler_evidence.bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=60_000,
    )
    root = tmp_path / "evidence"
    root.mkdir(parents=True)
    (root / f"{_PASS_ID}.json").write_text(json.dumps(bounded), encoding="utf-8")

    receipt, exit_code = _listing(root)

    assert exit_code == 3
    assert receipt["operator_actions"] == []
    assert [item["reason"] for item in receipt["non_evaluating_passes"]] == [
        "size_fallback_source_cycles_summarized"
    ]


@pytest.mark.parametrize("marker", ["dropped", "absent"])
def test_a_dropped_or_absent_source_cycle_marker_reads_as_dropped(tmp_path: Path, marker: str) -> None:
    """Legacy files (no marker) fail closed on the same reason a dropped marker does."""

    from services.orchestrator import scheduler_evidence
    from tests.test_production_scheduler import _incident_scheduler_evidence_payload

    payload = _incident_scheduler_evidence_payload(_PASS_ID)
    payload["blocked_candidates"] = []
    payload["source_cycles"] = [_breaker_released_cycle(index) for index in range(8)]
    bounded = scheduler_evidence.bounded_evidence_payload(
        payload,
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=2_600,
    )
    assert bounded["limit"]["source_cycles"]["status"] == "dropped"
    if marker == "absent":
        bounded["limit"] = {key: value for key, value in bounded["limit"].items() if key != "source_cycles"}
    root = tmp_path / "evidence"
    root.mkdir(parents=True)
    (root / f"{_PASS_ID}.json").write_text(json.dumps(bounded), encoding="utf-8")

    receipt, exit_code = _listing(root)

    assert exit_code == 3
    assert [item["reason"] for item in receipt["non_evaluating_passes"]] == ["size_fallback_source_cycles_absent"]
