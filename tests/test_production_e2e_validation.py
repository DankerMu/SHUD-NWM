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
        "flood_alerts",
        "pipeline_job",
        "pipeline_logs",
        "tile_metadata",
    }

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
            json.dumps({"run_id": f"{dependency}-run", "status": "ready", "evidence_dir": str(root)}),
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


def test_validate_e2e_blocks_on_blocked_dependency_summary(tmp_path: Path) -> None:
    root = tmp_path / "met"
    root.mkdir()
    (root / "summary.json").write_text(json.dumps({"run_id": "met-run", "status": "blocked"}), encoding="utf-8")

    summary = validate_e2e(
        ProductionE2EConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="blockeddep", met_evidence_root=root)
    )

    assert summary["status"] == "blocked"
    assert summary["blockers"][0]["error_code"] == "PRODUCTION_E2E_DEPENDENCY_BLOCKED"


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
