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
            return subprocess.CompletedProcess(command, 0, stdout="7777|COMPLETED|0:0|00:00:11|cn04|CPU\n", stderr="")
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
    assert any(call[0] == "sacct" and "-j" in call for call in calls)


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
