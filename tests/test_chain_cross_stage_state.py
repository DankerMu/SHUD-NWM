"""Cross-stage state residue in the forecast chain (batch C: #2416, #2394, #2393, #1845).

Governing invariant (``openspec/changes/chain-cross-stage-state-residue``):
every stage decision in one ``orchestrate_cycle`` call is derived from that
stage's own rows, that model's own identity, and the manifest's top-level
fields -- never from another stage's reservation, another model's row, or a
marker the manifest deliberately stripped.

Seams: ``ForecastOrchestrator.orchestrate_cycle`` (public), the scheduler's
``_candidate_basin_manifest`` -> chain ``_restart_stage_from_basins`` hop, and
``_candidate_scoped_cycle_execution`` (the restart stage's second consumer).
Only Slurm is faked.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import chain_runtime_utils
from services.orchestrator import scheduler_candidate_manifest as manifest_module
from tests.test_orchestration_chain import (
    FakeCycleRepository,
    FakeCycleSlurmClient,
    _basins,
    _orchestrator,
)

_OUTPUT_URI = "s3://nhms/runs/out.nc"
_FRESH_FULL_CHAIN = {"required": True, "mode": "full_chain"}


def _manifest(state_evidence: dict[str, Any], **extra: Any) -> dict[str, Any]:
    # Function-local on purpose: a module-scope import would grow the frozen
    # direct-importer anchor of test_production_scheduler.py (test_select_ci_tests).
    from tests.test_production_scheduler import _scheduler_candidate_fixture

    candidate = replace(_scheduler_candidate_fixture(), state_evidence=state_evidence)
    return manifest_module._candidate_basin_manifest(candidate, output_uri=_OUTPUT_URI, **extra)


def _fresh_full_chain_manifest_with_residual_marker() -> dict[str, Any]:
    return _manifest({"restart_stage": "forecast", "fresh_ingestion": dict(_FRESH_FULL_CHAIN)})


# --- #2416: the manifest's top-level field is the single restart source ------


def test_fresh_full_chain_manifest_residual_marker_does_not_reach_the_chain() -> None:
    """#2416 table A reversed: the stripped marker is not read back from evidence."""

    manifest = _fresh_full_chain_manifest_with_residual_marker()

    assert "restart_stage" not in manifest
    # The evidence still travels verbatim (audit); only its restart marker is ignored.
    assert manifest["state_evidence"]["restart_stage"] == "forecast"
    assert chain_runtime_utils._restart_stage_from_basins([manifest]) is None


def test_full_cohort_member_with_residual_marker_does_not_skip_convert_or_forcing(tmp_path: Path) -> None:
    """#2416 table B reversed: a ``(0,"full")`` cohort starts at convert for every member."""

    fresh_manifest = _fresh_full_chain_manifest_with_residual_marker()
    basins = _basins(3)
    # Member 0 carries exactly what the manifest builder hands the chain for a
    # fresh full-chain candidate: evidence with the residual marker and no
    # top-level ``restart_stage``. Members 1 and 2 are markerless.
    basins[0]["state_evidence"] = fresh_manifest["state_evidence"]
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, FakeCycleRepository(), client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", basins)

    assert result.status == "complete"
    assert client.submissions[0]["stage"] == "convert"
    submitted_stages = [submission["stage"] for submission in client.submissions]
    assert submitted_stages[:3] == ["convert", "forcing", "forecast"]


@pytest.mark.parametrize("evidence_key", ["restart_stage", "restart_from_stage"])
def test_ordinary_restart_candidate_keeps_its_restart_through_the_manifest(
    tmp_path: Path, evidence_key: str
) -> None:
    """Must-preserve (3.3): a non-fresh candidate's restart reaches the chain via the top-level key."""

    manifest = _manifest({"decision": "retry_failed", evidence_key: "forecast"})

    assert manifest["restart_stage"] == "forecast"
    assert chain_runtime_utils._restart_stage_from_basins([manifest]) == "forecast"

    basin = _basins(1)[0]
    basin["restart_stage"] = manifest["restart_stage"]
    basin["state_evidence"] = manifest["state_evidence"]
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, FakeCycleRepository(), client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert result.status == "complete"
    submitted_stages = [submission["stage"] for submission in client.submissions]
    assert submitted_stages[0] == "forecast"
    assert "convert" not in submitted_stages
    assert "forcing" not in submitted_stages


def test_manifest_restart_stage_keeps_raw_value_and_chain_canonicalizes_it() -> None:
    """The manifest writes the raw evidence value; the chain canonicalizes on read."""

    manifest = _manifest({"decision": "retry_failed", "restart_from_stage": "parse_output"})

    assert manifest["restart_stage"] == "parse_output"
    assert chain_runtime_utils._restart_stage_from_basins([manifest]) == "parse"


def test_raw_repair_downstream_emitter_marker_stays_off_the_manifest_top_level() -> None:
    """Census pin (3.4): ``retry_downstream_after_raw_repair`` writes only
    ``restart_from_stage: "download"`` (a retired stage) and is NOT fresh full-chain.

    Its top-level ``restart_stage`` must stay absent: raw readers of that key
    (``chain_array_accounting`` forecast projections, ``reconcile``) would
    otherwise project ``"download"``, which the accepted-submit evidence
    validator rejects (``ACCEPTED_RESTART_STAGES``). The chain starts at stage 0
    for it either way.
    """

    manifest = _manifest(
        {
            "decision": "retry_failed",
            "reason": "retry_downstream_after_raw_repair",
            "restart_stage": None,
            "restart_from_stage": "download",
            "fresh_ingestion": {"required": False, "mode": "reuse_repaired_raw_then_full_chain"},
        }
    )

    assert "restart_stage" not in manifest
    assert chain_runtime_utils._restart_stage_from_basins([manifest]) is None


def test_restart_stage_from_basins_takes_earliest_marker_and_ignores_markerless_members() -> None:
    """3.6 ``min`` semantics: earliest claim among marker-carrying members."""

    basins = [{"restart_stage": "parse"}, {}, {"restart_stage": "forecast"}]

    assert chain_runtime_utils._restart_stage_from_basins(basins) == "forecast"


# --- #2416 3.5: the second consumer, _candidate_scoped_cycle_execution --------


def test_candidate_scope_of_single_fresh_full_chain_basin_without_run_id_no_longer_narrows() -> None:
    """3.5(a) FLIPS True -> False: the stripped marker no longer narrows scope.

    Accepted: without the marker the basin is exactly a markerless fresh
    candidate, which was never candidate-scoped. Production basins always carry
    ``orchestration_run_id`` (3.5(c)), so production scope is unchanged.
    """

    manifest = _fresh_full_chain_manifest_with_residual_marker()
    assert "orchestration_run_id" not in manifest

    assert chain_runtime_utils._candidate_scoped_cycle_execution([manifest]) is False


def test_candidate_scope_of_single_ordinary_restart_basin_is_unchanged() -> None:
    """3.5(b) does not flip: the top-level key keeps the single basin scoped."""

    manifest = _manifest({"decision": "retry_failed", "restart_from_stage": "forecast"})
    assert "orchestration_run_id" not in manifest

    assert chain_runtime_utils._candidate_scoped_cycle_execution([manifest]) is True


def test_candidate_scope_with_orchestration_run_id_is_unchanged() -> None:
    """3.5(c) does not flip: a run-id-stamped basin is scoped regardless of restart."""

    fresh = _fresh_full_chain_manifest_with_residual_marker()
    fresh["orchestration_run_id"] = "cycle_gfs_2026052106_full_cohort_abc"
    plain = _manifest({})
    plain["orchestration_run_id"] = "cycle_gfs_2026052106_full_cohort_abc"

    assert chain_runtime_utils._candidate_scoped_cycle_execution([fresh]) is True
    assert chain_runtime_utils._candidate_scoped_cycle_execution([fresh, plain]) is True
