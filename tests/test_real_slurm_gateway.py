from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from jinja2.exceptions import SecurityError
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.orchestrator.chain import ANALYSIS_STAGES, M3_STAGES
from services.orchestrator.persistence import Base
from services.orchestrator.retry import NON_TRANSIENT_ERROR_CODES, TRANSIENT_ERROR_CODES
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings
from services.slurm_gateway.gateway import (
    ManifestValidationError,
    SlurmGatewayError,
    SlurmParseError,
    SlurmTimeoutError,
    TemplateNotFoundError,
    TemplateSecurityError,
    create_gateway,
)
from services.slurm_gateway.models import SlurmJobRecord, SlurmJobStatus, SubmitJobRequest
from services.slurm_gateway.real_backend import RealSlurmGateway
from workers.flood_frequency.config import HindcastConfig
from workers.flood_frequency.hindcast import submit_hindcast_slurm


def _assert_slurm_state_error_code(monkeypatch, tmp_path: Path, slurm_state: str, expected_error_code: str) -> None:
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        stdout = f"12345|{slurm_state}|1:0|2026-05-08T12:00:00|2026-05-08T12:05:00\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.get_job_status("12345")

    assert record.status == SlurmJobStatus.FAILED
    assert record.error_code == expected_error_code
    assert record.manifest["slurm_raw_state"] == slurm_state


def _write_resource_profiles(tmp_path: Path) -> Path:
    path = tmp_path / "resource_profiles.yaml"
    path.write_text(
        """
resource_profiles:
  default:
    partition: compute
    nodes: 1
    ntasks: 1
    cpus_per_task: 32
    memory_gb: 128
    walltime: "06:00:00"
    max_concurrent: 4
    shud_threads: 32
  overrides: {}
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _write_template(tmp_path: Path, name: str = "run.sbatch", content: str | None = None) -> Path:
    template_dir = tmp_path / "sbatch"
    template_dir.mkdir()
    (template_dir / name).write_text(
        content
        or """
#!/usr/bin/env bash
#SBATCH --partition={{partition}}
#SBATCH --cpus-per-task={{cpus_per_task}}
echo "{{run_id}} {{model_id}} {{shud_threads}}"
""".lstrip(),
        encoding="utf-8",
    )
    return template_dir


def _gateway(tmp_path: Path, template_name: str = "run.sbatch") -> RealSlurmGateway:
    return RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(_write_template(tmp_path, template_name)),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"run_shud_forecast_array": template_name},
            workspace_dir=str(tmp_path / "workspace"),
        )
    )


def test_real_slurm_gateway_fake_binaries_cover_command_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "slurm-commands.jsonl"
    _write_fake_slurm_binary(bin_dir / "sbatch", command_log)
    _write_fake_slurm_binary(bin_dir / "sacct", command_log)
    _write_fake_slurm_binary(bin_dir / "scancel", command_log)
    _write_fake_slurm_binary(bin_dir / "sinfo", command_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    gateway = _gateway(tmp_path)
    request = SubmitJobRequest(
        run_id="run_001",
        model_id="model_001",
        job_type="run_shud_forecast_array",
        manifest={"run_id": "run_001", "model_id": "model_001", "job_type": "run_shud_forecast_array"},
    )
    submitted = gateway.submit_job(request)
    assert submitted.job_id == "12345"

    array = gateway.submit_job_array(
        job_type="run_shud_forecast_array",
        cycle_id="cycle_001",
        stage_name="forecast",
        tasks=[
            _fake_array_task("run_001", "model_001"),
            _fake_array_task("run_002", "model_002"),
        ],
    )
    assert array.job_id == "12345"
    assert array.manifest["array_task_count"] == 2
    assert array.manifest["max_concurrent"] == 2

    status = gateway.get_job_status("12345")
    assert status.status == SlurmJobStatus.SUCCEEDED
    tasks = gateway.get_array_task_results("12345")
    assert tasks == [
        {"task_id": 0, "job_id": "12345_0", "state": "COMPLETED", "exit_code": 0},
        {"task_id": 1, "job_id": "12345_1", "state": "FAILED", "exit_code": 1},
    ]
    jobs = gateway.list_jobs(limit=10, offset=0)
    assert [job.job_id for job in jobs] == ["12345", "12346"]
    assert gateway.health().status == "healthy"
    cancelled = gateway.cancel_job("12345")
    assert cancelled.status == SlurmJobStatus.CANCELLED

    log_dir = tmp_path / "workspace" / "run_001" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "12345.out").write_text("master stdout\n", encoding="utf-8")
    (log_dir / "12345_0.out").write_text("task zero stdout\n", encoding="utf-8")
    (log_dir / "12345_1.err").write_text("task one stderr\n", encoding="utf-8")
    logs = gateway.fetch_logs("12345")
    assert logs.logs == "master stdout\n"
    assert logs.array_task_logs is not None
    assert logs.array_task_logs[0]["stdout"] == "task zero stdout\n"
    assert logs.array_task_logs[1]["stderr"] == "task one stderr\n"

    command_records = [
        json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert {record["program"] for record in command_records} == {"sbatch", "sacct", "sinfo", "scancel"}
    assert all(isinstance(arg, str) and "\n" not in arg for record in command_records for arg in record["argv"])
    assert any("--array=0-1%2" in record["argv"] for record in command_records if record["program"] == "sbatch")


def _write_fake_slurm_binary(path: Path, command_log: Path) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

program = Path(sys.argv[0]).name
argv = sys.argv[1:]
Path({str(command_log)!r}).open("a", encoding="utf-8").write(json.dumps({{"program": program, "argv": argv}}) + "\\n")
if program == "sbatch":
    print("Submitted batch job 12345")
elif program == "sacct" and "--format=JobID,State,ExitCode,Start,End" in argv:
    print("12345|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00")
    print("12345_0|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00")
    print("12345_1|FAILED|1:0|2026-05-08T12:00:00|2026-05-08T12:04:00")
elif program == "sacct" and "--format=JobID,State,ExitCode" in argv:
    print("12345|COMPLETED|0:0")
    print("12345_0|COMPLETED|0:0")
    print("12345_1|FAILED|1:0")
elif program == "sacct":
    print("12345|run_001|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00")
    print("12346|run_002|RUNNING|0:0|2026-05-08T12:00:00|")
elif program == "sinfo":
    print("slurm 24.05.0")
elif program == "scancel":
    sys.exit(0)
else:
    sys.exit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _fake_array_task(run_id: str, model_id: str) -> dict[str, str]:
    return {
        "run_id": run_id,
        "model_id": model_id,
        "basin_version_id": "basin_v1",
        "river_network_version_id": "rnv_v1",
        "source_id": "GFS",
        "cycle_time": "2026-05-08T12:00:00Z",
    }


def _production_gateway(tmp_path: Path) -> RealSlurmGateway:
    return RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )


def _production_manifest(tmp_path: Path, job_type: str) -> dict[str, object]:
    analysis_job_types = {
        "run_shud_analysis",
        "parse_analysis_output",
        "save_state_snapshot",
    }
    return {
        "run_id": "run_001",
        "model_id": "model_001",
        "basin_id": "basin_001",
        "basin_version_id": "basin_001",
        "river_network_version_id": "river_001",
        "job_type": job_type,
        "stage": job_type,
        "stage_name": job_type,
        "cycle_id": "cycle_001",
        "source_id": "ERA5" if job_type.startswith("analysis") or job_type in analysis_job_types else "GFS",
        "cycle_time": "2026-05-12T00:00:00Z",
        "start_time": "2026-05-12T00:00:00Z",
        "end_time": "2026-05-13T00:00:00Z",
        "segment_count": 2,
        "model_package_uri": "models/model_001/package",
        "forcing_version_id": "forcing_001",
        "forcing_package_uri": "forcing/package",
        "run_manifest_uri": "runs/run_001/input/manifest.json",
        "output_uri": "runs/run_001/output/",
        "log_uri": "runs/run_001/logs/",
        "workspace_dir": str(tmp_path / "workspace"),
        "object_store_root": str(tmp_path / "object-store"),
        "object_store_prefix": "prod",
        "year": 1993,
        "manifest_index_path": str(tmp_path / "manifest_index.json"),
    }


def _hindcast_store() -> Session:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _attach_hindcast_schemas(engine)
    session = Session(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE core.model_instance (
                    model_id TEXT PRIMARY KEY,
                    basin_version_id TEXT NOT NULL,
                    river_network_version_id TEXT NOT NULL,
                    model_package_uri TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE met.forcing_version (
                    forcing_version_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    cycle_time DATETIME,
                    start_time DATETIME NOT NULL,
                    end_time DATETIME NOT NULL,
                    station_count INTEGER NOT NULL,
                    forcing_package_uri TEXT,
                    checksum TEXT,
                    lineage_json TEXT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO core.model_instance (
                    model_id, basin_version_id, river_network_version_id, model_package_uri
                )
                VALUES ('yangtze_shud_v12', 'basin_v1', 'rnv_v1', 'object://models/yangtze')
                """
            )
        )
    return session


def _attach_hindcast_schemas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS core")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS met")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")


def _insert_hindcast_forcing_version(session: Session, year: int, forcing_package_uri: str) -> None:
    session.execute(
        text(
            """
            INSERT INTO met.forcing_version (
                forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
                station_count, forcing_package_uri, checksum, lineage_json
            )
            VALUES (
                :forcing_version_id, 'yangtze_shud_v12', 'ERA5', :start_time, :start_time, :end_time,
                1, :forcing_package_uri, 'abc', '{}'
            )
            """
        ),
        {
            "forcing_version_id": f"forc_era5_hindcast_yangtze_shud_v12_{year}",
            "start_time": datetime(year, 1, 1, tzinfo=UTC),
            "end_time": datetime(year + 1, 1, 1, tzinfo=UTC),
            "forcing_package_uri": forcing_package_uri,
        },
    )
    session.commit()


def test_submit_job_parses_sbatch_stdout(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job(
        SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="run_shud_forecast_array")
    )

    assert record.job_id == "12345"
    assert record.status == SlurmJobStatus.SUBMITTED
    assert calls[0][0][0] == "sbatch"
    assert calls[0][1]["shell"] is False


@pytest.mark.parametrize("stage", ANALYSIS_STAGES)
def test_analysis_production_templates_submit_without_script_payload(monkeypatch, tmp_path, stage) -> None:
    gateway = _production_gateway(tmp_path)
    captured: dict[str, str] = {}

    def fake_run(command, **kwargs):
        del kwargs
        captured["script"] = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job(
        SubmitJobRequest(
            run_id="run_001",
            model_id="model_001",
            job_type=stage.job_type,
            manifest={**_production_manifest(tmp_path, stage.job_type), "script": "echo ignored"},
        )
    )

    assert record.job_id == "12345"
    assert record.manifest["script"] == "echo ignored"
    assert "echo ignored" not in captured["script"]
    assert f'export NHMS_JOB_TYPE="{stage.job_type}"' in captured["script"]


def test_analysis_download_template_uses_configured_area(monkeypatch, tmp_path) -> None:
    gateway = _production_gateway(tmp_path)
    captured: dict[str, str] = {}

    def fake_run(command, **kwargs):
        del kwargs
        captured["script"] = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    manifest = {
        **_production_manifest(tmp_path, "analysis_download_source_cycle"),
        "analysis_date": "2026-05-12",
        "analysis_start_time": "2026-05-12T00:00:00Z",
        "analysis_end_time": "2026-05-13T00:00:00Z",
        "analysis_date_range": "2026-05-12T00:00:00Z/2026-05-13T00:00:00Z",
        "era5_area": "45,80,5,135",
    }
    gateway.submit_job(
        SubmitJobRequest(
            run_id="run_001",
            model_id="model_001",
            job_type="analysis_download_source_cycle",
            manifest=manifest,
        )
    )

    assert 'nhms-era5 download --date "2026-05-12" --area "45,80,5,135"' in captured["script"]


def test_production_mapping_file_defaults_and_templates_are_complete() -> None:
    template_dir = Path("infra/sbatch")
    production_job_types = {stage.job_type for stage in (*M3_STAGES, *ANALYSIS_STAGES)} | {"hindcast"}

    assert SlurmGatewaySettings().job_type_templates == DEFAULT_JOB_TYPE_TEMPLATES
    assert production_job_types.issubset(DEFAULT_JOB_TYPE_TEMPLATES)
    assert all((template_dir / DEFAULT_JOB_TYPE_TEMPLATES[job_type]).is_file() for job_type in production_job_types)


def test_job_type_template_mapping_file_matches_defaults_and_templates_exist() -> None:
    template_dir = Path("infra/sbatch")
    production_job_types = {stage.job_type for stage in (*M3_STAGES, *ANALYSIS_STAGES)} | {"hindcast"}
    mapping = yaml.safe_load(Path("config/job_type_templates.yaml").read_text(encoding="utf-8"))

    assert mapping["job_type_templates"] == DEFAULT_JOB_TYPE_TEMPLATES
    assert production_job_types.issubset(mapping["job_type_templates"])
    assert all((template_dir / mapping["job_type_templates"][job_type]).is_file() for job_type in production_job_types)


def test_submit_job_accepts_nested_model_id_and_script_manifest(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        return subprocess.CompletedProcess([], 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job(
        SubmitJobRequest(
            manifest={
                "run_id": "run_001",
                "stage": "run_shud_forecast_array",
                "model": {"model_id": "model_001"},
                "script": "#!/usr/bin/env bash\necho ok\n",
            }
        )
    )

    assert record.job_id == "12345"
    assert record.model_id == "model_001"
    assert record.manifest["model"]["model_id"] == "model_001"


@pytest.mark.parametrize(
    ("slurm_state", "expected"),
    [
        ("PENDING", SlurmJobStatus.SUBMITTED),
        ("REQUEUED", SlurmJobStatus.SUBMITTED),
        ("RUNNING", SlurmJobStatus.RUNNING),
        ("COMPLETED", SlurmJobStatus.SUCCEEDED),
        ("FAILED", SlurmJobStatus.FAILED),
        ("TIMEOUT", SlurmJobStatus.FAILED),
        ("NODE_FAIL", SlurmJobStatus.FAILED),
        ("OUT_OF_MEMORY", SlurmJobStatus.FAILED),
        ("CANCELLED", SlurmJobStatus.CANCELLED),
    ],
)
def test_sacct_state_parsing(monkeypatch, tmp_path, slurm_state, expected):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        stdout = (
            "12345.batch|FAILED|1:0|2026-05-08T12:00:00|2026-05-08T12:01:00\n"
            f"12345|{slurm_state}|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gateway.get_job_status("12345").status == expected


@pytest.mark.parametrize(
    ("slurm_state", "expected_error_code"),
    [
        ("TIMEOUT", "SLURM_TIMEOUT"),
        ("NODE_FAIL", "NODE_FAILURE"),
        ("PREEMPTED", "NODE_FAILURE"),
        ("OUT_OF_MEMORY", "OUT_OF_MEMORY"),
        ("BOOT_FAIL", "SLURM_JOB_FAILED"),
    ],
)
def test_failed_sacct_states_produce_stable_error_codes(
    monkeypatch,
    tmp_path,
    slurm_state: str,
    expected_error_code: str,
) -> None:
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        stdout = f"12345|{slurm_state}|1:0|2026-05-08T12:00:00|2026-05-08T12:05:00\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.get_job_status("12345")

    assert record.status == SlurmJobStatus.FAILED
    assert record.error_code == expected_error_code
    assert record.manifest["slurm_raw_state"] == slurm_state


def test_timeout_produces_slurm_timeout_error_code(monkeypatch, tmp_path) -> None:
    _assert_slurm_state_error_code(monkeypatch, tmp_path, "TIMEOUT", "SLURM_TIMEOUT")


def test_node_fail_produces_node_failure_error_code(monkeypatch, tmp_path) -> None:
    _assert_slurm_state_error_code(monkeypatch, tmp_path, "NODE_FAIL", "NODE_FAILURE")


def test_preempted_produces_node_failure_error_code(monkeypatch, tmp_path) -> None:
    _assert_slurm_state_error_code(monkeypatch, tmp_path, "PREEMPTED", "NODE_FAILURE")


def test_out_of_memory_produces_out_of_memory_error_code(monkeypatch, tmp_path) -> None:
    _assert_slurm_state_error_code(monkeypatch, tmp_path, "OUT_OF_MEMORY", "OUT_OF_MEMORY")


def test_unknown_terminal_produces_slurm_job_failed_error_code(monkeypatch, tmp_path) -> None:
    _assert_slurm_state_error_code(monkeypatch, tmp_path, "BOOT_FAIL", "SLURM_JOB_FAILED")


def test_cancelled_state_does_not_produce_error_code(monkeypatch, tmp_path) -> None:
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        stdout = "12345|CANCELLED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.get_job_status("12345")

    assert record.status == SlurmJobStatus.CANCELLED
    assert record.error_code is None
    assert record.manifest["slurm_raw_state"] == "CANCELLED"


def test_slurm_error_codes_align_with_retry_sets() -> None:
    assert "SLURM_TIMEOUT" in TRANSIENT_ERROR_CODES
    assert "NODE_FAILURE" in TRANSIENT_ERROR_CODES
    assert "OUT_OF_MEMORY" in NON_TRANSIENT_ERROR_CODES
    assert "SLURM_JOB_FAILED" not in TRANSIENT_ERROR_CODES
    assert "SLURM_JOB_FAILED" not in NON_TRANSIENT_ERROR_CODES


def test_array_task_results_parse_task_lines_only(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        stdout = "\n".join(
            [
                "12345|COMPLETED|0:0",
                "12345.batch|COMPLETED|0:0",
                "12345_0|COMPLETED|0:0",
                "12345_1|FAILED|1:0",
                "12345_1.batch|FAILED|1:0",
                "12345.extern|COMPLETED|0:0",
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gateway.get_array_task_results("12345") == [
        {"task_id": 0, "job_id": "12345_0", "state": "COMPLETED", "exit_code": 0},
        {"task_id": 1, "job_id": "12345_1", "state": "FAILED", "exit_code": 1},
    ]
    assert "--format=JobID,State,ExitCode" in calls[0]
    assert "--jobs=12345" in calls[0]


def test_scancel_invocation(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.cancel_job("12345")

    assert calls == [["scancel", "12345"]]
    assert record.status == SlurmJobStatus.CANCELLED


@pytest.mark.parametrize("operation", ["cancel_job", "get_job_status"])
def test_job_id_option_injection_rejected(monkeypatch, tmp_path, operation):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid job_id")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmGatewayError) as exc_info:
        getattr(gateway, operation)("--user=root")

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "INVALID_JOB_ID"


def test_template_whitelist_rejects_path_traversal(tmp_path):
    template_dir = _write_template(tmp_path)
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"bad": "../bad.sbatch"},
        )
    )

    with pytest.raises(TemplateSecurityError):
        gateway.render_template("bad", {"run_id": "run_001", "model_id": "model_001", "job_type": "bad"})


def test_manifest_injection_rejected(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid manifests")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError):
        gateway.submit_job(
            SubmitJobRequest(run_id="run_001;rm", model_id="model_001", job_type="run_shud_forecast_array")
        )


def test_unsupported_legacy_job_type_rejected_before_submission(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for unsupported job_type")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TemplateNotFoundError):
        gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="legacy_unsupported"))


def test_hindcast_single_job_type_resolves_configured_template(monkeypatch, tmp_path):
    template_dir = _write_template(tmp_path, name="hindcast.sbatch")
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"hindcast": "hindcast.sbatch"},
            workspace_dir=str(tmp_path / "workspace"),
        )
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="hindcast"))

    assert record.job_id == "12345"
    assert record.manifest["job_type"] == "hindcast"
    assert calls[0][0] == "sbatch"


def test_submit_hindcast_slurm_payload_writes_real_forcing_manifest_index(monkeypatch, tmp_path: Path) -> None:
    with _hindcast_store() as session:
        _insert_hindcast_forcing_version(session, 1993, "object://forcing/package/1993")
        gateway = _production_gateway(tmp_path)

        def fake_run(command, **kwargs):
            del kwargs
            assert Path(command[0]).name == "sbatch"
            return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = submit_hindcast_slurm(
            "yangtze_shud_v12",
            "ERA5",
            [1993],
            HindcastConfig(
                workspace_root=tmp_path / "workspace",
                object_store_root=tmp_path / "object-store",
                object_store_prefix="hindcast/prod",
                db_session=session,
                slurm_client=gateway,
            ),
        )

    record = gateway._jobs[result.slurm_job_array_id or ""]
    manifest_index = json.loads(Path(record.manifest["manifest_index_path"]).read_text(encoding="utf-8"))

    assert result.slurm_job_array_id == "12345"
    assert manifest_index[0]["forcing_version_id"] == "forc_era5_hindcast_yangtze_shud_v12_1993"
    assert manifest_index[0]["forcing_package_uri"] == "object://forcing/package/1993"


def test_sandboxed_environment_restricts_template_access(tmp_path):
    template_dir = _write_template(
        tmp_path,
        content="{{ cycler.__init__.__globals__.os.system('echo unsafe') }}",
    )
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"run_shud_forecast_array": "run.sbatch"},
        )
    )

    with pytest.raises(SecurityError):
        gateway.render_template(
            "run_shud_forecast_array",
            {"run_id": "run_001", "model_id": "model_001", "job_type": "run_shud_forecast_array"},
        )


def test_subprocess_timeout_handling(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmTimeoutError):
        gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="run_shud_forecast_array"))


def test_factory_returns_real_gateway_for_slurm_backend(tmp_path):
    gateway = create_gateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(_write_template(tmp_path)),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
        )
    )

    assert isinstance(gateway, RealSlurmGateway)


def test_health_check_success_and_failure(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def success(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 0, stdout="slurm 24.05.1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", success)
    assert gateway.health().model_dump() == {
        "backend": "slurm",
        "version": "slurm 24.05.1",
        "status": "healthy",
        "error": None,
    }

    def failure(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing sinfo")

    monkeypatch.setattr(subprocess, "run", failure)
    response = gateway.health()
    assert response.backend == "slurm"
    assert response.status == "unhealthy"
    assert response.version == ""
    assert response.error


def test_fetch_logs_refuses_symlink(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"))
    now = datetime.now(UTC)
    gateway._jobs["12345"] = SlurmJobRecord(
        job_id="12345",
        run_id="run_001",
        model_id="model_001",
        status=SlurmJobStatus.SUCCEEDED,
        submitted_at=now,
        updated_at=now,
        finished_at=now,
    )
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("secret", encoding="utf-8")
    log_dir = tmp_path / "workspace" / "run_001" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "12345.out").symlink_to(secret_path)

    response = gateway.fetch_logs("12345")

    assert response.logs == ""
    assert response.truncated is False


def test_fetch_logs_after_restart_uses_durable_workspace_path(monkeypatch, tmp_path) -> None:
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        return subprocess.CompletedProcess([], 1, stdout="", stderr="sacct unavailable")

    monkeypatch.setattr(subprocess, "run", fake_run)
    now = datetime.now(UTC)
    gateway._jobs["12345"] = SlurmJobRecord(
        job_id="12345",
        run_id="run_001",
        model_id="model_001",
        status=SlurmJobStatus.SUCCEEDED,
        submitted_at=now,
        updated_at=now,
        finished_at=now,
    )
    log_dir = tmp_path / "workspace" / "run_001" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "12345.out").write_text("durable stdout", encoding="utf-8")

    gateway._jobs.clear()
    response = gateway.fetch_logs("12345")

    assert response.run_id == "12345"
    assert response.logs == "durable stdout"
    assert response.metadata_complete is False


def test_fetch_logs_restart_without_record_reports_incomplete_metadata(monkeypatch, tmp_path) -> None:
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        return subprocess.CompletedProcess([], 1, stdout="", stderr="sacct unavailable")

    monkeypatch.setattr(subprocess, "run", fake_run)
    log_dir = tmp_path / "workspace" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "12345.out").write_text("root stdout", encoding="utf-8")

    response = gateway.fetch_logs("12345")

    assert response.logs == "root stdout"
    assert response.metadata_complete is False


def test_array_master_log_aggregates_task_logs(monkeypatch, tmp_path) -> None:
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"))
    now = datetime.now(UTC)
    gateway._jobs["12345"] = SlurmJobRecord(
        job_id="12345",
        run_id="run_001",
        model_id="model_001",
        status=SlurmJobStatus.SUCCEEDED,
        submitted_at=now,
        updated_at=now,
        finished_at=now,
    )
    log_dir = tmp_path / "workspace" / "run_001" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "12345_0.out").write_text("task 0 stdout", encoding="utf-8")
    (log_dir / "12345_0.err").write_text("task 0 stderr", encoding="utf-8")
    (log_dir / "12345_1.out").write_text("task 1 stdout", encoding="utf-8")
    (log_dir / "12345_1.err").write_text("task 1 stderr", encoding="utf-8")

    response = gateway.fetch_logs("12345")

    assert response.array_task_logs == [
        {
            "task_id": 0,
            "stdout": "task 0 stdout",
            "stderr": "task 0 stderr",
            "truncated": False,
            "missing_stdout": False,
            "missing_stderr": False,
        },
        {
            "task_id": 1,
            "stdout": "task 1 stdout",
            "stderr": "task 1 stderr",
            "truncated": False,
            "missing_stdout": False,
            "missing_stderr": False,
        },
    ]


def test_missing_task_log_does_not_discard_existing(monkeypatch, tmp_path) -> None:
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"))
    now = datetime.now(UTC)
    gateway._jobs["12345"] = SlurmJobRecord(
        job_id="12345",
        run_id="run_001",
        model_id="model_001",
        status=SlurmJobStatus.FAILED,
        submitted_at=now,
        updated_at=now,
        finished_at=now,
    )
    log_dir = tmp_path / "workspace" / "run_001" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "12345_0.out").write_text("task 0 stdout", encoding="utf-8")
    (log_dir / "12345_1.err").write_text("task 1 stderr", encoding="utf-8")

    response = gateway.fetch_logs("12345")

    assert response.array_task_logs == [
        {
            "task_id": 0,
            "stdout": "task 0 stdout",
            "stderr": "",
            "truncated": False,
            "missing_stdout": False,
            "missing_stderr": True,
        },
        {
            "task_id": 1,
            "stdout": "",
            "stderr": "task 1 stderr",
            "truncated": False,
            "missing_stdout": True,
            "missing_stderr": False,
        },
    ]


def test_parse_slurm_datetime_rejects_garbage(tmp_path):
    gateway = _gateway(tmp_path)

    with pytest.raises(SlurmParseError):
        gateway._parse_slurm_datetime("definitely-not-a-time")


def test_list_jobs_defaults_to_lookback_start_time(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gateway.list_jobs(limit=100, offset=0) == []
    assert any(arg.startswith("--starttime=") for arg in calls[0])


def test_fake_slurm_command_matrix_for_production_job_types(monkeypatch, tmp_path) -> None:
    gateway = _production_gateway(tmp_path)
    commands: list[list[str]] = []
    submitted_job_ids = iter(("12345", "12346"))

    def fake_run(command, **kwargs):
        del kwargs
        commands.append(command)
        executable = Path(command[0]).name
        if executable == "sbatch":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"Submitted batch job {next(submitted_job_ids)}\n",
                stderr="",
            )
        if executable == "sacct" and "--format=JobID,State,ExitCode,Start,End" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="12345|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00\n",
                stderr="",
            )
        if executable == "sacct" and "--format=JobID,State,ExitCode" in command:
            requested_job = next(arg.removeprefix("--jobs=") for arg in command if arg.startswith("--jobs="))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{requested_job}_0|COMPLETED|0:0\n{requested_job}_1|FAILED|1:0\n",
                stderr="",
            )
        if executable == "scancel":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if executable == "sinfo":
            return subprocess.CompletedProcess(command, 0, stdout="slurm 24.05.1\n", stderr="")
        raise AssertionError(f"unexpected fake Slurm command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    analysis_record = gateway.submit_job(
        SubmitJobRequest(
            run_id="run_001",
            model_id="model_001",
            job_type="run_shud_analysis",
            manifest=_production_manifest(tmp_path, "run_shud_analysis"),
        )
    )
    hindcast_record = gateway.submit_job(
        SubmitJobRequest(
            run_id="run_002",
            model_id="model_001",
            job_type="hindcast",
            manifest=_production_manifest(tmp_path, "hindcast"),
        )
    )
    status = gateway.get_job_status(analysis_record.job_id)
    tasks = gateway.get_array_task_results(hindcast_record.job_id)
    log_dir = tmp_path / "workspace" / "run_001" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / f"{analysis_record.job_id}.out").write_text("analysis log", encoding="utf-8")
    logs = gateway.fetch_logs(analysis_record.job_id)
    cancelled = gateway.cancel_job(hindcast_record.job_id)
    health = gateway.health()

    assert status.status == SlurmJobStatus.SUCCEEDED
    assert tasks[0]["task_id"] == 0
    assert logs.logs == "analysis log"
    assert cancelled.status == SlurmJobStatus.CANCELLED
    assert health.status == "healthy"
    assert {"sbatch", "sacct", "scancel", "sinfo"}.issubset({Path(command[0]).name for command in commands})
