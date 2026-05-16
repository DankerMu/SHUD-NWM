from __future__ import annotations

import json
import subprocess
from pathlib import Path

from services.production_closure import slurm_validation


def test_validate_slurm_fake_lane_writes_required_evidence_and_redacts(monkeypatch, tmp_path: Path, capsys) -> None:
    evidence_root = tmp_path / "artifacts"
    secret_uri = "s3://user:pass@example.invalid/models/qhh/package?X-Amz-Signature=abc&token=secret"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", secret_uri)
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(tmp_path / "shared-workspace"))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://user:pass@bucket/prod?token=secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@example.invalid/nhms")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret")

    exit_code = slurm_validation.main(
        ["validate-slurm", "--evidence-root", str(evidence_root), "--run-id", "m10_147", "--fake-slurm"]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    lane_dir = evidence_root / "m10_147" / "slurm"
    assert summary["status"] == "ready"
    assert summary["evidence_dir"] == str(lane_dir)
    for name in summary["files"]:
        assert (lane_dir / name).exists()

    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in lane_dir.iterdir() if path.is_file())
    assert "super-secret" not in evidence_text
    assert "token=secret" not in evidence_text
    assert ":secret@" not in evidence_text
    assert "user:pass@" not in evidence_text
    assert "X-Amz-Signature" not in evidence_text
    assert "[redacted]" in evidence_text

    rendered = (lane_dir / "rendered_run_shud_forecast_array.sbatch").read_text(encoding="utf-8")
    assert "#SBATCH --account=friends" in rendered
    assert "#SBATCH --output=" in rendered
    assert "#SBATCH --error=" in rendered
    assert "#SBATCH --cpus-per-task=2" in rendered
    assert "#SBATCH --mem=8G" in rendered
    assert "#SBATCH --time=00:30:00" in rendered
    assert "export SHUD_THREADS=2" in rendered
    assert "export OMP_NUM_THREADS=2" in rendered
    assert (
        'nhms-shud-runtime execute --manifest-index "$NHMS_MANIFEST_INDEX" '
        '--task-id "${SLURM_ARRAY_TASK_ID:-0}"'
    ) in rendered

    manifest_index = json.loads((lane_dir / "manifest_index.json").read_text(encoding="utf-8"))
    assert len(manifest_index) == 2
    assert manifest_index[0]["expected_outcome"] == "succeeded"
    assert manifest_index[1]["expected_outcome"] == "controlled_failure"
    for entry in manifest_index:
        expected_output_uri = f"s3://bucket/prod/runs/{entry['run_id']}/output/"
        expected_log_uri = f"s3://bucket/prod/runs/{entry['run_id']}/logs/"
        expected_forcing_uri = f"s3://bucket/prod/forcing/gfs/2026051600/basin_v1/{entry['model_id']}/"
        assert entry["output_uri"] == expected_output_uri
        assert entry["log_uri"] == expected_log_uri
        assert entry["forcing_uri"] == expected_forcing_uri

        runtime_manifest_path = Path(entry["manifest_path"])
        assert runtime_manifest_path.exists()
        runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
        assert runtime_manifest["run_id"] == entry["run_id"]
        assert runtime_manifest["run_type"] == "forecast"
        assert runtime_manifest["model"]["model_id"] == entry["model_id"]
        assert runtime_manifest["model"]["model_package_uri"]
        assert runtime_manifest["forcing"]["forcing_uri"] == expected_forcing_uri
        assert runtime_manifest["outputs"]["run_manifest_uri"] == (
            f"s3://bucket/prod/runs/{entry['run_id']}/input/manifest.json"
        )
        assert runtime_manifest["outputs"]["output_uri"] == entry["output_uri"]
        assert runtime_manifest["outputs"]["log_uri"] == entry["log_uri"]

    partial = json.loads((lane_dir / "array_partial_success.json").read_text(encoding="utf-8"))
    assert partial["successful_outputs_remain_publishable"] is True
    assert partial["failed_outputs_blocked"] is True
    assert partial["tasks"][0]["publishable"] is True
    assert partial["tasks"][1]["error_code"] == "SLURM_JOB_FAILED"

    qc = json.loads((lane_dir / "qc_blocking.json").read_text(encoding="utf-8"))
    assert qc["malformed_task"]["error_code"] == "NON_FINITE_FLOW"
    assert qc["malformed_task"]["publication_blocked"] is True
    assert qc["sibling_success"]["publishable"] is True


def test_validate_slurm_missing_preflight_writes_blocker_artifact(tmp_path: Path, monkeypatch, capsys) -> None:
    for key in (
        "NHMS_PRODUCTION_SLURM_CLUSTER",
        "NHMS_PRODUCTION_SLURM_ACCOUNT",
        "NHMS_PRODUCTION_SLURM_PARTITION",
        "NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI",
    ):
        monkeypatch.delenv(key, raising=False)

    exit_code = slurm_validation.main(
        ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "blocked"]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert {blocker["field"] for blocker in summary["blockers"]} == {
        "NHMS_PRODUCTION_SLURM_CLUSTER",
        "NHMS_PRODUCTION_SLURM_ACCOUNT",
        "NHMS_PRODUCTION_SLURM_PARTITION",
        "NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI",
    }
    preflight = json.loads((tmp_path / "artifacts" / "blocked" / "slurm" / "preflight.json").read_text())
    assert preflight["schema"] == "nhms.production_closure.slurm.preflight.v1"


def test_validate_slurm_submit_uses_real_command_boundary_with_mocked_slurm(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(tmp_path / "shared-workspace"))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs["shell"] is False if "shell" in kwargs else True
        program = Path(command[0]).name
        if program == "sbatch":
            return subprocess.CompletedProcess(command, 0, stdout="7777\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "7777|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "7777_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "7777_1|FAILED|2:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        if program == "scontrol":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "ClusterName = shudhpc\n"
                    "AccountingStoragePass = supersecret\n"
                    "SlurmctldHost = cn01\n"
                    "SelectType = select/cons_tres\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "submit147", "--submit"]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "submitted"
    accounting = json.loads((tmp_path / "artifacts" / "submit147" / "slurm" / "slurm_accounting.json").read_text())
    assert accounting["mode"] == "submitted"
    assert accounting["job_id"] == "7777"
    assert accounting["records"][0]["state"] == "COMPLETED"
    assert calls[0][:2] == ["sbatch", "--parsable"]
    assert "--array=0-1%2" in calls[0]
    assert "--account=friends" in calls[0]
    assert any(call[0] == "sacct" and "-j" in call for call in calls)
    assert {record["task_id"] for record in accounting["records"] if record["task_id"] is not None} == {0, 1}

    evidence_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "artifacts" / "submit147" / "slurm").iterdir()
        if path.is_file()
    )
    assert "AccountingStoragePass" not in evidence_text
    assert "supersecret" not in evidence_text


def test_validate_slurm_submit_blocks_when_task_accounting_rows_are_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(tmp_path / "shared-workspace"))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            return subprocess.CompletedProcess(command, 0, stdout="8888\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(command, 0, stdout="8888|COMPLETED|0:0|00:00:11|cn04|CPU\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "missingtasks", "--submit"]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert summary["blockers"] == [
        {
            "error_code": "SLURM_ARRAY_TASK_ACCOUNTING_MISSING",
            "field": "sacct",
            "missing_task_ids": "0,1",
        }
    ]
    lane_dir = tmp_path / "artifacts" / "missingtasks" / "slurm"
    accounting = json.loads((lane_dir / "slurm_accounting.json").read_text())
    assert accounting["mode"] == "blocked"
    assert accounting["records"][0]["task_id"] is None
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert partial["status"] == "blocked"
    assert partial["successful_outputs_remain_publishable"] is False
    assert all(task["publishable"] is False for task in partial["tasks"])


def test_validate_slurm_rejects_unsafe_run_id(tmp_path: Path) -> None:
    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "../escape"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert not (tmp_path / "escape").exists()


def test_sacct_evidence_parser_records_stable_fields_and_error_codes() -> None:
    records = slurm_validation.parse_sacct_evidence(
        "123|COMPLETED|0:0|00:01:00|cn04|CPU\n"
        "123_0|COMPLETED|0:0|00:00:59|cn04|CPU\n"
        "123_1|TIMEOUT|1:0|00:30:00|cn05|CPU\n"
        "123_2|OUT_OF_MEMORY|9:0|00:03:00|cn06|GPU\n"
    )

    assert records[0] == {
        "job_id": "123",
        "task_id": None,
        "state": "COMPLETED",
        "exit_code": 0,
        "elapsed": "00:01:00",
        "node_list": "cn04",
        "partition": "CPU",
        "error_code": None,
    }
    assert records[2]["task_id"] == 1
    assert records[2]["error_code"] == "SLURM_TIMEOUT"
    assert records[3]["error_code"] == "OUT_OF_MEMORY"


def shutil_proxy():
    return slurm_validation.shutil
