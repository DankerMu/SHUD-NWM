"""The owner-side facade seam: ``scheduler_state_common._facade_seam``.

The subject is the owner.  The owner-path cases call the owner
``scheduler_state_decision._candidate_state_decision`` directly: a patch on
``services.orchestrator.scheduler`` is only observed because the owner reads the
facade name at call time.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_state as scheduler_state_module
from services.orchestrator import scheduler_state_decision as scheduler_state_decision_module
from services.orchestrator import scheduler_state_failure as scheduler_state_failure_module
from services.orchestrator.scheduler_state_common import _facade_seam
from workers.data_adapters.base import CycleDiscovery

_FACADE_MODULE = "services.orchestrator.scheduler"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _candidate() -> scheduler_module.SchedulerCandidate:
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
            model_id="model_a",
            basin_id="basin_a",
            basin_version_id="basin_a_v1",
            river_network_version_id="basin_a_rivnet_v1",
            segment_count=3,
            output_segment_count=3,
            model_package_uri="s3://nhms/models/model_a/package/",
            shud_code_version="2.0",
            resource_profile={},
            resource_profile_summary={},
            display_capabilities={},
        ),
        horizon={},
    )


def _raw_manifest_repair_state(candidate: Any, manifest_uri: str) -> dict[str, Any]:
    # A succeeded download plus a convert that exhausted its retries on
    # INVALID_MANIFEST: the raw-manifest repair leg probes the manifest URI.
    return {
        "candidate_id": candidate.candidate_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "forecast_cycle": {
            "cycle_id": candidate.cycle_id,
            "source_id": candidate.source_id,
            "cycle_time": candidate.cycle_time_utc,
            "manifest_uri": manifest_uri,
        },
        "pipeline_jobs": [
            {
                "job_id": "job_cycle_gfs_2026052106_download",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": candidate.cycle_id,
                "status": "succeeded",
                "stage": "download",
                "job_type": "download_source_cycle",
                "updated_at": "2026-05-21T06:02:00Z",
            },
            {
                "job_id": "job_cycle_gfs_2026052106_convert",
                "run_id": "cycle_gfs_2026052106",
                "cycle_id": candidate.cycle_id,
                "status": "failed",
                "stage": "convert",
                "job_type": "convert_canonical",
                "error_code": "INVALID_MANIFEST",
                "retry_count": 3,
                "updated_at": "2026-05-21T06:03:00Z",
            },
        ],
        "pipeline_status": "failed",
        "failed_stage": "convert",
        "error_code": "INVALID_MANIFEST",
        "retry_count": 3,
        "retry_limit": 3,
    }


def test_owner_candidate_state_decision_reads_facade_active_jobs_patch(monkeypatch: Any) -> None:
    candidate = _candidate()
    patched_job = {
        "job_id": "job_patched_facade",
        "slurm_job_id": "patched-facade",
        "status": "running",
        "source": "scheduler facade monkeypatch",
    }

    def patched_active_jobs(_state: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [patched_job]

    monkeypatch.setattr(scheduler_module, "_state_active_jobs", patched_active_jobs)

    decision = scheduler_state_decision_module._candidate_state_decision(
        candidate,
        {
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "forcing_version_id": candidate.forcing_version_id,
            "pipeline_jobs": [],
        },
    )

    assert decision is not None
    assert decision.action == "skip"
    assert decision.reason == "active_slurm_job"
    assert decision.evidence["active_slurm_jobs"] == [patched_job]


def test_owner_candidate_state_decision_reads_facade_manifest_missing_patch(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    candidate = _candidate()
    manifest_uri = "s3://nhms/raw/gfs/2026052106/manifest.json"
    calls: list[tuple[str, str]] = []

    def patched_manifest_missing(patched_candidate: Any, patched_manifest_uri: str) -> bool:
        calls.append((patched_candidate.candidate_id, patched_manifest_uri))
        return True

    # Without a configured root the probe short-circuits fail-closed before the
    # seam is consulted; a real (empty) root puts the seam on the path.
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setattr(scheduler_module, "_object_manifest_is_missing", patched_manifest_missing)

    decision = scheduler_state_decision_module._candidate_state_decision(
        candidate,
        _raw_manifest_repair_state(candidate, manifest_uri),
    )

    assert decision is not None
    assert decision.action == "retry"
    assert decision.reason == "repair_missing_raw_manifest"
    assert decision.evidence["raw_manifest_repair"]["manifest_exists"] is False
    assert calls == [(candidate.candidate_id, manifest_uri)]


def test_facade_seam_returns_fallback_when_facade_not_loaded(monkeypatch: Any) -> None:
    def fallback(_state: Mapping[str, Any]) -> list[dict[str, Any]]:
        return []

    monkeypatch.delitem(sys.modules, _FACADE_MODULE, raising=False)

    assert _FACADE_MODULE not in sys.modules
    assert _facade_seam("_state_active_jobs", fallback) is fallback


def test_facade_seam_returns_fallback_for_non_callable_facade_attribute(monkeypatch: Any) -> None:
    def fallback(_state: Mapping[str, Any]) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(scheduler_module, "_state_active_jobs", "not callable")

    assert _facade_seam("_state_active_jobs", fallback) is fallback


def test_facade_seam_returns_fallback_for_name_absent_from_facade() -> None:
    def fallback() -> None:
        return None

    assert not hasattr(scheduler_module, "_facade_seam_no_such_name")
    assert _facade_seam("_facade_seam_no_such_name", fallback) is fallback


def test_facade_seam_returns_callable_facade_patch_as_is(monkeypatch: Any) -> None:
    def patched(_state: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"job_id": "patched"}]

    monkeypatch.setattr(scheduler_module, "_state_active_jobs", patched)

    assert _facade_seam("_state_active_jobs", scheduler_state_decision_module._state_active_jobs) is patched


def test_facade_seam_treats_wraps_spy_as_patch(monkeypatch: Any) -> None:
    owner_binding = scheduler_state_module._state_active_jobs
    calls: list[Mapping[str, Any]] = []

    @functools.wraps(owner_binding)
    def spy(state: Mapping[str, Any]) -> list[dict[str, Any]]:
        calls.append(state)
        return owner_binding(state)

    def fallback(_state: Mapping[str, Any]) -> list[dict[str, Any]]:
        return []

    # Unpatched, the facade binding is the owner function itself.
    assert scheduler_module._state_active_jobs is owner_binding
    monkeypatch.setattr(scheduler_module, "_state_active_jobs", spy)

    # A ``functools.wraps`` spy carries ``__wrapped__ is owner_binding`` but is not
    # the facade's own re-export, so the seam returns it and the owner calls it.
    assert spy.__wrapped__ is owner_binding
    assert _facade_seam("_state_active_jobs", fallback) is spy
    candidate = _candidate()
    state = {
        "candidate_id": candidate.candidate_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "pipeline_jobs": [],
    }
    scheduler_state_decision_module._candidate_state_decision(candidate, state)
    assert len(calls) == 1


def test_facade_seam_treats_direct_reexport_as_unpatched(monkeypatch: Any) -> None:
    owner_binding = scheduler_state_module._state_active_jobs

    def fallback(_state: Mapping[str, Any]) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(scheduler_module, "_state_active_jobs", owner_binding)

    assert _facade_seam("_state_active_jobs", fallback) is fallback


def test_facade_seam_owner_module_patch_wins_while_facade_unpatched(monkeypatch: Any) -> None:
    def owner_patch(_candidate: Any, _manifest_uri: str) -> bool:
        return True

    monkeypatch.setattr(scheduler_state_failure_module, "_object_manifest_is_missing", owner_patch)

    facade_binding = scheduler_module._object_manifest_is_missing
    assert facade_binding is scheduler_state_module._object_manifest_is_missing
    assert (
        _facade_seam("_object_manifest_is_missing", scheduler_state_failure_module._object_manifest_is_missing)
        is owner_patch
    )


def test_facade_seam_facade_patch_wins_when_both_patched(monkeypatch: Any) -> None:
    def owner_patch(_candidate: Any, _manifest_uri: str) -> bool:
        return False

    def facade_patch(_candidate: Any, _manifest_uri: str) -> bool:
        return True

    monkeypatch.setattr(scheduler_state_failure_module, "_object_manifest_is_missing", owner_patch)
    monkeypatch.setattr(scheduler_module, "_object_manifest_is_missing", facade_patch)

    assert (
        _facade_seam("_object_manifest_is_missing", scheduler_state_failure_module._object_manifest_is_missing)
        is facade_patch
    )
