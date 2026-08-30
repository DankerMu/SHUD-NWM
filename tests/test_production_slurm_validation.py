from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from packages.common import safe_fs
from services.production_closure import slurm_validation
from services.slurm_gateway.real_backend import SLURM_STATE_MAP
from tests.slurm_template_helpers import _join_line_continuations

# Spec layout: workspace/{run_id}_cycle/array_logs/{index-stem}. Independent of
# production_closure helpers so regressions cannot pass by construction.
_PRODUCTION_ARRAY_INDEX_STEM = "manifest_index"


def _neutral_array_log_dir(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / f"{run_id}_cycle" / "array_logs" / _PRODUCTION_ARRAY_INDEX_STEM


def _rendered_array_log_dir(rendered: str) -> Path:
    output_line = next(line for line in rendered.splitlines() if line.startswith("#SBATCH --output="))
    error_line = next(line for line in rendered.splitlines() if line.startswith("#SBATCH --error="))
    output_dir = Path(output_line.split("=", maxsplit=1)[1]).parent
    error_dir = Path(error_line.split("=", maxsplit=1)[1]).parent
    assert output_dir == error_dir
    return output_dir


def _assert_no_shared_array_log_dir(workspace_root: Path, run_id: str) -> None:
    assert not (workspace_root / f"{run_id}_cycle").exists()


@pytest.fixture(autouse=True)
def _valid_shud_executable_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> None:
    """Default to a valid SHUD executable so the #257 preflight does not add an
    unrelated blocker to the existing Slurm validation/accounting tests.

    Tests exercising stub/missing-executable behavior override SHUD_EXECUTABLE
    explicitly via monkeypatch.setenv.
    """

    bin_dir = tmp_path_factory.mktemp("shud_bin")
    executable = bin_dir / "shud_omp"
    # Mirror the real SHUD binary: only a no-argument call prints the banner.
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -gt 0 ]; then\n'
        '  echo "Unknown option: $1" >&2\n'
        "  exit 1\n"
        "fi\n"
        'echo "Simulator for Hydrologic Unstructured Domains v2.0  2022"\n'
        'echo "./shud [-0gv] <project_name>"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("SHUD_EXECUTABLE", str(executable))


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
    # Command form only: the heredoc-region assertions above keep judging the
    # raw rendering, where bash performs no continuation.
    assert (
        'nhms-shud-runtime execute --manifest-index "$NHMS_MANIFEST_INDEX" '
        '--task-id "${SLURM_ARRAY_TASK_ID:-0}"'
    ) in _join_line_continuations(rendered)

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

    workspace_root = tmp_path / "shared-workspace"
    expected_log_dir = _neutral_array_log_dir(workspace_root, "m10_147")
    assert _rendered_array_log_dir(rendered) == expected_log_dir
    _assert_no_shared_array_log_dir(workspace_root, "m10_147")


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
    _assert_no_shared_array_log_dir(tmp_path / "shared-workspace", "blocked")


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
    _assert_no_shared_array_log_dir(workspace_root, "blockedsubmit")


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
    _assert_no_shared_array_log_dir(Path.cwd() / "workspace", "preflightonly")

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
        ("NHMS_PRODUCTION_SLURM_WALLTIME", "31-00:00:00"),
        ("NHMS_PRODUCTION_SLURM_WALLTIME", "00:00:00"),
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
            script_path = Path(command[-1])
            rendered = script_path.read_text(encoding="utf-8")
            log_dir = _rendered_array_log_dir(rendered)
            expected_log_dir = _neutral_array_log_dir(workspace_root, "submit147")
            assert log_dir == expected_log_dir
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
    sbatch_call = next(call for call in calls if Path(call[0]).name == "sbatch")
    assert sbatch_call[:2] == ["sbatch", "--parsable"]
    assert "--array=0-1%2" in sbatch_call
    assert "--account=friends" in sbatch_call
    assert any(Path(call[0]).name == "sacct" and "-j" in call for call in calls)
    assert {record["task_id"] for record in accounting["records"] if record["task_id"] is not None} == {0, 1}
    lane_dir = tmp_path / "artifacts" / "submit147" / "slurm"
    rendered = (lane_dir / "rendered_run_shud_forecast_array.sbatch").read_text(encoding="utf-8")
    workspace_manifest_index = tmp_path / "shared-workspace" / "runs" / "submit147" / "input" / "manifest_index.json"
    expected_log_dir = _neutral_array_log_dir(workspace_root, "submit147")
    assert workspace_manifest_index.exists()
    assert f"export NHMS_MANIFEST_INDEX={workspace_manifest_index}" in rendered
    assert _rendered_array_log_dir(rendered) == expected_log_dir
    assert expected_log_dir.is_dir()
    assert not (workspace_root / "submit147" / "logs").exists()

    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert partial["array_job_id"] == "7777"
    assert partial["tasks"][0]["job_id"] == "7777_0"
    assert Path(partial["tasks"][0]["stdout_path"]) == expected_log_dir / "7777_0.out"
    assert Path(partial["tasks"][0]["stderr_path"]) == expected_log_dir / "7777_0.err"
    assert Path(partial["tasks"][0]["stderr_path"]).parent == expected_log_dir
    assert Path(partial["tasks"][0]["stderr_path"]).parent == _rendered_array_log_dir(rendered)
    assert partial["tasks"][0]["log_verified"] is True
    assert partial["tasks"][1]["job_id"] == "7777_1"
    assert Path(partial["tasks"][1]["stdout_path"]) == expected_log_dir / "7777_1.out"
    assert Path(partial["tasks"][1]["stderr_path"]) == expected_log_dir / "7777_1.err"
    assert Path(partial["tasks"][1]["stderr_path"]).parent == expected_log_dir
    assert Path(partial["tasks"][1]["stderr_path"]).parent == _rendered_array_log_dir(rendered)
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
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    expected_log_dir = _neutral_array_log_dir(workspace_root, "missinglogs")

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = _rendered_array_log_dir(Path(command[-1]).read_text(encoding="utf-8"))
            assert log_dir == expected_log_dir
            assert log_dir.is_dir()
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
    missing_paths = {
        Path(blocker["path"])
        for blocker in summary["blockers"]
        if blocker["error_code"] == "SLURM_ARRAY_TASK_LOG_MISSING"
    }
    assert missing_paths
    assert all(path.parent == expected_log_dir for path in missing_paths)
    lane_dir = tmp_path / "artifacts" / "missinglogs" / "slurm"
    rendered = (lane_dir / "rendered_run_shud_forecast_array.sbatch").read_text(encoding="utf-8")
    assert _rendered_array_log_dir(rendered) == expected_log_dir
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert all(task["publishable"] is False for task in partial["tasks"])
    assert all(task["log_verified"] is False for task in partial["tasks"])
    assert Path(partial["tasks"][0]["stdout_path"]).parent == expected_log_dir
    assert Path(partial["tasks"][1]["stderr_path"]).parent == expected_log_dir
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
            log_dir = _rendered_array_log_dir(Path(command[-1]).read_text(encoding="utf-8"))
            assert log_dir == _neutral_array_log_dir(workspace_root, "missingmarker")
            assert log_dir.is_dir()
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
    expected_log_dir = _neutral_array_log_dir(workspace_root, "missingmarker")
    rendered = (lane_dir / "rendered_run_shud_forecast_array.sbatch").read_text(encoding="utf-8")
    assert _rendered_array_log_dir(rendered) == expected_log_dir
    partial = json.loads((lane_dir / "array_partial_success.json").read_text())
    assert Path(partial["tasks"][1]["stdout_path"]).parent == expected_log_dir
    assert Path(partial["tasks"][1]["stderr_path"]).parent == expected_log_dir
    assert partial["tasks"][0]["publishable"] is True
    assert partial["tasks"][1]["publishable"] is False
    assert partial["tasks"][1]["error_code"] == "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MARKER_MISSING"
    qc = json.loads((lane_dir / "qc_blocking.json").read_text())
    assert qc["malformed_task"]["status"] == "not_verified"
    assert qc["malformed_task"]["publication_blocked"] is False


def test_validate_slurm_shared_log_dir_refuses_symlink_swap_before_mkdir_without_external_write(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    run_id = "logmkdirswap"
    workspace_root = tmp_path / "shared-workspace"
    external = tmp_path / "external-shared-logs"
    external.mkdir()
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    original_ensure = safe_fs.ensure_directory_no_follow
    swapped = False

    expected_log_dir = _neutral_array_log_dir(workspace_root, run_id)
    cycle_root = expected_log_dir.parent.parent

    def swap_run_workspace_before_log_dir_create(path: Path, *, containment_root: Path | None = None) -> Path:
        nonlocal swapped
        if path == expected_log_dir and not swapped:
            swapped = True
            cycle_root.symlink_to(external, target_is_directory=True)
        return original_ensure(path, containment_root=containment_root)

    monkeypatch.setattr(safe_fs, "ensure_directory_no_follow", swap_run_workspace_before_log_dir_create)
    monkeypatch.setattr(slurm_validation, "ensure_directory_no_follow", swap_run_workspace_before_log_dir_create)
    sbatch_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        if Path(command[0]).name == "sbatch":
            sbatch_calls.append(command)
            raise AssertionError("sbatch must not run after an unsafe array log directory swap")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as extra_exc:
        slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                run_id,
                "--submit",
                "--poll-interval-seconds",
                "1",
                "--poll-timeout-seconds",
                "0",
            ]
        )

    assert extra_exc.value.code == 1
    assert swapped is True
    assert sbatch_calls == []
    captured = capsys.readouterr()
    assert "PRODUCTION_SLURM_LOG_DIR_INVALID" in captured.err
    assert "Traceback" not in captured.err
    assert sorted(path.name for path in external.iterdir()) == []


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
            log_dir = _rendered_array_log_dir(Path(command[-1]).read_text(encoding="utf-8"))
            assert log_dir == _neutral_array_log_dir(workspace_root, "missingsignature")
            assert log_dir.is_dir()
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
            log_dir = _rendered_array_log_dir(Path(command[-1]).read_text(encoding="utf-8"))
            assert log_dir == _neutral_array_log_dir(workspace_root, "symlinklog")
            assert log_dir.is_dir()
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
            log_dir = _rendered_array_log_dir(Path(command[-1]).read_text(encoding="utf-8"))
            assert log_dir == _neutral_array_log_dir(workspace_root, "fifolog")
            assert log_dir.is_dir()
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

    def swap_after_path_check(config, path, *, field, task_id, manifest_index):
        nonlocal swapped
        blocker = original_validate_path(config, path, field=field, task_id=task_id, manifest_index=manifest_index)
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
            log_dir = _rendered_array_log_dir(Path(command[-1]).read_text(encoding="utf-8"))
            assert log_dir == _neutral_array_log_dir(workspace_root, "racedlog")
            assert log_dir.is_dir()
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


def test_validate_slurm_submit_blocks_log_parent_swapped_to_symlink_after_path_check(
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
    outside = tmp_path / "external-logs"
    outside.mkdir()
    outside_marker = outside / "7815_1.out"
    external_sentinel = "EXTERNAL_PARENT_SWAP_MARKER_DO_NOT_READ"
    outside_marker.write_text(
        f"{slurm_validation.CONTROLLED_FAILURE_LOG_MARKER}\nNON_FINITE_FLOW\n{external_sentinel}\n",
        encoding="utf-8",
    )
    swapped = False
    original_validate_path = slurm_validation._validate_slurm_log_path
    original_open = slurm_validation.os.open

    def swap_parent_after_path_check(config, path, *, field, task_id, manifest_index):
        nonlocal swapped
        blocker = original_validate_path(config, path, field=field, task_id=task_id, manifest_index=manifest_index)
        if blocker is None and not swapped and task_id == 1 and field == "task_1_out":
            log_dir = _neutral_array_log_dir(workspace_root, "parentracedlog")
            for child in log_dir.iterdir():
                child.unlink()
            log_dir.rmdir()
            log_dir.symlink_to(outside, target_is_directory=True)
            swapped = True
        return blocker

    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path) == outside or Path(path) == outside_marker:
            raise AssertionError("external Slurm log path must not be opened")
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(slurm_validation, "_validate_slurm_log_path", swap_parent_after_path_check)
    monkeypatch.setattr(slurm_validation.os, "open", guarded_open)

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            log_dir = _rendered_array_log_dir(Path(command[-1]).read_text(encoding="utf-8"))
            assert log_dir == _neutral_array_log_dir(workspace_root, "parentracedlog")
            assert log_dir.is_dir()
            (log_dir / "7815_0.out").write_text("task 0 stdout\n", encoding="utf-8")
            (log_dir / "7815_0.err").write_text("task 0 stderr\n", encoding="utf-8")
            (log_dir / "7815_1.out").write_text("initial benign stdout\n", encoding="utf-8")
            (log_dir / "7815_1.err").write_text("task 1 stderr\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="7815\n", stderr="")
        if program == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "7815|COMPLETED|0:0|00:00:11|cn04|CPU\n"
                    "7815_0|COMPLETED|0:0|00:00:10|cn04|CPU\n"
                    "7815_1|FAILED|2:0|00:00:05|cn04|CPU\n"
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
            "parentracedlog",
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
    lane_dir = tmp_path / "artifacts" / "parentracedlog" / "slurm"
    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in lane_dir.iterdir() if path.is_file())
    assert external_sentinel not in evidence_text
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
            log_dir = _rendered_array_log_dir(Path(command[-1]).read_text(encoding="utf-8"))
            assert log_dir == _neutral_array_log_dir(workspace_root, "oversizedlog")
            assert log_dir.is_dir()
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
            log_dir = _rendered_array_log_dir(Path(command[-1]).read_text(encoding="utf-8"))
            assert log_dir == _neutral_array_log_dir(workspace_root, "nofail")
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
    expected_log_dir = _neutral_array_log_dir(workspace_root, "sbatchfailed")
    rendered = (lane_dir / "rendered_run_shud_forecast_array.sbatch").read_text(encoding="utf-8")
    assert _rendered_array_log_dir(rendered) == expected_log_dir
    assert expected_log_dir.is_dir()
    assert list(expected_log_dir.iterdir()) == []


def test_validate_slurm_submit_refuses_regular_file_neutral_log_dir_without_sbatch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    workspace_root = tmp_path / "shared-workspace"
    run_id = "filelogdir"
    expected_log_dir = _neutral_array_log_dir(workspace_root, run_id)
    expected_log_dir.parent.mkdir(parents=True)
    expected_log_dir.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    sbatch_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        if Path(command[0]).name == "sbatch":
            sbatch_calls.append(command)
            raise AssertionError("sbatch must not run when the array log directory is unsafe")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                run_id,
                "--submit",
                "--poll-interval-seconds",
                "1",
                "--poll-timeout-seconds",
                "0",
            ]
        )

    assert exc_info.value.code == 1
    assert sbatch_calls == []
    captured = capsys.readouterr()
    assert "PRODUCTION_SLURM_LOG_DIR_INVALID" in captured.err
    assert "Traceback" not in captured.err
    assert expected_log_dir.is_file()
    assert expected_log_dir.read_text(encoding="utf-8") == "not a directory\n"


def test_validate_slurm_submit_refuses_fifo_neutral_log_dir_without_sbatch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    workspace_root = tmp_path / "shared-workspace"
    run_id = "fifologdir"
    expected_log_dir = _neutral_array_log_dir(workspace_root, run_id)
    expected_log_dir.parent.mkdir(parents=True)
    os.mkfifo(expected_log_dir)
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    sbatch_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        if Path(command[0]).name == "sbatch":
            sbatch_calls.append(command)
            raise AssertionError("sbatch must not run when the array log directory is a FIFO")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as extra_exc:
        slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                run_id,
                "--submit",
                "--poll-interval-seconds",
                "1",
                "--poll-timeout-seconds",
                "0",
            ]
        )

    assert extra_exc.value.code == 1
    assert sbatch_calls == []
    captured = capsys.readouterr()
    assert "PRODUCTION_SLURM_LOG_DIR_INVALID" in captured.err
    assert "Traceback" not in captured.err
    assert expected_log_dir.exists()
    assert not expected_log_dir.is_dir()
    assert not expected_log_dir.is_symlink()


def test_validate_slurm_submit_refuses_symlink_leaf_neutral_log_dir_without_sbatch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    workspace_root = tmp_path / "shared-workspace"
    run_id = "symlinkleaflogdir"
    expected_log_dir = _neutral_array_log_dir(workspace_root, run_id)
    external = tmp_path / "external-leaf-logs"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("do-not-write\n", encoding="utf-8")
    expected_log_dir.parent.mkdir(parents=True)
    expected_log_dir.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    sbatch_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        if Path(command[0]).name == "sbatch":
            sbatch_calls.append(command)
            raise AssertionError("sbatch must not run when the array log directory is a symlink")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as extra_exc:
        slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                run_id,
                "--submit",
                "--poll-interval-seconds",
                "1",
                "--poll-timeout-seconds",
                "0",
            ]
        )

    assert extra_exc.value.code == 1
    assert sbatch_calls == []
    captured = capsys.readouterr()
    assert "PRODUCTION_SLURM_LOG_DIR_INVALID" in captured.err
    assert "Traceback" not in captured.err
    assert expected_log_dir.is_symlink()
    assert sorted(path.name for path in external.iterdir()) == ["sentinel.txt"]
    assert sentinel.read_text(encoding="utf-8") == "do-not-write\n"


def test_validate_slurm_submit_refuses_symlink_ancestor_neutral_log_dir_without_sbatch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    workspace_root = tmp_path / "shared-workspace"
    run_id = "symlinkancestorlogdir"
    expected_log_dir = _neutral_array_log_dir(workspace_root, run_id)
    cycle_root = expected_log_dir.parent.parent
    external = tmp_path / "external-cycle"
    (external / "array_logs" / expected_log_dir.name).mkdir(parents=True)
    sentinel = external / "array_logs" / expected_log_dir.name / "sentinel.txt"
    sentinel.write_text("do-not-write\n", encoding="utf-8")
    workspace_root.mkdir(parents=True)
    cycle_root.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    sbatch_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        if Path(command[0]).name == "sbatch":
            sbatch_calls.append(command)
            raise AssertionError("sbatch must not run when an ancestor of the array log directory is a symlink")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as extra_exc:
        slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                run_id,
                "--submit",
                "--poll-interval-seconds",
                "1",
                "--poll-timeout-seconds",
                "0",
            ]
        )

    assert extra_exc.value.code == 1
    assert sbatch_calls == []
    captured = capsys.readouterr()
    assert "PRODUCTION_SLURM_LOG_DIR_INVALID" in captured.err
    assert "Traceback" not in captured.err
    assert cycle_root.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "do-not-write\n"


def test_validate_slurm_rejects_unsafe_run_id(tmp_path: Path) -> None:
    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "../escape"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("suffix", ["new-root", "missing/deep"])
def test_validate_slurm_rejects_primary_evidence_root_under_existing_symlink(
    tmp_path: Path,
    suffix: str,
) -> None:
    target_root = tmp_path / "target-root"
    target_root.mkdir()
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(target_root, target_is_directory=True)

    with pytest.raises(slurm_validation.ProductionValidationError) as exc_info:
        slurm_validation.ProductionSlurmConfig.from_env(
            evidence_root=symlink_root / suffix,
            run_id="safe",
            submit=False,
            fake_slurm=True,
        )

    assert exc_info.value.error_code == "PRODUCTION_SLURM_EVIDENCE_SYMLINK"
    assert not (target_root / suffix).exists()


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


def test_validate_slurm_existing_lane_regular_file_reports_stable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    evidence_root = tmp_path / "artifacts"
    lane_path = evidence_root / "file_lane" / "slurm"
    lane_path.parent.mkdir(parents=True)
    lane_path.write_text("not a directory", encoding="utf-8")

    try:
        exit_code = slurm_validation.main(
            ["validate-slurm", "--evidence-root", str(evidence_root), "--run-id", "file_lane", "--fake-slurm"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_SLURM_EVIDENCE_PATH_UNSAFE" in capsys.readouterr().err


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


def test_slurm_evidence_writer_rejects_lane_parent_symlink_swap_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = slurm_validation.EvidenceWriter(
        tmp_path / "artifacts",
        tmp_path / "artifacts" / "swap" / "slurm",
        force=True,
    )
    writer.prepare()
    external = tmp_path / "external"
    external.mkdir()
    original_verify = safe_fs._verify_fd_matches_path
    swapped = False

    def swap_lane_parent(fd: int, path: Path) -> None:
        nonlocal swapped
        if path == writer.lane_dir and not swapped:
            swapped = True
            writer.lane_dir.rmdir()
            writer.lane_dir.symlink_to(external, target_is_directory=True)
        original_verify(fd, path)

    monkeypatch.setattr(safe_fs, "_verify_fd_matches_path", swap_lane_parent)

    with pytest.raises(slurm_validation.ProductionValidationError) as exc_info:
        writer.write_json(writer.lane_dir / "summary.json", {"status": "ready"})

    assert exc_info.value.error_code == "PRODUCTION_SLURM_EVIDENCE_PATH_UNSAFE"
    assert not (external / "summary.json").exists()


def test_slurm_runtime_manifest_rejects_parent_symlink_swap_even_outside_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = slurm_validation.EvidenceWriter(
        tmp_path / "artifacts",
        tmp_path / "artifacts" / "swap" / "slurm",
        force=True,
    )
    writer.prepare()
    workspace = tmp_path / "workspace"
    manifest_parent = workspace / "runs" / "swap_success" / "input"
    manifest_parent.mkdir(parents=True)
    external = tmp_path / "external-runtime"
    external.mkdir()
    original_verify = safe_fs._verify_fd_matches_path
    swapped = False

    def swap_manifest_parent(fd: int, path: Path) -> None:
        nonlocal swapped
        if path == manifest_parent and not swapped:
            swapped = True
            manifest_parent.rmdir()
            manifest_parent.symlink_to(external, target_is_directory=True)
        original_verify(fd, path)

    monkeypatch.setattr(safe_fs, "_verify_fd_matches_path", swap_manifest_parent)

    with pytest.raises(slurm_validation.ProductionValidationError) as exc_info:
        writer.write_runtime_manifest_json(manifest_parent / "manifest.json", {"status": "ready"})

    assert exc_info.value.error_code == "PRODUCTION_SLURM_EVIDENCE_PATH_UNSAFE"
    assert not (external / "manifest.json").exists()


def test_validate_slurm_shared_runtime_cleanup_refuses_parent_symlink_swap_without_external_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    workspace_root = tmp_path / "shared-workspace"
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(shutil_proxy(), "which", lambda command: f"/usr/bin/{command}")
    external = tmp_path / "external-cleanup"
    external_input = external / "input"
    external_input.mkdir(parents=True)
    external_manifest = external_input / "manifest_index.json"
    external_manifest.write_text("external must remain\n", encoding="utf-8")

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="blocked after manifest write")
        return subprocess.CompletedProcess(command, 0, stdout=f"{program} ok\n", stderr="")

    original_unlink_no_follow = safe_fs.unlink_no_follow
    swapped = False

    def swap_cleanup_parent(path: Path, *, containment_root: Path | None = None, missing_ok: bool = False):
        nonlocal swapped
        if path.name == "manifest_index.json" and path.parent.name == "input" and not swapped:
            swapped = True
            run_dir = path.parent.parent
            safe_fs.rmtree_no_follow(run_dir, containment_root=workspace_root)
            run_dir.symlink_to(external, target_is_directory=True)
        return original_unlink_no_follow(path, containment_root=containment_root, missing_ok=missing_ok)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(slurm_validation, "unlink_no_follow", swap_cleanup_parent)

    try:
        exit_code = slurm_validation.main(
            [
                "validate-slurm",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                "cleanupswap",
                "--submit",
            ]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert external_manifest.read_text(encoding="utf-8") == "external must remain\n"
    cleanup = json.loads((tmp_path / "artifacts" / "cleanupswap" / "slurm" / "slurm_accounting.json").read_text())
    assert any(item["status"] == "failed" for item in cleanup["shared_runtime_input_cleanup"])


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

    original_unlink_no_follow = slurm_validation.unlink_no_follow

    def fake_unlink(path: Path, *, containment_root: Path | None = None, missing_ok: bool = False):
        if path.name == "manifest_index.json":
            raise OSError("nfs busy")
        return original_unlink_no_follow(path, containment_root=containment_root, missing_ok=missing_ok)

    def fake_run(command, **kwargs):
        del kwargs
        program = Path(command[0]).name
        if program == "sbatch":
            monkeypatch.setattr(slurm_validation, "unlink_no_follow", fake_unlink)
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


def test_terminal_slurm_states_are_all_present_in_slurm_state_map() -> None:
    # The file-cohort task projection reads SLURM_STATE_MAP without a default, so a
    # terminal state this module knows about but the map does not strands the cohort
    # on task_accounting_incomplete.  Pin the containment so the vocabularies cannot
    # drift apart a third time (BOOT_FAIL, then REVOKED/SPECIAL_EXIT).
    assert slurm_validation.TERMINAL_SLURM_STATES <= set(SLURM_STATE_MAP)


@pytest.mark.parametrize("state", ["", "   "])
def test_normalize_slurm_state_treats_empty_state_as_unknown(state: str) -> None:
    assert slurm_validation._normalize_slurm_state(state) == "UNKNOWN"


def test_sacct_evidence_parser_converges_empty_state_to_unknown() -> None:
    # sacct row passes the six-field count check but carries no State: it must fall
    # to the UNKNOWN vocabulary rather than raising IndexError out of the parser.
    records = slurm_validation.parse_sacct_evidence("123_0||1:0|00:01:00|cn04|CPU\n")

    assert records[0]["state"] == "UNKNOWN"
    assert records[0]["error_code"] == "SLURM_JOB_FAILED"


def shutil_proxy():
    return slurm_validation.shutil


# --- Issue #257 / M23-6: SHUD executable preflight blockers ------------------


def _slurm_config_with_solver(monkeypatch, tmp_path: Path, solver: str, *, submit: bool, fake: bool):
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(tmp_path / "shared-workspace"))
    monkeypatch.setenv("SHUD_EXECUTABLE", solver)
    return slurm_validation.ProductionSlurmConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m10_257",
        submit=submit,
        fake_slurm=fake,
    )


@pytest.mark.parametrize("stub", ["/bin/true", "/bin/false", "true", "false", " "])
def test_preflight_blockers_reject_stub_solver_in_fake_mode(monkeypatch, tmp_path: Path, stub: str) -> None:
    config = _slurm_config_with_solver(monkeypatch, tmp_path, stub, submit=False, fake=True)

    blockers = slurm_validation._preflight_blockers(config)

    codes = {blocker["error_code"] for blocker in blockers}
    assert codes & {"SHUD_EXECUTABLE_STUB_REJECTED", "SHUD_EXECUTABLE_NOT_CONFIGURED"}
    assert "secret" not in json.dumps(blockers)


def test_preflight_blockers_allow_default_solver_in_fake_mode(monkeypatch, tmp_path: Path) -> None:
    config = _slurm_config_with_solver(monkeypatch, tmp_path, "shud_omp", submit=False, fake=True)

    blockers = slurm_validation._preflight_blockers(config)

    shud_codes = {b["error_code"] for b in blockers if b["error_code"].startswith("SHUD_EXECUTABLE")}
    assert shud_codes == set()


def test_validate_slurm_blocks_stub_solver_without_success_evidence(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_CLUSTER", "shudhpc")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_ACCOUNT", "friends")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_PARTITION", "CPU")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", "s3://bucket/models/qhh/package")
    monkeypatch.setenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", str(tmp_path / "shared-workspace"))
    monkeypatch.setenv("SHUD_EXECUTABLE", "/bin/true")

    exit_code = slurm_validation.main(
        ["validate-slurm", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "stubrun", "--fake-slurm"]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "blocked"
    assert summary["live_slurm_executed"] is False
    assert "SHUD_EXECUTABLE_STUB_REJECTED" in {b["error_code"] for b in summary["blockers"]}


def test_preflight_blockers_reject_missing_library_for_live_submit(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "shud_omp"
    binary.write_text('#!/bin/sh\necho "SHUD"\n', encoding="utf-8")
    binary.chmod(0o755)
    config = _slurm_config_with_solver(monkeypatch, tmp_path, str(binary), submit=True, fake=False)

    import packages.common.shud_preflight as preflight

    monkeypatch.setattr(preflight, "_missing_shared_libraries", lambda _resolved: ["libsecret-token.so.1"])
    monkeypatch.setattr(preflight, "_version_identity_signal", lambda _resolved: "present")

    blockers = slurm_validation._preflight_blockers(config)

    library_blockers = [b for b in blockers if b["error_code"] == "SHUD_EXECUTABLE_LIBRARY_MISSING"]
    assert library_blockers
    assert library_blockers[0]["library"] == "libsecret-token.so.1"
    assert "password=" not in json.dumps(blockers)


def test_preflight_blockers_pass_valid_solver_for_live_submit(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "shud_omp"
    binary.write_text('#!/bin/sh\necho "SHUD v1"\n', encoding="utf-8")
    binary.chmod(0o755)
    config = _slurm_config_with_solver(monkeypatch, tmp_path, str(binary), submit=True, fake=False)

    import packages.common.shud_preflight as preflight

    monkeypatch.setattr(preflight, "_missing_shared_libraries", lambda _resolved: [])
    monkeypatch.setattr(preflight, "_version_identity_signal", lambda _resolved: "present")

    blockers = slurm_validation._preflight_blockers(config)

    shud_codes = {b["error_code"] for b in blockers if b["error_code"].startswith("SHUD_EXECUTABLE")}
    assert shud_codes == set()
