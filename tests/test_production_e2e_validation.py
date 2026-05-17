from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.production_closure import slurm_validation
from services.production_closure.e2e_validation import (
    ProductionE2EConfig,
    ProductionE2EValidationError,
    _argparse_main,
    validate_e2e,
)


def test_validate_e2e_default_lane_writes_required_ready_evidence(tmp_path: Path) -> None:
    summary = validate_e2e(ProductionE2EConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m10_150"))

    lane_dir = tmp_path / "artifacts" / "m10_150" / "e2e"
    assert summary["status"] == "ready"
    assert summary["stage_statuses"] == {
        "download": "ready",
        "canonical": "ready",
        "forcing": "ready",
        "slurm": "ready",
        "parse": "ready",
        "frequency": "ready",
        "tile": "ready",
        "api": "ready",
        "frontend": "ready",
    }
    for name in [
        "preflight.json",
        "dependency_status.json",
        "stage_manifest.json",
        "api_contract_evidence.json",
        "frontend_smoke_evidence.json",
        "shud_output_qc.json",
        "environment.json",
        "summary.json",
    ]:
        assert (lane_dir / name).is_file()

    preflight = _read_json(lane_dir / "preflight.json")
    assert preflight["source_cycle"] == "2026-05-07T00:00:00Z"
    assert preflight["model_set"] == ["basins_qhh_shud_fixture"]
    assert preflight["execution_policy"]["real_slurm_required"] is False

    dependency_status = _read_json(lane_dir / "dependency_status.json")
    assert {item["status"] for item in dependency_status["dependencies"]} == {"skipped"}
    assert all(item["live_success_claimed"] is False for item in dependency_status["dependencies"])

    api = _read_json(lane_dir / "api_contract_evidence.json")
    assert api["status"] == "ready"
    assert api["live_api_executed"] is False
    assert api["run_id_specific_api_filters_added"] is False
    assert {query["contract"] for query in api["contract_queries"]} == {
        "model_detail",
        "forecast_series",
        "flood_alerts_summary",
        "flood_alerts_ranking",
        "flood_alerts_timeline",
        "jobs",
        "job_logs",
        "tile_metadata",
    }
    paths = {query["contract"]: query["path"] for query in api["contract_queries"]}
    assert paths["model_detail"] == "/api/v1/models/basins_qhh_shud_fixture"
    assert (
        paths["forecast_series"]
        == "/api/v1/basin-versions/basins_qhh_shud_fixture_basin_v1/river-segments/seg_a/forecast-series"
    )
    assert paths["flood_alerts_summary"] == "/api/v1/flood-alerts/summary"
    assert paths["flood_alerts_ranking"] == "/api/v1/flood-alerts/ranking"
    assert paths["flood_alerts_timeline"] == "/api/v1/flood-alerts/timeline"
    assert paths["jobs"] == "/api/v1/jobs"
    assert paths["job_logs"] == "/api/v1/jobs/m10_150-array-0/logs"
    assert paths["tile_metadata"] == "/api/v1/tiles/flood-return-period"
    queries = {query["contract"]: query.get("query", {}) for query in api["contract_queries"]}
    assert queries["forecast_series"] == {
        "issue_time": "2026-05-07T00:00:00Z",
        "variables": "q_down",
        "scenarios": "GFS",
        "include_analysis": "false",
    }
    assert "source" not in queries["forecast_series"]
    assert "cycle_time" not in queries["forecast_series"]
    assert queries["flood_alerts_ranking"] == {
        "run_id": "m10_150",
        "limit": 10,
        "offset": 0,
        "valid_time": "2026-05-07T03:00:00Z",
    }
    assert "segment_id" not in queries["flood_alerts_ranking"]

    stage_manifest = _read_json(lane_dir / "stage_manifest.json")
    for stage in stage_manifest["stages"]:
        assert stage["outputs"], stage["stage"]
        for output in stage["outputs"]:
            if output.startswith(str(lane_dir)):
                assert Path(output).exists(), output

    frontend = _read_json(lane_dir / "frontend_smoke_evidence.json")
    assert frontend["status"] == "ready"
    assert frontend["live_frontend_executed"] is False
    assert frontend["mock_api_routes_used"] is False
    assert frontend["lineage"]["run_id"] == "m10_150"


def test_validate_e2e_consumes_dependency_summaries_without_claiming_live_success(tmp_path: Path) -> None:
    for dependency in ("met", "slurm", "object-store"):
        root = tmp_path / dependency
        root.mkdir()
        (root / "summary.json").write_text(
            json.dumps(
                {
                    "schema": _dependency_schema(dependency),
                    "issue": _dependency_issue(dependency),
                    "run_id": f"{dependency}-run",
                    "status": "ready",
                    "evidence_dir": str(root),
                }
            ),
            encoding="utf-8",
        )

    summary = validate_e2e(
        ProductionE2EConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="deps",
            met_evidence_root=tmp_path / "met",
            slurm_evidence_root=tmp_path / "slurm",
            object_store_evidence_root=tmp_path / "object-store",
        )
    )

    lane_dir = tmp_path / "artifacts" / "deps" / "e2e"
    dependencies = _read_json(lane_dir / "dependency_status.json")["dependencies"]
    assert summary["status"] == "ready"
    assert {item["status"] for item in dependencies} == {"consumed"}
    assert all(item["live_success_claimed"] is False for item in dependencies)
    assert {item["summary_status"] for item in dependencies} == {"ready"}


def test_validate_e2e_consumes_submitted_slurm_dependency_summary(tmp_path: Path) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    (root / "summary.json").write_text(
        json.dumps(
            {
                "schema": "nhms.production_closure.slurm.v1",
                "issue": 147,
                "run_id": "slurm-submit-run",
                "status": "submitted",
                "evidence_dir": str(root),
            }
        ),
        encoding="utf-8",
    )

    summary = validate_e2e(
        ProductionE2EConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="submitted-slurm",
            slurm_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "submitted-slurm" / "e2e"
    dependencies = _read_json(lane_dir / "dependency_status.json")["dependencies"]
    slurm_dependency = next(item for item in dependencies if item["dependency"] == "slurm")
    assert summary["status"] == "ready"
    assert slurm_dependency["status"] == "consumed"
    assert slurm_dependency["summary_status"] == "submitted"
    assert slurm_dependency["live_success_claimed"] is False


def test_validate_e2e_blocks_on_blocked_dependency_summary(tmp_path: Path) -> None:
    root = tmp_path / "met"
    root.mkdir()
    (root / "summary.json").write_text(
        json.dumps(
            {
                "schema": "nhms.production_closure.met.v1",
                "issue": 149,
                "run_id": "met-run",
                "status": "blocked",
            }
        ),
        encoding="utf-8",
    )

    summary = validate_e2e(
        ProductionE2EConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="blockeddep", met_evidence_root=root)
    )

    lane_dir = tmp_path / "artifacts" / "blockeddep" / "e2e"
    stage_manifest = _read_json(lane_dir / "stage_manifest.json")
    api = _read_json(lane_dir / "api_contract_evidence.json")
    frontend = _read_json(lane_dir / "frontend_smoke_evidence.json")

    assert summary["status"] == "blocked"
    assert summary["blockers"][0]["error_code"] == "PRODUCTION_E2E_DEPENDENCY_BLOCKED"
    assert set(stage_manifest["stage_statuses"].values()) == {"blocked"}
    assert all(not stage["outputs"] for stage in stage_manifest["stages"])
    for payload in _stage_artifact_json_payloads(lane_dir):
        assert payload.get("status") != "ready"
        assert payload.get("metadata", {}).get("status") != "ready"
    assert api["status"] == "blocked"
    assert api["execution_mode"] == "not_executed"
    assert frontend["status"] == "blocked"
    assert frontend["execution_mode"] == "not_executed"


@pytest.mark.parametrize("bad_status", ["failed", "error", "not_executed", "unknown"])
def test_validate_e2e_rejects_non_ready_dependency_statuses(tmp_path: Path, bad_status: str) -> None:
    root = tmp_path / "object-store"
    root.mkdir()
    (root / "summary.json").write_text(
        json.dumps(
            {
                "schema": "nhms.production_closure.object_store.v1",
                "issue": 148,
                "run_id": "object-run",
                "status": bad_status,
            }
        ),
        encoding="utf-8",
    )

    summary = validate_e2e(
        ProductionE2EConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id=f"baddep-{bad_status}",
            object_store_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / f"baddep-{bad_status}" / "e2e"
    dependency = _read_json(lane_dir / "dependency_status.json")["dependencies"][1]
    assert summary["status"] == "blocked"
    assert dependency["dependency"] == "object_store"
    assert dependency["status"] == "blocked"
    assert dependency["summary_status"] == bad_status


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": "nhms.production_closure.object_store.v1", "issue": 148},
        {"schema": "nhms.production_closure.met.v1", "issue": 149, "status": "ready"},
    ],
)
def test_validate_e2e_rejects_missing_status_or_wrong_dependency_schema(
    tmp_path: Path,
    payload: dict,
) -> None:
    root = tmp_path / "object-store"
    root.mkdir()
    (root / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    summary = validate_e2e(
        ProductionE2EConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id=f"invaliddep-{len(payload)}",
            object_store_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / f"invaliddep-{len(payload)}" / "e2e"
    dependency = _read_json(lane_dir / "dependency_status.json")["dependencies"][1]
    assert summary["status"] == "blocked"
    assert dependency["status"] == "blocked"
    assert dependency["status"] != "consumed"


def test_validate_e2e_rejects_malformed_dependency_json(tmp_path: Path) -> None:
    root = tmp_path / "met"
    root.mkdir()
    (root / "summary.json").write_text("{not json", encoding="utf-8")

    summary = validate_e2e(
        ProductionE2EConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="malformeddep",
            met_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "malformeddep" / "e2e" / "dependency_status.json")[
        "dependencies"
    ][0]
    assert summary["status"] == "blocked"
    assert dependency["status"] == "blocked"
    assert dependency["execution_mode"] == "not_executed"


@pytest.mark.parametrize(
    ("fixture", "error_code"),
    [
        ("missing_rivqdown", "SHUD_RIVQDOWN_MISSING"),
        ("malformed_columns", "SHUD_RIVQDOWN_MALFORMED_COLUMNS"),
        ("non_finite", "SHUD_RIVQDOWN_NON_FINITE"),
        ("missing_required_output", "SHUD_REQUIRED_OUTPUT_MISSING"),
        ("count_mismatch", "SHUD_RIVQDOWN_COUNT_MISMATCH"),
        ("time_axis_mismatch", "SHUD_RIVQDOWN_TIME_AXIS_MISMATCH"),
    ],
)
def test_validate_e2e_shud_qc_blockers_stop_downstream_publication(
    tmp_path: Path,
    fixture: str,
    error_code: str,
) -> None:
    summary = validate_e2e(
        ProductionE2EConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id=fixture,
            shud_qc_fixture=fixture,
        )
    )

    lane_dir = tmp_path / "artifacts" / fixture / "e2e"
    qc = _read_json(lane_dir / "shud_output_qc.json")
    stage_manifest = _read_json(lane_dir / "stage_manifest.json")
    api = _read_json(lane_dir / "api_contract_evidence.json")
    frontend = _read_json(lane_dir / "frontend_smoke_evidence.json")

    assert summary["status"] == "blocked"
    assert qc["error_code"] == error_code
    assert qc["downstream_publication_blocked"] is True
    assert set(qc["downstream_blocked_stages"]) == {"parse", "frequency", "tile", "api", "frontend"}
    assert stage_manifest["stage_statuses"]["parse"] == "blocked"
    assert stage_manifest["stage_statuses"]["frequency"] == "blocked"
    assert stage_manifest["stage_statuses"]["tile"] == "blocked"
    assert api["status"] == "blocked"
    assert api["execution_mode"] == "not_executed"
    assert frontend["status"] == "blocked"
    assert frontend["execution_mode"] == "not_executed"
    assert qc["retained_paths"]["raw_output_dir"].endswith(f"{fixture}/e2e/raw/shud")


def test_validate_e2e_rejects_unsafe_run_id_before_writes(tmp_path: Path) -> None:
    with pytest.raises(ProductionE2EValidationError) as exc_info:
        ProductionE2EConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="../escape")

    assert exc_info.value.error_code == "PRODUCTION_E2E_RUN_ID_UNSAFE"
    assert not (tmp_path / "artifacts").exists()


def test_validate_e2e_same_run_requires_force(tmp_path: Path) -> None:
    config = ProductionE2EConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="same")
    validate_e2e(config)

    with pytest.raises(ProductionE2EValidationError) as exc_info:
        validate_e2e(config)

    assert exc_info.value.error_code == "PRODUCTION_E2E_EVIDENCE_EXISTS"
    forced_summary = validate_e2e(
        ProductionE2EConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="same", force=True)
    )
    assert forced_summary["status"] == "ready"


@pytest.mark.parametrize(
    ("fixture", "error_code", "missing_name"),
    [
        ("missing_rivqdown", "SHUD_RIVQDOWN_MISSING", "rivqdown"),
        ("missing_required_output", "SHUD_REQUIRED_OUTPUT_MISSING", "log"),
    ],
)
def test_validate_e2e_force_does_not_reuse_stale_shud_raw_outputs(
    tmp_path: Path,
    fixture: str,
    error_code: str,
    missing_name: str,
) -> None:
    validate_e2e(ProductionE2EConfig.from_env(evidence_root=tmp_path / "artifacts", run_id=f"force-{fixture}"))

    summary = validate_e2e(
        ProductionE2EConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id=f"force-{fixture}",
            shud_qc_fixture=fixture,
            force=True,
        )
    )

    lane_dir = tmp_path / "artifacts" / f"force-{fixture}" / "e2e"
    qc = _read_json(lane_dir / "shud_output_qc.json")
    assert summary["status"] == "blocked"
    assert qc["error_code"] == error_code
    assert qc["retained_paths"][missing_name] is None


@pytest.mark.parametrize(
    "fixture",
    [
        "missing_rivqdown",
        "malformed_columns",
        "non_finite",
        "missing_required_output",
        "count_mismatch",
        "time_axis_mismatch",
    ],
)
def test_validate_e2e_force_qc_blocker_removes_stale_downstream_stage_artifacts(
    tmp_path: Path,
    fixture: str,
) -> None:
    run_id = f"force-downstream-{fixture}"
    validate_e2e(ProductionE2EConfig.from_env(evidence_root=tmp_path / "artifacts", run_id=run_id))

    lane_dir = tmp_path / "artifacts" / run_id / "e2e"
    assert _read_json(lane_dir / "stage_artifacts" / "parse" / "parsed_timeseries_manifest.json")["status"] == "ready"
    assert (lane_dir / "stage_artifacts" / "tile" / "0" / "0" / "0.pbf").is_file()

    summary = validate_e2e(
        ProductionE2EConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id=run_id,
            shud_qc_fixture=fixture,
            force=True,
        )
    )

    stage_manifest = _read_json(lane_dir / "stage_manifest.json")
    assert summary["status"] == "blocked"
    for stage in ("parse", "frequency", "tile", "api", "frontend"):
        assert stage_manifest["stage_statuses"][stage] == "blocked"
    for payload in _downstream_stage_artifact_json_payloads(lane_dir):
        assert payload.get("status") != "ready"
        assert payload.get("metadata", {}).get("status") != "ready"
        assert payload.get("execution_mode") == "not_executed"
    assert not (lane_dir / "stage_artifacts" / "tile" / "0" / "0" / "0.pbf").exists()


def test_validate_e2e_force_refuses_symlinked_raw_parent_without_unlinking_external_file(tmp_path: Path) -> None:
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / "rawlink" / "e2e"
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "rawlink.rivqdown"
    external_file.write_text("external evidence must remain\n", encoding="utf-8")
    lane_dir.mkdir(parents=True)
    (lane_dir / "raw").symlink_to(external_dir, target_is_directory=True)

    with pytest.raises(ProductionE2EValidationError) as exc_info:
        validate_e2e(ProductionE2EConfig.from_env(evidence_root=evidence_root, run_id="rawlink", force=True))

    assert exc_info.value.error_code == "PRODUCTION_E2E_EVIDENCE_SYMLINK"
    assert external_file.read_text(encoding="utf-8") == "external evidence must remain\n"


def test_validate_e2e_force_refuses_symlinked_stage_artifacts_without_unlinking_external_file(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / "stagelink" / "e2e"
    external_dir = tmp_path / "external-stage"
    external_dir.mkdir()
    external_file = external_dir / "0.pbf"
    external_file.write_text("external stage artifact must remain\n", encoding="utf-8")
    lane_dir.mkdir(parents=True)
    (lane_dir / "stage_artifacts").symlink_to(external_dir, target_is_directory=True)

    with pytest.raises(ProductionE2EValidationError) as exc_info:
        validate_e2e(ProductionE2EConfig.from_env(evidence_root=evidence_root, run_id="stagelink", force=True))

    assert exc_info.value.error_code == "PRODUCTION_E2E_EVIDENCE_SYMLINK"
    assert external_file.read_text(encoding="utf-8") == "external stage artifact must remain\n"


def test_validate_e2e_rejects_multi_model_set_before_writes(tmp_path: Path) -> None:
    config = ProductionE2EConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="multi",
        model_set="model_a,model_b",
    )

    with pytest.raises(ProductionE2EValidationError) as exc_info:
        validate_e2e(config)

    assert exc_info.value.error_code == "PRODUCTION_E2E_MODEL_SET_UNSUPPORTED"
    assert not (tmp_path / "artifacts" / "multi").exists()


def test_validate_e2e_redacts_secret_shaped_values_from_evidence_and_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_E2E_DB_TARGET", "db-password=supersecret")
    monkeypatch.setenv("NHMS_PRODUCTION_E2E_FRONTEND_API_BASE", "https://frontend.example/api")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")

    exit_code = slurm_validation._argparse_main(
        [
            "validate-e2e",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "redact",
            "--db-target",
            "staging",
            "--object-prefix",
            "s3://bucket/prefix/password=supersecret",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "supersecret" not in captured.out
    assert "supersecret" not in captured.err
    assert "PRODUCTION_E2E_OBJECT_PREFIX_UNSAFE" in captured.err

    exit_code = slurm_validation._argparse_main(
        [
            "validate-e2e",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "redact",
            "--db-target",
            "staging",
            "--object-prefix",
            "s3://bucket/prefix",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "supersecret" not in captured.out
    evidence_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "artifacts" / "redact" / "e2e").glob("*.json")
    )
    assert "supersecret" not in evidence_text
    assert "[redacted]" in evidence_text


def test_validate_e2e_click_and_argparse_dispatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    click_exit = slurm_validation._click_main(
        [
            "validate-e2e",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "click",
        ]
    )
    assert click_exit == 0
    click_summary = json.loads(capsys.readouterr().out)
    assert click_summary["status"] == "ready"

    argparse_exit = _argparse_main(
        [
            "validate-e2e",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "argparse",
        ]
    )
    assert argparse_exit == 0
    argparse_summary = json.loads(capsys.readouterr().out)
    assert argparse_summary["status"] == "ready"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _stage_artifact_json_payloads(lane_dir: Path) -> list[dict]:
    return [_read_json(path) for path in sorted((lane_dir / "stage_artifacts").rglob("*.json"))]


def _downstream_stage_artifact_json_payloads(lane_dir: Path) -> list[dict]:
    downstream_dirs = ("parse", "frequency", "tile", "api", "frontend")
    return [
        _read_json(path)
        for stage_name in downstream_dirs
        for path in sorted((lane_dir / "stage_artifacts" / stage_name).rglob("*.json"))
    ]


def _dependency_schema(dependency: str) -> str:
    return {
        "met": "nhms.production_closure.met.v1",
        "slurm": "nhms.production_closure.slurm.v1",
        "object-store": "nhms.production_closure.object_store.v1",
    }[dependency]


def _dependency_issue(dependency: str) -> int:
    return {"met": 149, "slurm": 147, "object-store": 148}[dependency]
