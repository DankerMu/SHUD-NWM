from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from services.production_closure import slurm_validation


def test_validate_slurm_fake_lane_writes_required_evidence_and_redacts(monkeypatch, tmp_path: Path, capsys) -> None:
    evidence_root = tmp_path / "artifacts"
    secret_uri = "s3://user:pass@example.invalid/models/qhh/package?X-Amz-Signature=abc&token=secret"
    monkeypatch.delenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", raising=False)
    monkeypatch.delenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", raising=False)
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
    assert summary["execution_mode"] == "deterministic_fixture"
    assert summary["deterministic_fixture"] is True
    assert summary["live_slurm_executed"] is False
    assert summary["live_slurm_status"] == "not_executed"
    assert summary["final_production_readiness_claimed"] is False
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
    assert 'VALIDATION_EXPECTED_OUTCOME="$(python - <<' in rendered
    assert 'if [[ "$VALIDATION_EXPECTED_OUTCOME" == "controlled_failure" ]]; then' in rendered
    assert slurm_validation.CONTROLLED_FAILURE_LOG_MARKER in rendered
    assert "NON_FINITE_FLOW" in rendered
    assert "parse_rivqdown_file" in rendered
    assert "controlled_failure.rivqdown" in rendered
    assert (
        'nhms-shud-runtime execute --manifest-index "$NHMS_MANIFEST_INDEX" '
        '--task-id "${SLURM_ARRAY_TASK_ID:-0}"'
    ) in rendered

    manifest_index = json.loads((lane_dir / "manifest_index.json").read_text(encoding="utf-8"))
    assert len(manifest_index) == 2
    assert all(str(lane_dir) in entry["manifest_path"] for entry in manifest_index)
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


def test_validate_slurm_uses_documented_production_object_store_env_names(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    evidence_root = tmp_path / "artifacts"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/prod/models/qhh/package/")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(tmp_path / "shared-workspace"))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "generic-object-store"))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://generic/prefix")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "production-object-store"))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://production/prefix")

    exit_code = slurm_validation.main(
        ["validate-slurm", "--evidence-root", str(evidence_root), "--run-id", "m10_148", "--fake-slurm"]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    lane_dir = evidence_root / "m10_148" / "slurm"
    preflight = json.loads((lane_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight["object_store"]["root"] == str(tmp_path / "production-object-store")
    assert preflight["object_store"]["prefix"] == "s3://production/prefix"

    manifest_index = json.loads((lane_dir / "manifest_index.json").read_text(encoding="utf-8"))
    assert manifest_index[0]["output_uri"] == "s3://production/prefix/runs/m10_148_success/output/"


def test_validate_slurm_missing_preflight_writes_blocker_artifact(tmp_path: Path, monkeypatch, capsys) -> None:
    for key in (
        "NHMS_PRODUCTION_SLURM_CLUSTER",
        "NHMS_PRODUCTION_SLURM_ACCOUNT",
        "NHMS_PRODUCTION_SLURM_PARTITION",
        "NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(tmp_path / "shared-workspace"))

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


def test_validate_slurm_blocked_submit_keeps_manifests_inside_evidence_lane(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.delenv("NHMS_PRODUCTION_SLURM_ACCOUNT", raising=False)
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "blockedsubmit",
            "--submit",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "blockedsubmit" / "slurm"
    assert summary["status"] == "blocked"
    assert summary["manifest_index_path"] == str(lane_dir / "manifest_index.json")
    assert all(str(lane_dir) in path for path in summary["runtime_manifest_paths"])
    assert not (workspace_root / "runs" / "blockedsubmit" / "input" / "manifest_index.json").exists()
    assert not (workspace_root / "runs" / "blockedsubmit_success" / "input" / "manifest.json").exists()


def test_validate_slurm_preflight_only_does_not_publish_planned_success(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.delenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", raising=False)

    exit_code = slurm_validation.main(
        ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "preflightonly"]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "preflightonly" / "slurm"
    assert summary["status"] == "ready"
    assert all(str(lane_dir) in path for path in summary["runtime_manifest_paths"])
    assert not (Path.cwd() / "workspace" / "runs" / "preflightonly_success" / "input" / "manifest.json").exists()
    assert not (Path.cwd() / "workspace" / "runs" / "preflightonly" / "input" / "manifest_index.json").exists()

    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert partial["status"] == "preflight_only"
    assert partial["successful_outputs_remain_publishable"] is False
    assert partial["array_job_id"] is None
    assert all(task["job_id"] is None for task in partial["tasks"])
    assert all(task["publishable"] is False for task in partial["tasks"])

    retry_cancel = json.loads((lane_dir / "retry_cancel.json").read_text())
    assert retry_cancel["cancel"]["state"] == "not_executed"
    assert retry_cancel["cancel"]["job_id"] is None

    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in lane_dir.iterdir() if path.is_file())
    assert "9001" not in evidence_text
    assert "9002" not in evidence_text


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--poll-timeout-seconds", "inf"),
        ("--poll-interval-seconds", "nan"),
        ("--poll-timeout-seconds", "-1"),
        ("--poll-interval-seconds", "0"),
        ("--poll-interval-seconds", "301"),
        ("--poll-timeout-seconds", "86401"),
    ],
)
def test_validate_slurm_rejects_invalid_poll_options_without_evidence(
    tmp_path: Path,
    capsys,
    option: str,
    value: str,
) -> None:
    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "badpoll", option, value]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_SLURM_POLL_OPTION_INVALID" in capsys.readouterr().err
    assert not (tmp_path / "artifacts" / "badpoll" / "slurm").exists()


def test_validate_slurm_rejects_invalid_poll_env_without_evidence(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_POLL_TIMEOUT_SECONDS", "inf")

    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "badpollenv"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_SLURM_POLL_OPTION_INVALID" in capsys.readouterr().err
    assert not (tmp_path / "artifacts" / "badpollenv" / "slurm").exists()


@pytest.mark.parametrize(
    ("argv", "expected_error"),
    [
        (["validate-slurm", "--run-id", "missingroot"], "Missing option '--evidence-root'"),
        (["validate-slurm", "--evidence-root", "artifacts", "--bad-option"], "No such option: --bad-option"),
    ],
)
def test_click_usage_errors_exit_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
    argv: list[str],
    expected_error: str,
) -> None:
    pytest.importorskip("click")
    monkeypatch.chdir(tmp_path)

    try:
        exit_code = slurm_validation.main(argv)
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "Usage:" in captured.err
    if expected_error.startswith("No such option"):
        assert "No such option" in captured.err
        assert "--bad-option" in captured.err
    else:
        assert expected_error in captured.err
    assert "Traceback" not in captured.err


def test_validate_slurm_stdout_redacts_summary_like_evidence(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    secret_package_uri = "s3://user:pass@bucket/path?token=secret&X-Amz-Signature=abc"

    def fake_validate(config: slurm_validation.ProductionSlurmConfig) -> dict[str, object]:
        return {
            "schema": "nhms.production_closure.slurm.v1",
            "run_id": config.run_id,
            "status": "ready",
            "evidence_dir": str(config.lane_dir),
            "model_package_uri": secret_package_uri,
            "notes": "path token=secret x-amz-signature=abc credential=hidden",
        }

    monkeypatch.setattr(slurm_validation, "validate_slurm", fake_validate)

    exit_code = slurm_validation.main(
        ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "stdoutredact"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "user:pass@" not in captured.out
    assert "?token=secret" not in captured.out
    assert "token=secret" not in captured.out
    assert "x-amz-signature=abc" not in captured.out
    assert "credential=hidden" not in captured.out
    assert json.loads(captured.out)["model_package_uri"] == "s3://bucket/path"


def test_packaged_validate_object_store_stdout_redacts_summary_like_evidence(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    secret_package_uri = "s3://user:pass@bucket/path?token=secret&X-Amz-Signature=abc"

    def fake_validate(config: slurm_validation.ProductionObjectStoreConfig) -> dict[str, object]:
        return {
            "schema": "nhms.production_closure.object_store.v1",
            "run_id": config.run_id,
            "status": "ready",
            "evidence_dir": str(config.lane_dir),
            "model_package_uri": secret_package_uri,
            "notes": "path token=secret x-amz-signature=abc credential=hidden",
        }

    monkeypatch.setattr(slurm_validation, "validate_object_store", fake_validate)

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "stdoutobj"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "user:pass@" not in captured.out
    assert "?token=secret" not in captured.out
    assert "token=secret" not in captured.out
    assert "x-amz-signature=abc" not in captured.out
    assert "credential=hidden" not in captured.out
    assert json.loads(captured.out)["model_package_uri"] == "s3://bucket/path"

    exit_code = slurm_validation._argparse_main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts-argparse"), "--run-id", "argparseobj"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "user:pass@" not in captured.out
    assert "?token=secret" not in captured.out
    assert "token=secret" not in captured.out
    assert "x-amz-signature=abc" not in captured.out
    assert "credential=hidden" not in captured.out
    assert json.loads(captured.out)["model_package_uri"] == "s3://bucket/path"


def test_validate_slurm_submit_fake_conflict_fails_without_evidence(
    tmp_path: Path,
    capsys,
) -> None:
    try:
        exit_code = slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                "conflict",
                "--submit",
                "--fake-slurm",
            ]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_SLURM_SUBMIT_FAKE_CONFLICT" in capsys.readouterr().err
    assert not (tmp_path / "artifacts" / "conflict" / "slurm").exists()


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("NHMS_PRODUCTION_SLURM_PARTITION", "CPU\n#SBATCH --nodes=99"),
        ("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends;rm"),
        ("NHMS_PRODUCTION_SLURM_WALLTIME", "00:99:00"),
        ("NHMS_PRODUCTION_SLURM_CPUS_PER_TASK", "0"),
        ("NHMS_PRODUCTION_SLURM_MEMORY_GB", "4097"),
    ],
)
def test_validate_slurm_rejects_invalid_resource_env_without_evidence(
    tmp_path: Path,
    monkeypatch,
    capsys,
    env_name: str,
    value: str,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv(env_name, value)

    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "badresource"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_SLURM_RESOURCE_INVALID" in capsys.readouterr().err
    assert not (tmp_path / "artifacts" / "badresource" / "slurm").exists()


def test_validate_slurm_submit_uses_real_command_boundary_with_mocked_slurm(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(slurm_validation.time, "sleep", lambda seconds: None)
    calls: list[list[str]] = []

    sacct_responses = [
        "7777|PENDING|0:0|00:00:00||CPU\n",
        (
            "7777|COMPLETED|0:0|00:00:11|cn04|CPU\n"
            "7777_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
            "7777_1|FAILED|2:0|00:00:05|cn04|CPU\n"
        ),
    ]

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs["shell"] is False if "shell" in kwargs else True
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = workspace_root / "submit147" / "logs"
            assert log_dir.is_dir()
            (log_dir / "7777_0.out").write_text("task 0 stdout\n", encoding="utf-8")
            (log_dir / "7777_0.err").write_text("task 0 stderr\n", encoding="utf-8")
            (log_dir / "7777_1.out").write_text(
                f"{slurm_validation.CONTROLLED_FAILURE_LOG_MARKER}\nNON_FINITE_FLOW\n",
                encoding="utf-8",
            )
            (log_dir / "7777_1.err").write_text("task 1 stderr\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="7777\n", stderr="")
        if program == "sacct":
            stdout = sacct_responses.pop(0) if sacct_responses else (
                "7777|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                "7777_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                "7777_1|FAILED|2:0|00:00:05|cn04|CPU\n"
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=stdout,
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
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "submit147",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "1",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "submitted"
    accounting = json.loads((tmp_path / "artifacts" / "submit147" / "slurm" / "slurm_accounting.json").read_text())
    assert accounting["mode"] == "submitted"
    assert accounting["job_id"] == "7777"
    assert accounting["poll"]["attempts"] == 2
    assert accounting["records"][0]["state"] == "COMPLETED"
    assert calls[0][:2] == ["sbatch", "--parsable"]
    assert "--array=0-1%2" in calls[0]
    assert "--account=friends" in calls[0]
    assert any(call[0] == "sacct" and "-j" in call for call in calls)
    assert {record["task_id"] for record in accounting["records"] if record["task_id"] is not None} == {0, 1}
    lane_dir = tmp_path / "artifacts" / "submit147" / "slurm"
    rendered = (lane_dir / "rendered_run_shud_forecast_array.sbatch").read_text(encoding="utf-8")
    workspace_manifest_index = tmp_path / "shared-workspace" / "runs" / "submit147" / "input" / "manifest_index.json"
    assert workspace_manifest_index.exists()
    assert f'export NHMS_MANIFEST_INDEX="{workspace_manifest_index}"' in rendered

    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert partial["array_job_id"] == "7777"
    assert partial["tasks"][0]["job_id"] == "7777_0"
    assert partial["tasks"][0]["stderr_path"].endswith("/submit147/logs/7777_0.err")
    assert partial["tasks"][0]["log_verified"] is True
    assert partial["tasks"][1]["job_id"] == "7777_1"
    assert partial["tasks"][1]["stderr_path"].endswith("/submit147/logs/7777_1.err")
    assert partial["tasks"][1]["log_verified"] is True
    qc = json.loads((lane_dir / "qc_blocking.json").read_text())
    assert qc["malformed_task"]["evidence_verified"] is True
    assert qc["malformed_task"]["publication_blocked"] is True
    retry_cancel = json.loads((lane_dir / "retry_cancel.json").read_text())
    assert retry_cancel["cancel"]["state"] == "not_executed"
    assert retry_cancel["cancel"]["job_id"] is None

    evidence_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "artifacts" / "submit147" / "slurm").iterdir()
        if path.is_file()
    )
    assert "AccountingStoragePass" not in evidence_text
    assert "supersecret" not in evidence_text
    assert "9001" not in evidence_text
    assert "9002" not in evidence_text


def test_validate_slurm_submit_blocks_when_shared_logs_are_missing(
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
            return subprocess.CompletedProcess(command, 0, stdout="6677\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "6677|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "6677_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "6677_1|FAILED|2:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "missinglogs",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    blocker_codes = {blocker["error_code"] for blocker in summary["blockers"]}
    assert "SLURM_ARRAY_TASK_LOG_MISSING" in blocker_codes
    assert "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MARKER_MISSING" in blocker_codes
    lane_dir = tmp_path / "artifacts" / "missinglogs" / "slurm"
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert all(task["publishable"] is False for task in partial["tasks"])
    assert all(task["log_verified"] is False for task in partial["tasks"])
    qc = json.loads((lane_dir / "qc_blocking.json").read_text())
    assert qc["malformed_task"]["status"] == "not_verified"
    assert qc["malformed_task"]["publication_blocked"] is False


def test_validate_slurm_submit_blocks_when_controlled_failure_marker_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = workspace_root / "missingmarker" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            for task_id in (0, 1):
                (log_dir / f"7788_{task_id}.out").write_text(f"task {task_id} stdout\n", encoding="utf-8")
                (log_dir / f"7788_{task_id}.err").write_text(f"task {task_id} stderr\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="7788\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "7788|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "7788_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "7788_1|FAILED|2:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "missingmarker",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert [blocker["error_code"] for blocker in summary["blockers"]] == [
        "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MARKER_MISSING"
    ]
    lane_dir = tmp_path / "artifacts" / "missingmarker" / "slurm"
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert partial["tasks"][0]["publishable"] is True
    assert partial["tasks"][1]["publishable"] is False
    assert partial["tasks"][1]["error_code"] == "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MARKER_MISSING"
    qc = json.loads((lane_dir / "qc_blocking.json").read_text())
    assert qc["malformed_task"]["status"] == "not_verified"
    assert qc["malformed_task"]["publication_blocked"] is False


def test_validate_slurm_submit_blocks_when_controlled_failure_signature_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = workspace_root / "missingsignature" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            for task_id in (0, 1):
                (log_dir / f"7799_{task_id}.out").write_text(f"task {task_id} stdout\n", encoding="utf-8")
                (log_dir / f"7799_{task_id}.err").write_text(f"task {task_id} stderr\n", encoding="utf-8")
            (log_dir / "7799_1.out").write_text(
                f"{slurm_validation.CONTROLLED_FAILURE_LOG_MARKER}\nsetup failed\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="7799\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "7799|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "7799_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "7799_1|FAILED|2:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "missingsignature",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert [blocker["error_code"] for blocker in summary["blockers"]] == [
        "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MARKER_MISSING"
    ]
    lane_dir = tmp_path / "artifacts" / "missingsignature" / "slurm"
    qc = json.loads((lane_dir / "qc_blocking.json").read_text())
    assert qc["malformed_task"]["status"] == "not_verified"
    assert qc["malformed_task"]["publication_blocked"] is False


def test_validate_slurm_submit_blocks_symlinked_log_without_touching_target(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    outside = tmp_path / "outside-sentinel.log"
    sentinel = b"external sentinel\n" + (b"x" * (slurm_validation.MAX_SLURM_LOG_BYTES + 1))
    outside.write_bytes(sentinel)

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = workspace_root / "symlinklog" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "7811_0.out").write_text("task 0 stdout\n", encoding="utf-8")
            (log_dir / "7811_0.err").write_text("task 0 stderr\n", encoding="utf-8")
            (log_dir / "7811_1.out").symlink_to(outside)
            (log_dir / "7811_1.err").write_text(
                f"{slurm_validation.CONTROLLED_FAILURE_LOG_MARKER}\nNON_FINITE_FLOW\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="7811\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "7811|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "7811_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "7811_1|FAILED|2:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "symlinklog",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    assert outside.read_bytes() == sentinel
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    blocker_codes = [blocker["error_code"] for blocker in summary["blockers"]]
    assert "SLURM_ARRAY_TASK_LOG_UNSAFE" in blocker_codes
    lane_dir = tmp_path / "artifacts" / "symlinklog" / "slurm"
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    task1 = next(task for task in partial["tasks"] if task["task_id"] == 1)
    assert task1["log_status"] == "blocked"
    assert task1["error_code"] == "SLURM_ARRAY_TASK_LOG_UNSAFE"


def test_validate_slurm_submit_blocks_fifo_log_without_hanging(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = workspace_root / "fifolog" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "7814_0.out").write_text("task 0 stdout\n", encoding="utf-8")
            (log_dir / "7814_0.err").write_text("task 0 stderr\n", encoding="utf-8")
            os.mkfifo(log_dir / "7814_1.out")
            (log_dir / "7814_1.err").write_text(
                f"{slurm_validation.CONTROLLED_FAILURE_LOG_MARKER}\nNON_FINITE_FLOW\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="7814\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "7814|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "7814_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "7814_1|FAILED|2:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "fifolog",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    blocker_codes = [blocker["error_code"] for blocker in summary["blockers"]]
    assert "SLURM_ARRAY_TASK_LOG_UNREADABLE" in blocker_codes
    lane_dir = tmp_path / "artifacts" / "fifolog" / "slurm"
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    task1 = next(task for task in partial["tasks"] if task["task_id"] == 1)
    assert task1["log_status"] == "blocked"
    assert task1["error_code"] == "SLURM_ARRAY_TASK_LOG_UNREADABLE"


def test_validate_slurm_submit_blocks_log_swapped_to_symlink_after_path_check(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    outside = tmp_path / "outside-controlled-failure.log"
    outside.write_text(
        f"{slurm_validation.CONTROLLED_FAILURE_LOG_MARKER}\nNON_FINITE_FLOW\n",
        encoding="utf-8",
    )
    swapped = False
    original_validate_path = slurm_validation._validate_slurm_log_path
    original_open = slurm_validation.os.open

    def swap_after_path_check(config, path, *, field, task_id):
        nonlocal swapped
        blocker = original_validate_path(config, path, field=field, task_id=task_id)
        if blocker is None and not swapped and task_id == 1 and field == "task_1_out":
            path.unlink()
            path.symlink_to(outside)
            swapped = True
        return blocker

    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        assert Path(path) != outside
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(slurm_validation, "_validate_slurm_log_path", swap_after_path_check)
    monkeypatch.setattr(slurm_validation.os, "open", guarded_open)

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = workspace_root / "racedlog" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "7813_0.out").write_text("task 0 stdout\n", encoding="utf-8")
            (log_dir / "7813_0.err").write_text("task 0 stderr\n", encoding="utf-8")
            (log_dir / "7813_1.out").write_text("initial benign stdout\n", encoding="utf-8")
            (log_dir / "7813_1.err").write_text("task 1 stderr\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="7813\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "7813|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "7813_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "7813_1|FAILED|2:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "racedlog",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    assert swapped is True
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    blocker_codes = [blocker["error_code"] for blocker in summary["blockers"]]
    assert "SLURM_ARRAY_TASK_LOG_UNSAFE" in blocker_codes
    assert "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MARKER_MISSING" in blocker_codes
    lane_dir = tmp_path / "artifacts" / "racedlog" / "slurm"
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    task1 = next(task for task in partial["tasks"] if task["task_id"] == 1)
    assert task1["log_status"] == "blocked"
    assert task1["error_code"] == "SLURM_ARRAY_TASK_LOG_UNSAFE"


def test_validate_slurm_submit_blocks_oversized_log(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = workspace_root / "oversizedlog" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "7812_0.out").write_text("task 0 stdout\n", encoding="utf-8")
            (log_dir / "7812_0.err").write_text("task 0 stderr\n", encoding="utf-8")
            (log_dir / "7812_1.out").write_bytes(b"x" * (slurm_validation.MAX_SLURM_LOG_BYTES + 1))
            (log_dir / "7812_1.err").write_text(
                f"{slurm_validation.CONTROLLED_FAILURE_LOG_MARKER}\nNON_FINITE_FLOW\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="7812\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "7812|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "7812_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "7812_1|FAILED|2:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "oversizedlog",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    blocker_codes = [blocker["error_code"] for blocker in summary["blockers"]]
    assert "SLURM_ARRAY_TASK_LOG_TOO_LARGE" in blocker_codes
    lane_dir = tmp_path / "artifacts" / "oversizedlog" / "slurm"
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    task1 = next(task for task in partial["tasks"] if task["task_id"] == 1)
    assert task1["log_status"] == "blocked"
    assert task1["error_code"] == "SLURM_ARRAY_TASK_LOG_TOO_LARGE"


def test_validate_slurm_submit_blocks_when_task_accounting_rows_never_finish(
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
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "8888|RUNNING|0:0|00:00:11|cn04|CPU\n"
                    "8888_0|RUNNING|0:0|00:00:09|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "missingtasks",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert {blocker["error_code"] for blocker in summary["blockers"]} == {
        "SLURM_ARRAY_TASK_ACCOUNTING_MISSING",
        "SLURM_ARRAY_TASK_ACCOUNTING_UNFINISHED",
    }
    assert all(blocker["timeout"] == "true" for blocker in summary["blockers"])
    lane_dir = tmp_path / "artifacts" / "missingtasks" / "slurm"
    accounting = json.loads((lane_dir / "slurm_accounting.json").read_text())
    assert accounting["mode"] == "blocked"
    assert accounting["records"][0]["task_id"] is None
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert partial["status"] == "blocked"
    assert partial["successful_outputs_remain_publishable"] is False
    workspace_root = tmp_path / "shared-workspace"
    assert not (workspace_root / "runs" / "sbatchfailed" / "input" / "manifest_index.json").exists()
    assert not (workspace_root / "runs" / "sbatchfailed_success" / "input" / "manifest.json").exists()
    assert not (workspace_root / "runs" / "sbatchfailed_controlled_fail" / "input" / "manifest.json").exists()
    assert all(task["publishable"] is False for task in partial["tasks"])


def test_validate_slurm_submit_blocks_when_controlled_failure_does_not_occur(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = workspace_root / "nofail" / "logs"
            assert log_dir.is_dir()
            for task_id in (0, 1):
                (log_dir / f"9999_{task_id}.out").write_text(f"task {task_id} stdout\n", encoding="utf-8")
                (log_dir / f"9999_{task_id}.err").write_text(f"task {task_id} stderr\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="9999\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "9999|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "9999_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "9999_1|COMPLETED|0:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "nofail",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert summary["blockers"] == [
        {
            "error_code": "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MISSING",
            "field": "sacct",
            "task_id": "1",
            "state": "COMPLETED",
            "exit_code": "0",
            "timeout": "true",
        }
    ]


@pytest.mark.parametrize("state", ["CANCELLED by 123", "TIMEOUT", "OUT_OF_MEMORY"])
def test_validate_slurm_submit_blocks_cancel_timeout_and_oom_as_controlled_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
    state: str,
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
            return subprocess.CompletedProcess(command, 0, stdout="7778\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "7778|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "7778_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    f"7778_1|{state}|2:0|00:00:05|cn04|CPU\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            f"block{state.split()[0].lower()}",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert summary["blockers"][0]["error_code"] == "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MISSING"


def test_validate_slurm_submit_sbatch_failure_writes_blocked_bundle(
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
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="invalid account")
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "sbatchfailed",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "sbatchfailed" / "slurm"
    assert summary["status"] == "blocked"
    assert summary["blockers"] == [
        {"error_code": "SBATCH_SUBMISSION_FAILED", "field": "sbatch", "returncode": "1"}
    ]
    for name in [*summary["files"], "summary.json"]:
        assert (lane_dir / name).exists()

    accounting = json.loads((lane_dir / "slurm_accounting.json").read_text())
    assert accounting["mode"] == "blocked"
    assert accounting["submit"]["returncode"] == 1
    assert accounting["submit"]["stderr"] == "invalid account"
    assert accounting["poll"]["attempts"] == 0
    assert accounting["shared_runtime_inputs_cleaned"] is True
    assert {item["status"] for item in accounting["shared_runtime_input_cleanup"]} == {"absent"}
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert partial["status"] == "blocked"
    assert partial["successful_outputs_remain_publishable"] is False
    workspace_root = tmp_path / "shared-workspace"
    assert summary["manifest_index_path"] == str(lane_dir / "manifest_index.json")
    assert all(str(lane_dir) in path for path in summary["runtime_manifest_paths"])
    assert not (workspace_root / "runs" / "sbatchfailed" / "input" / "manifest_index.json").exists()
    assert not (workspace_root / "runs" / "sbatchfailed_success" / "input" / "manifest.json").exists()
    assert not (workspace_root / "runs" / "sbatchfailed_controlled_fail" / "input" / "manifest.json").exists()


def test_validate_slurm_rejects_unsafe_run_id(tmp_path: Path) -> None:
    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "../escape"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert not (tmp_path / "escape").exists()


def test_validate_slurm_refuses_existing_evidence_file_unless_force(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    evidence_root = tmp_path / "artifacts"
    existing = evidence_root / "rerun" / "slurm" / "preflight.json"
    existing.parent.mkdir(parents=True)
    existing.write_text('{"old": true}\n', encoding="utf-8")

    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(evidence_root), "--run-id", "rerun", "--fake-slurm"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_SLURM_EVIDENCE_EXISTS" in capsys.readouterr().err
    assert json.loads(existing.read_text(encoding="utf-8")) == {"old": True}

    assert (
        slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(evidence_root),
                "--run-id",
                "rerun",
                "--fake-slurm",
                "--force",
            ]
        )
        == 0
    )


def test_validate_slurm_refuses_symlinked_runtime_manifest(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")

    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir = workspace_root / "runs" / "symlinkmanifest_success"
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(outside, target_is_directory=True)

    try:
        exit_code = slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                "symlinkmanifest",
                "--submit",
            ]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert not (outside / "input" / "manifest.json").exists()
    assert run_dir.is_symlink()


def test_validate_slurm_refuses_existing_runtime_manifest_unless_force(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="blocked after manifest write")
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    manifest_path = workspace_root / "runs" / "existingmanifest_success" / "input" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"old": true}\n', encoding="utf-8")

    try:
        exit_code = slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                "existingmanifest",
                "--submit",
            ]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_SLURM_RUNTIME_MANIFEST_EXISTS" in capsys.readouterr().err
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {"old": True}

    assert (
        slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                "existingmanifest",
                "--submit",
                "--force",
            ]
        )
        == 0
    )
    assert not manifest_path.exists()


def test_validate_slurm_submit_reports_shared_input_cleanup_failure(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(tmp_path / "shared-workspace"))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")

    original_unlink = Path.unlink

    def fake_unlink(path: Path, *args, **kwargs):
        if path.name == "manifest_index.json":
            raise OSError("nfs busy")
        return original_unlink(path, *args, **kwargs)

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            monkeypatch.setattr(Path, "unlink", fake_unlink)
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="invalid account")
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    exit_code = slurm_validation.main(
        [
            "validate-slurm",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "cleanupfailed",
            "--submit",
            "--poll-interval-seconds",
            "1",
            "--poll-timeout-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert {blocker["error_code"] for blocker in summary["blockers"]} == {
        "SBATCH_SUBMISSION_FAILED",
        "PRODUCTION_SLURM_SHARED_INPUT_CLEANUP_FAILED",
    }
    lane_dir = tmp_path / "artifacts" / "cleanupfailed" / "slurm"
    accounting = json.loads((lane_dir / "slurm_accounting.json").read_text())
    assert accounting["shared_runtime_inputs_cleaned"] is False
    assert any(item["status"] == "failed" for item in accounting["shared_runtime_input_cleanup"])
    assert (tmp_path / "shared-workspace" / "runs" / "cleanupfailed" / "input" / "manifest_index.json").exists()


def test_validate_slurm_rejects_symlinked_lane_and_evidence_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence_root = tmp_path / "artifacts"
    (evidence_root / "symlinklane").parent.mkdir(parents=True)
    (evidence_root / "symlinklane").symlink_to(outside, target_is_directory=True)

    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(evidence_root), "--run-id", "symlinklane", "--fake-slurm"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert not (outside / "slurm" / "preflight.json").exists()

    evidence_root = tmp_path / "artifacts_file"
    lane_dir = evidence_root / "symlinkfile" / "slurm"
    lane_dir.mkdir(parents=True)
    target = outside / "preflight.json"
    (lane_dir / "preflight.json").symlink_to(target)

    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(evidence_root), "--run-id", "symlinkfile", "--fake-slurm"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert not target.exists()


def test_sacct_evidence_parser_records_stable_fields_and_error_codes() -> None:
    records = slurm_validation.parse_sacct_evidence(
        "123|COMPLETED|0:0|00:01:00|cn04|CPU\n"
        "123_0|COMPLETED|0:0|00:00:59|cn04|CPU\n"
        "123_1|CANCELLED by 123|1:0|00:30:00|cn05|CPU\n"
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
    assert records[2]["state"] == "CANCELLED"
    assert records[2]["error_code"] is None
    assert records[3]["error_code"] == "OUT_OF_MEMORY"


def shutil_proxy():
    return slurm_validation.shutil
