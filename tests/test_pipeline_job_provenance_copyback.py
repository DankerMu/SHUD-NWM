from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_orchestration_chain import (
    FakeCycleRepository,
    FakeCycleSlurmClient,
    _basins,
    _orchestrator,
    _precip_context,
)


def test_copyback_still_copies_run_tree_when_provenance_publisher_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from services.orchestrator import chain_forecast_execution
    from services.orchestrator.pipeline_job_provenance import PipelineJobProvenanceError

    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    copyback_root = tmp_path / "shared-object-store"
    copyback_root.mkdir(parents=True)
    monkeypatch.setenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", str(copyback_root))
    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(tmp_path / "journal"))
    (tmp_path / "journal").mkdir()
    copyback_calls: list[dict[str, Any]] = []

    def fake_copyback_run_trees(**kwargs: Any) -> dict[str, Any]:
        copyback_calls.append(dict(kwargs))
        return {
            "status": "copied",
            "root": str(copyback_root),
            "run_ids": list(kwargs["run_ids"]),
            "file_count": 1,
            "byte_count": 4,
            "runs": [{"run_id": run_id, "object_key": f"runs/{run_id}"} for run_id in kwargs["run_ids"]],
        }

    def boom_publisher(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise PipelineJobProvenanceError("JOURNAL_READ_FAILED", "injected publisher failure")

    monkeypatch.setattr(chain_forecast_execution, "copyback_run_trees", fake_copyback_run_trees)
    monkeypatch.setattr(chain_forecast_execution, "publish_runs_pipeline_job_provenance", boom_publisher)
    context = _precip_context(cycle_id="gfs_2026050100", active_basins=_basins(1))

    chain_forecast_execution._copyback_stage_run_trees(orchestrator, context, stage="parse")

    assert copyback_calls == [
        {
            "object_store_root": orchestrator.config.object_store_root,
            "copyback_root": str(copyback_root),
            "run_ids": ["run_0"],
            "extra_object_keys": (),
            "object_store_prefix": orchestrator.config.object_store_prefix,
        }
    ]
    copyback_event = next(event for event in repository.events if event["event_type"] == "object_store_copyback")
    provenance_event = next(event for event in repository.events if event["event_type"] == "pipeline_job_provenance")
    assert copyback_event["status_to"] == "copied"
    assert provenance_event["status_to"] == "failed"
    assert provenance_event["details"]["runs"][0]["reason"] == "JOURNAL_READ_FAILED"
    assert repository.cycle_statuses == []


def test_copyback_still_copies_run_tree_when_provenance_publisher_raises_generic_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from services.orchestrator import chain_forecast_execution

    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    copyback_root = tmp_path / "shared-object-store"
    copyback_root.mkdir(parents=True)
    monkeypatch.setenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", str(copyback_root))
    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(tmp_path / "journal"))
    (tmp_path / "journal").mkdir()
    copyback_calls: list[list[str]] = []

    def fake_copyback_run_trees(**kwargs: Any) -> dict[str, Any]:
        copyback_calls.append(list(kwargs["run_ids"]))
        return {"status": "copied", "run_ids": list(kwargs["run_ids"])}

    def boom_publisher(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise RuntimeError("injected generic publisher failure")

    monkeypatch.setattr(chain_forecast_execution, "copyback_run_trees", fake_copyback_run_trees)
    monkeypatch.setattr(chain_forecast_execution, "publish_runs_pipeline_job_provenance", boom_publisher)
    context = _precip_context(cycle_id="gfs_2026050100", active_basins=_basins(1))

    chain_forecast_execution._copyback_stage_run_trees(orchestrator, context, stage="parse")

    assert copyback_calls == [["run_0"]]
    provenance_event = next(event for event in repository.events if event["event_type"] == "pipeline_job_provenance")
    copyback_event = next(event for event in repository.events if event["event_type"] == "object_store_copyback")
    assert copyback_event["status_to"] == "copied"
    assert provenance_event["status_to"] == "failed"
    assert provenance_event["details"]["runs"][0]["reason"] == "RuntimeError"


def test_copyback_copies_actual_run_artifact_when_provenance_batch_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from services.orchestrator import chain_forecast_execution
    from services.orchestrator.pipeline_job_provenance import PipelineJobProvenanceError
    from tests.test_run_tree_copyback import _write_run

    repository = FakeCycleRepository()
    orchestrator = _orchestrator(tmp_path, repository, FakeCycleSlurmClient())
    run_id = "fcst_gfs_2026062700_basins_heihe_shud"
    object_root = Path(orchestrator.config.object_store_root)
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, run_id, output_text="copied-after-provenance-failure\n")
    monkeypatch.setenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", str(copyback_root))
    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(tmp_path / "journal"))
    (tmp_path / "journal").mkdir()

    def boom_publisher(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise PipelineJobProvenanceError("JOURNAL_READ_FAILED", "injected publisher failure")

    monkeypatch.setattr(chain_forecast_execution, "publish_runs_pipeline_job_provenance", boom_publisher)
    context = _precip_context(
        source_id="gfs",
        cycle_id="gfs_2026062700",
        active_basins=[{"run_id": run_id}],
    )

    chain_forecast_execution._copyback_stage_run_trees(orchestrator, context, stage="parse")

    copied_output = copyback_root / "runs" / run_id / "output" / "q.rivqdown.csv"
    assert copied_output.read_text(encoding="utf-8") == "copied-after-provenance-failure\n"
    provenance_event = next(event for event in repository.events if event["event_type"] == "pipeline_job_provenance")
    assert provenance_event["status_to"] == "failed"
    assert provenance_event["details"]["runs"][0]["reason"] == "JOURNAL_READ_FAILED"
