from __future__ import annotations

import json
import os
import shlex
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
    ConfigurationError,
    ManifestValidationError,
    SlurmGatewayError,
    SlurmJobNotFoundError,
    SlurmParseError,
    SlurmTimeoutError,
    SlurmValidationError,
    TemplateNotFoundError,
    TemplateSecurityError,
    create_gateway,
)
from services.slurm_gateway.mock_backend import MockSlurmGateway
from services.slurm_gateway.models import SlurmJobRecord, SlurmJobStatus, SubmitJobRequest
from services.slurm_gateway.real_backend import LOG_TRUNCATION_MARKER, RealSlurmGateway
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


def _write_resource_profiles_with_update(tmp_path: Path, update: dict[str, object]) -> Path:
    path = _write_resource_profiles(tmp_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["resource_profiles"]["default"].update(update)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
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
            job_type_templates={"run_shud_analysis": template_name, "run_shud_forecast_array": template_name},
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
    _write_fake_slurm_binary(bin_dir / "squeue", command_log)
    _write_fake_slurm_binary(bin_dir / "sacct", command_log)
    _write_fake_slurm_binary(bin_dir / "scancel", command_log)
    _write_fake_slurm_binary(bin_dir / "sinfo", command_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    gateway = _gateway(tmp_path)
    request = SubmitJobRequest(
        run_id="run_001",
        model_id="model_001",
        job_type="run_shud_analysis",
        manifest={"run_id": "run_001", "model_id": "model_001", "job_type": "run_shud_analysis"},
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
    assert status.status == SlurmJobStatus.CANCELLED
    assert status.elapsed == "00:05:00"
    assert status.max_rss == "1024K"
    assert status.resource_metrics == {
        "elapsed": "00:05:00",
        "max_rss": "1024K",
        "ave_rss": "512K",
        "alloc_tres": "cpu=2,mem=4G",
    }
    tasks = gateway.get_array_task_results("12345")
    assert tasks == [
        {
            "task_id": 0,
            "job_id": "12345_0",
            "state": "COMPLETED",
            "status": "succeeded",
            "exit_code": 0,
            "elapsed": "00:04:30",
            "max_rss": "900K",
            "resource_metrics": {
                "elapsed": "00:04:30",
                "max_rss": "900K",
                "ave_rss": "450K",
                "alloc_tres": "cpu=1,mem=2G",
            },
            "accounting": {
                "elapsed": "00:04:30",
                "max_rss": "900K",
                "ave_rss": "450K",
                "alloc_tres": "cpu=1,mem=2G",
            },
        },
        {
            "task_id": 1,
            "job_id": "12345_1",
            "state": "FAILED",
            "status": "failed",
            "exit_code": 1,
            "elapsed": "00:03:30",
            "max_rss": "800K",
            "resource_metrics": {
                "elapsed": "00:03:30",
                "max_rss": "800K",
                "ave_rss": "400K",
                "alloc_tres": "cpu=1,mem=2G",
            },
            "accounting": {
                "elapsed": "00:03:30",
                "max_rss": "800K",
                "ave_rss": "400K",
                "alloc_tres": "cpu=1,mem=2G",
            },
        },
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
    assert {record["program"] for record in command_records} == {"sbatch", "squeue", "sacct", "scancel"}
    assert all(isinstance(arg, str) and "\n" not in arg for record in command_records for arg in record["argv"])
    assert any("--array=0-1%2" in record["argv"] for record in command_records if record["program"] == "sbatch")


def test_single_task_array_submits_real_array_and_parses_task_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        commands.append(command)
        executable = Path(command[0]).name
        if executable == "sbatch":
            return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")
        if executable == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="12345_0|COMPLETED|0:0|00:01:00|256K|128K|cpu=1,mem=1G\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gateway = _gateway(tmp_path)

    record = gateway.submit_job_array(
        job_type="run_shud_forecast_array",
        cycle_id="cycle_001",
        stage_name="forecast",
        tasks=[_fake_array_task("run_001", "model_001")],
    )
    tasks = gateway.get_array_task_results(record.job_id)

    assert any("--array=0-0%1" in command for command in commands if Path(command[0]).name == "sbatch")
    assert tasks[0]["task_id"] == 0
    assert tasks[0]["status"] == "succeeded"


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
if argv == ["--version"]:
    print("slurm 24.05.0")
    sys.exit(0)
if program == "sbatch":
    print("Submitted batch job 12345")
elif program == "sacct" and "--format=JobID,State,ExitCode,Start,End,Elapsed,MaxRSS,AveRSS,AllocTRES" in argv:
    print("12345|CANCELLED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|00:05:00|1024K|512K|cpu=2,mem=4G")
    print("12345_0|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|00:04:30|900K|450K|cpu=1,mem=2G")
    print("12345_1|FAILED|1:0|2026-05-08T12:00:00|2026-05-08T12:04:00|00:03:30|800K|400K|cpu=1,mem=2G")
elif program == "sacct" and "--format=JobID,State,ExitCode,Start,End" in argv:
    print("12345|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00")
    print("12345_0|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00")
    print("12345_1|FAILED|1:0|2026-05-08T12:00:00|2026-05-08T12:04:00")
elif program == "sacct" and "--format=JobID,State,ExitCode,Elapsed,MaxRSS,AveRSS,AllocTRES" in argv:
    print("12345|COMPLETED|0:0|00:05:00|1024K|512K|cpu=2,mem=4G")
    print("12345_0|COMPLETED|0:0|00:04:30|900K|450K|cpu=1,mem=2G")
    print("12345_1|FAILED|1:0|00:03:30|800K|400K|cpu=1,mem=2G")
elif program == "sacct" and "--format=JobID,State,ExitCode" in argv:
    print("12345|COMPLETED|0:0")
    print("12345_0|COMPLETED|0:0")
    print("12345_1|FAILED|1:0")
elif program == "sacct":
    print("12345|run_001|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00")
    print("12346|run_002|RUNNING|0:0|2026-05-08T12:00:00|")
elif program == "sinfo":
    print("slurm 24.05.0")
elif program == "squeue":
    sys.exit(0)
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
        SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="run_shud_analysis")
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
    assert f"export NHMS_JOB_TYPE={stage.job_type}" in captured["script"]


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
                "stage": "run_shud_analysis",
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
    assert "OUT_OF_MEMORY" in TRANSIENT_ERROR_CODES
    assert "OUT_OF_MEMORY" not in NON_TRANSIENT_ERROR_CODES
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
                "12345|COMPLETED|0:0|00:02:00|1024K|512K|cpu=2,mem=4G",
                "12345.batch|COMPLETED|0:0|00:02:00|1024K|512K|cpu=2,mem=4G",
                "12345_0|COMPLETED|0:0|00:01:00|256K|128K|cpu=1,mem=1G",
                "12345_1|FAILED|1:0|00:01:30|512K|256K|cpu=1,mem=1G",
                "12345_1.batch|FAILED|1:0|00:01:30|512K|256K|cpu=1,mem=1G",
                "12345.extern|COMPLETED|0:0|00:02:00|1024K|512K|cpu=2,mem=4G",
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gateway.get_array_task_results("12345") == [
        {
            "task_id": 0,
            "job_id": "12345_0",
            "state": "COMPLETED",
            "status": "succeeded",
            "exit_code": 0,
            "elapsed": "00:01:00",
            "max_rss": "256K",
            "resource_metrics": {
                "elapsed": "00:01:00",
                "max_rss": "256K",
                "ave_rss": "128K",
                "alloc_tres": "cpu=1,mem=1G",
            },
            "accounting": {
                "elapsed": "00:01:00",
                "max_rss": "256K",
                "ave_rss": "128K",
                "alloc_tres": "cpu=1,mem=1G",
            },
        },
        {
            "task_id": 1,
            "job_id": "12345_1",
            "state": "FAILED",
            "status": "failed",
            "exit_code": 1,
            "elapsed": "00:01:30",
            "max_rss": "512K",
            "resource_metrics": {
                "elapsed": "00:01:30",
                "max_rss": "512K",
                "ave_rss": "256K",
                "alloc_tres": "cpu=1,mem=1G",
            },
            "accounting": {
                "elapsed": "00:01:30",
                "max_rss": "512K",
                "ave_rss": "256K",
                "alloc_tres": "cpu=1,mem=1G",
            },
        },
    ]
    assert "--format=JobID,State,ExitCode,Elapsed,MaxRSS,AveRSS,AllocTRES" in calls[0]
    assert "--jobs=12345" in calls[0]


def _array_status_gateway(monkeypatch, tmp_path, stdout: str) -> RealSlurmGateway:
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return gateway


def test_get_job_status_array_all_completed_aggregates_to_succeeded(monkeypatch, tmp_path):
    stdout = "\n".join(
        [
            "6031_0|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|00:05:00",
            "6031_0.batch|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|00:05:00",
            "6031_1|COMPLETED|0:0|2026-05-08T12:01:00|2026-05-08T12:06:00|00:05:00",
            "6031_1.batch|COMPLETED|0:0|2026-05-08T12:01:00|2026-05-08T12:06:00|00:05:00",
        ]
    )
    gateway = _array_status_gateway(monkeypatch, tmp_path, stdout)
    assert gateway.get_job_status("6031").status == SlurmJobStatus.SUCCEEDED


def test_get_job_status_array_partial_failure_aggregates_to_failed(monkeypatch, tmp_path):
    stdout = "\n".join(
        [
            "6031_0|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|00:05:00",
            "6031_1|FAILED|1:0|2026-05-08T12:01:00|2026-05-08T12:03:00|00:02:00",
        ]
    )
    gateway = _array_status_gateway(monkeypatch, tmp_path, stdout)
    record = gateway.get_job_status("6031")
    assert record.status == SlurmJobStatus.FAILED


def test_get_job_status_array_running_member_keeps_parent_running(monkeypatch, tmp_path):
    stdout = "\n".join(
        [
            "6031_0|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|00:05:00",
            "6031_1|RUNNING|0:0|2026-05-08T12:01:00||",
        ]
    )
    gateway = _array_status_gateway(monkeypatch, tmp_path, stdout)
    assert gateway.get_job_status("6031").status == SlurmJobStatus.RUNNING


def test_get_job_status_array_pending_master_is_submitted(monkeypatch, tmp_path):
    stdout = "6031_[0-1]|PENDING|0:0|||\n"
    gateway = _array_status_gateway(monkeypatch, tmp_path, stdout)
    assert gateway.get_job_status("6031").status == SlurmJobStatus.SUBMITTED


def test_get_job_status_array_all_cancelled_aggregates_to_cancelled(monkeypatch, tmp_path):
    stdout = "\n".join(
        [
            "6031_0|CANCELLED|0:0|2026-05-08T12:00:00|2026-05-08T12:02:00|00:02:00",
            "6031_1|CANCELLED|0:0|2026-05-08T12:00:00|2026-05-08T12:02:00|00:02:00",
        ]
    )
    gateway = _array_status_gateway(monkeypatch, tmp_path, stdout)
    assert gateway.get_job_status("6031").status == SlurmJobStatus.CANCELLED


def test_get_job_status_missing_job_still_raises_not_found(monkeypatch, tmp_path):
    gateway = _array_status_gateway(monkeypatch, tmp_path, "")
    with pytest.raises(SlurmJobNotFoundError):
        gateway.get_job_status("6031")


def test_get_job_status_non_array_exact_match_unchanged(monkeypatch, tmp_path):
    stdout = "\n".join(
        [
            "6029|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|00:05:00",
            "6029.batch|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|00:05:00",
        ]
    )
    gateway = _array_status_gateway(monkeypatch, tmp_path, stdout)
    assert gateway.get_job_status("6029").status == SlurmJobStatus.SUCCEEDED


@pytest.mark.parametrize(
    "manifest_update",
    [
        {"object_store_prefix": "safe;rm"},
        {"object_store_prefix": "x" * 4097},
    ],
)
def test_manifest_export_values_reject_shell_meta_and_unbounded_strings(
    monkeypatch,
    tmp_path,
    manifest_update,
):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for unsafe manifests")

    monkeypatch.setattr(subprocess, "run", fake_run)

    manifest = {"run_id": "run_001", "model_id": "model_001", "job_type": "run_shud_analysis"}
    manifest.update(manifest_update)
    with pytest.raises(ManifestValidationError):
        gateway.submit_job(SubmitJobRequest(**manifest))


def test_manifest_export_quote_breakout_is_shell_quoted_before_sbatch(monkeypatch, tmp_path):
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
            job_type="download_source_cycle",
            manifest={
                **_production_manifest(tmp_path, "download_source_cycle"),
                "object_store_prefix": 'prod" PYTHONPATH=/tmp/evil #',
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
            },
        )
    )

    assert record.job_id == "12345"
    assert "PYTHONPATH=/tmp/evil #" in captured["script"]
    assert 'export OBJECT_STORE_PREFIX="prod" PYTHONPATH=/tmp/evil #' not in captured["script"]
    assert 'export OBJECT_STORE_PREFIX=\'prod" PYTHONPATH=/tmp/evil #\'' in captured["script"]


def test_submit_job_rejects_secret_quote_breakout_before_sbatch(monkeypatch, tmp_path):
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret breakout values")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="download_source_cycle",
                manifest={
                    **_production_manifest(tmp_path, "download_source_cycle"),
                    "object_store_prefix": 'prod" SECRET_TOKEN=supersecret #',
                    "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                },
            )
        )

    details = json.dumps(exc_info.value.details)
    assert "supersecret" not in details
    assert "SECRET_TOKEN" not in details


@pytest.mark.parametrize(
    "resource_update",
    [
        {"partition": "compute --account=vip"},
        {"partition": "-compute"},
        {"partition": 'compute"breakout'},
        {"partition": "compute#debug"},
        {"partition": "compute\\debug"},
        {"account": "friends --qos=high"},
        {"nodes": "1 --exclusive"},
        {"nodes": 0},
        {"nodes": 129},
        {"ntasks": "1 --exclusive"},
        {"ntasks": 0},
        {"ntasks": 4097},
        {"cpus_per_task": "1 --hint=nomultithread"},
        {"cpus_per_task": 0},
        {"cpus_per_task": 257},
        {"memory_gb": "8 --mem-per-cpu=8G"},
        {"memory_gb": 0},
        {"memory_gb": 4097},
        {"walltime": "01:00:00 --qos=high"},
        {"walltime": "01:61:00"},
        {"walltime": '01:00:00"'},
        {"walltime": "31-00:00:00"},
        {"walltime": "00:00:00"},
        {"max_concurrent": "2 --array=0-999"},
        {"max_concurrent": 0},
        {"max_concurrent": 10001},
        {"shud_threads": "8 --export=ALL"},
        {"shud_threads": 0},
        {"shud_threads": 257},
    ],
)
def test_resource_profile_directive_values_reject_injection_before_sbatch(
    monkeypatch,
    tmp_path,
    resource_update,
):
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles_with_update(tmp_path, resource_update)),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid resource profiles")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ConfigurationError) as exc_info:
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="download_source_cycle",
                manifest={
                    **_production_manifest(tmp_path, "download_source_cycle"),
                    "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                },
            )
        )

    details = json.dumps(exc_info.value.details)
    assert "--" not in details
    assert "exclusive" not in details
    assert "breakout" not in details


@pytest.mark.parametrize(
    "collision_key",
    [
        "run_id",
        "workspace_dir",
        "stage_name",
        "cycle_id",
        "object_store_root",
        "object_store_prefix",
        "manifest_index_path",
        "custom_metadata",
    ],
)
def test_resource_profile_closed_schema_rejects_manifest_context_collision_before_sbatch(
    monkeypatch,
    tmp_path,
    collision_key,
):
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles_with_update(tmp_path, {collision_key: "override"})),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid resource profiles")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ConfigurationError) as exc_info:
        gateway.render_template(
            "run_shud_forecast_array",
            {
                **_production_manifest(tmp_path, "run_shud_forecast_array"),
                "run_id": "manifest_run",
                "stage_name": "forecast",
                "cycle_id": "cycle_001",
                "workspace_dir": str(tmp_path / "workspace"),
            },
            str(tmp_path / "workspace" / "cycle_001" / "manifests" / "index.json"),
        )

    assert exc_info.value.details["reason"] == "unsupported_resource_profile_fields"
    assert collision_key in exc_info.value.details["unsupported_fields"]


def test_resource_profile_context_collision_rejects_array_submit_before_manifest_index_or_sbatch(
    monkeypatch,
    tmp_path,
):
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles_with_update(tmp_path, {"run_id": "profile_run"})),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid resource profiles")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ConfigurationError) as exc_info:
        gateway.submit_job_array(
            job_type="run_shud_forecast_array",
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=[{**_fake_array_task("run_001", "model_001"), "workspace_dir": str(tmp_path / "workspace")}],
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
            },
        )

    assert exc_info.value.details["reason"] == "unsupported_resource_profile_fields"
    assert "run_id" in exc_info.value.details["unsupported_fields"]
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


def test_safe_resource_profile_renders_manifest_identity_unchanged(monkeypatch, tmp_path):
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(
                _write_resource_profiles_with_update(
                    tmp_path,
                    {"partition": "compute-gpu.1", "account": "friends.team-1", "walltime": "2-01:00:00"},
                )
            ),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )
    captured: dict[str, str] = {}

    def fake_run(command, **kwargs):
        del kwargs
        captured["script"] = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job_array(
        job_type="run_shud_forecast_array",
        cycle_id="cycle_manifest",
        stage_name="forecast",
        tasks=[{**_fake_array_task("run_manifest", "model_001"), "workspace_dir": str(tmp_path / "workspace")}],
        manifest={
            "run_id": "cycle_manifest",
            "model_id": "model_001",
            "workspace_dir": str(tmp_path / "workspace"),
            "object_store_root": "/durable/object-store",
            "object_store_prefix": "prod/gfs",
            "published_artifact_root": "/published",
            "published_artifact_uri_prefix": "published://",
            "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
        },
    )

    assert record.run_id == "run_manifest"
    assert "#SBATCH --job-name=nhms_forecast" in captured["script"]
    assert f"#SBATCH --output={tmp_path / 'workspace'}/run_manifest/logs/%A_%a.out" in captured["script"]
    assert "export WORKSPACE_ROOT=" + shlex.quote(str(tmp_path / "workspace")) in captured["script"]
    assert "export NHMS_RUN_ID=run_manifest" in captured["script"]
    assert "export NHMS_CYCLE_ID=cycle_manifest" in captured["script"]
    assert "export OBJECT_STORE_ROOT=/durable/object-store" in captured["script"]
    assert "export OBJECT_STORE_PREFIX=prod/gfs" in captured["script"]
    assert "export NHMS_PUBLISHED_ARTIFACT_ROOT=/published" in captured["script"]
    assert "export NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://" in captured["script"]


def test_resource_profile_safe_account_and_day_walltime_render(monkeypatch, tmp_path):
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(
                _write_resource_profiles_with_update(
                    tmp_path,
                    {"partition": "compute-gpu.1", "account": "friends.team-1", "walltime": "2-01:00:00"},
                )
            ),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )
    captured: dict[str, str] = {}

    def fake_run(command, **kwargs):
        del kwargs
        captured["script"] = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    gateway.submit_job_array(
        job_type="run_shud_forecast_array",
        cycle_id="cycle_001",
        stage_name="forecast",
        tasks=[{**_fake_array_task("run_001", "model_001"), "workspace_dir": str(tmp_path / "workspace")}],
        manifest={
            "run_id": "cycle_001",
            "model_id": "model_001",
            "workspace_dir": str(tmp_path / "workspace"),
            "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
        },
    )

    assert "#SBATCH --partition=compute-gpu.1" in captured["script"]
    assert "#SBATCH --account=friends.team-1" in captured["script"]
    assert "#SBATCH --time=2-01:00:00" in captured["script"]


def test_scancel_invocation(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        executable = Path(command[0]).name
        if executable == "scancel":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if executable == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="12345|CANCELLED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.cancel_job("12345")

    assert calls == [
        ["scancel", "12345"],
        [
            "sacct",
            "--parsable2",
            "--noheader",
            "--format=JobID,State,ExitCode,Start,End,Elapsed,MaxRSS,AveRSS,AllocTRES",
            "--jobs=12345",
        ],
    ]
    assert record.status == SlurmJobStatus.CANCELLED
    assert record.manifest["cancellation_proven"] is True


def test_scancel_success_requires_authoritative_cancelled_state(monkeypatch, tmp_path) -> None:
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        executable = Path(command[0]).name
        if executable == "scancel":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if executable == "sacct":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="12345|RUNNING|0:0|2026-05-08T12:00:00|\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmGatewayError) as exc_info:
        gateway.cancel_job("12345")

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "SLURM_CANCELLATION_PENDING"
    assert exc_info.value.details["cancellation_proven"] is False


def test_scancel_success_with_missing_accounting_reports_gap(monkeypatch, tmp_path) -> None:
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        executable = Path(command[0]).name
        if executable == "scancel":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if executable == "sacct":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmGatewayError) as exc_info:
        gateway.cancel_job("12345")

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "SLURM_CANCELLATION_GAP"


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


def test_array_template_contract_rejects_swapped_allowlisted_template(tmp_path: Path) -> None:
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )

    with pytest.raises(TemplateSecurityError):
        gateway.submit_job_array(
            job_type="produce_forcing_array",
            cycle_id="cycle_001",
            stage_name="forcing",
            tasks=[_fake_array_task("run_001", "model_001")],
            manifest={
                "slurm_job_type_templates": {
                    **DEFAULT_JOB_TYPE_TEMPLATES,
                    "produce_forcing_array": "run_shud_forecast_array.sbatch",
                }
            },
        )


def test_non_array_template_contract_rejects_swapped_allowlisted_template_before_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for mismatched non-array template contract")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TemplateSecurityError):
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="download_source_cycle",
                manifest={
                    **_production_manifest(tmp_path, "download_source_cycle"),
                    "slurm_job_type_templates": {
                        **DEFAULT_JOB_TYPE_TEMPLATES,
                        "download_source_cycle": "convert_canonical.sbatch",
                    },
                },
            )
        )


@pytest.mark.parametrize(
    "secret_template",
    [
        "s3://bucket/template.sbatch?token=supersecret",
        "https://user:supersecret@example.com/template.sbatch",
    ],
)
def test_submit_job_rejects_secret_template_mapping_before_contract_or_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_template: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret template mappings")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="download_source_cycle",
                manifest={
                    **_production_manifest(tmp_path, "download_source_cycle"),
                    "slurm_job_type_templates": {
                        **DEFAULT_JOB_TYPE_TEMPLATES,
                        "download_source_cycle": secret_template,
                    },
                },
            )
        )

    details = json.dumps(exc_info.value.details)
    assert secret_template not in details
    assert "supersecret" not in details


@pytest.mark.parametrize(
    "secret_job_type",
    [
        "s3://bucket/template.sbatch?token=supersecret",
        "https://user:supersecret@example.com/template.sbatch",
    ],
)
def test_submit_job_rejects_secret_top_level_job_type_before_template_lookup_or_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_job_type: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret job_type")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type=secret_job_type,
            )
        )

    details = json.dumps(exc_info.value.details)
    assert secret_job_type not in details
    assert "supersecret" not in details


@pytest.mark.parametrize(
    "secret_template",
    [
        "s3://bucket/template.sbatch?token=supersecret",
        "https://user:supersecret@example.com/template.sbatch",
    ],
)
def test_submit_job_array_rejects_secret_template_mapping_before_contract_manifest_or_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_template: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret template mappings")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job_array(
            job_type="run_shud_forecast_array",
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=[_fake_array_task("run_001", "model_001")],
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "workspace"),
                "slurm_job_type_templates": {
                    **DEFAULT_JOB_TYPE_TEMPLATES,
                    "run_shud_forecast_array": secret_template,
                },
            },
        )

    details = json.dumps(exc_info.value.details)
    assert secret_template not in details
    assert "supersecret" not in details
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


@pytest.mark.parametrize(
    "secret_job_type",
    [
        "s3://bucket/template.sbatch?token=supersecret",
        "https://user:supersecret@example.com/template.sbatch",
    ],
)
def test_submit_job_array_rejects_secret_direct_job_type_before_manifest_index_or_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_job_type: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_write_manifest_index(*args, **kwargs):
        del args, kwargs
        raise AssertionError("write_manifest_index must not be called for secret job_type")

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret job_type")

    monkeypatch.setattr(gateway, "write_manifest_index", fake_write_manifest_index)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job_array(
            job_type=secret_job_type,
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=[_fake_array_task("run_001", "model_001")],
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
            },
        )

    details = json.dumps(exc_info.value.details)
    assert secret_job_type not in details
    assert "supersecret" not in details
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


@pytest.mark.parametrize(
    "secret_job_type",
    [
        "s3://bucket/template.sbatch?token=supersecret",
        "https://user:supersecret@example.com/template.sbatch",
    ],
)
def test_render_template_rejects_secret_direct_job_type_before_template_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_job_type: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_resolve_template_path(job_type: str):
        del job_type
        raise AssertionError("_resolve_template_path must not be called for secret job_type")

    monkeypatch.setattr(gateway, "_resolve_template_path", fake_resolve_template_path)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.render_template(
            secret_job_type,
            _production_manifest(tmp_path, "download_source_cycle"),
        )

    details = json.dumps(exc_info.value.details)
    assert secret_job_type not in details
    assert "supersecret" not in details


def test_safe_slurm_env_reaches_rendered_non_array_template_and_secret_is_rejected(tmp_path: Path) -> None:
    gateway = _production_gateway(tmp_path)
    manifest = {
        **_production_manifest(tmp_path, "download_source_cycle"),
        "slurm_env": {"NHMS_PROFILE": "prod/gfs_00", "NHMS_RUN_LABEL": "prod_gfs_00"},
    }

    rendered = gateway.render_template("download_source_cycle", manifest)

    assert f"export PATH={shlex.quote(str((Path.cwd() / '.venv' / 'bin').resolve()))}:$PATH" in rendered
    assert "export NHMS_PROFILE=prod/gfs_00" in rendered
    assert "export NHMS_RUN_LABEL=prod_gfs_00" in rendered
    with pytest.raises(ManifestValidationError):
        gateway.render_template(
            "download_source_cycle",
            {**manifest, "slurm_env": {"AWS_SECRET_ACCESS_KEY": "supersecret"}},
        )
    with pytest.raises(ManifestValidationError):
        gateway.render_template(
            "download_source_cycle",
            {**manifest, "slurm_env": {"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"}},
        )
    with pytest.raises(ManifestValidationError):
        gateway.render_template(
            "download_source_cycle",
            {**manifest, "slurm_env": {"OBJECT_STORE_PREFIX": "s3://bucket/prod?X-Amz-Signature=supersecret"}},
        )
    with pytest.raises(ManifestValidationError):
        gateway.render_template(
            "download_source_cycle",
            {**manifest, "slurm_env": {"NHMS_PROFILE": "https://user:supersecret@example.com/profile"}},
        )


def test_standalone_gateway_injects_grib_runtime_env_for_download_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = "/scratch/frd_muziyao/nhms-grib"
    monkeypatch.setenv("NHMS_GRIB_ENV_ROOT", root)
    gateway = _production_gateway(tmp_path)

    rendered = gateway.render_template("download_source_cycle", _production_manifest(tmp_path, "download_source_cycle"))

    quoted = shlex.quote(root)
    assert f"export PATH={quoted}/bin:$PATH" in rendered
    assert f"export LD_LIBRARY_PATH={quoted}/lib:${{LD_LIBRARY_PATH:-}}" in rendered


def test_safe_slurm_env_reaches_rendered_array_template_and_secret_is_rejected(tmp_path: Path) -> None:
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )
    manifest = {
        "run_id": "run_001",
        "model_id": "model_001",
        "job_type": "run_shud_forecast_array",
        "cycle_id": "cycle_001",
        "stage_name": "forecast",
        "manifest_index_path": str(tmp_path / "index.json"),
        "workspace_dir": str(tmp_path / "workspace"),
        "object_store_root": str(tmp_path / "object-store"),
        "slurm_env": {"NHMS_PROFILE": "prod/gfs_00", "NHMS_RUN_LABEL": "prod_gfs_00"},
    }

    rendered = gateway.render_template("run_shud_forecast_array", manifest, str(tmp_path / "index.json"))

    assert "export NHMS_PROFILE=prod/gfs_00" in rendered
    assert "export NHMS_RUN_LABEL=prod_gfs_00" in rendered
    with pytest.raises(ManifestValidationError):
        gateway.render_template(
            "run_shud_forecast_array",
            {**manifest, "slurm_env": {"AWS_SECRET_ACCESS_KEY": "supersecret"}},
            str(tmp_path / "index.json"),
        )
    with pytest.raises(ManifestValidationError):
        gateway.render_template(
            "run_shud_forecast_array",
            {**manifest, "slurm_env": {"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"}},
            str(tmp_path / "index.json"),
        )
    with pytest.raises(ManifestValidationError):
        gateway.render_template(
            "run_shud_forecast_array",
            {**manifest, "slurm_env": {"OBJECT_STORE_PREFIX": "s3://bucket/prod?signature=supersecret"}},
            str(tmp_path / "index.json"),
        )


def test_render_template_preserves_safe_url_query_execution_input(tmp_path: Path) -> None:
    safe_url = "https://example.com/notify?run=run_001&source=GFS"
    template_dir = _write_template(
        tmp_path,
        name="safe_url.sbatch",
        content='#!/usr/bin/env bash\necho "callback={{metadata.callback_uri}}"\n',
    )
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"run_shud_analysis": "safe_url.sbatch"},
            workspace_dir=str(tmp_path / "workspace"),
        )
    )

    rendered = gateway.render_template(
        "run_shud_analysis",
        {
            **_production_manifest(tmp_path, "run_shud_analysis"),
            "metadata": {"callback_uri": safe_url},
        },
    )

    assert f'echo "callback={safe_url}"' in rendered
    assert "[redacted]" not in rendered


def test_render_template_rejects_secret_url_value_before_render(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_url = "https://user:supersecret@example.com/notify?token=secret-token"
    template_dir = _write_template(
        tmp_path,
        name="secret_url.sbatch",
        content='#!/usr/bin/env bash\necho "callback={{metadata.callback_uri}}"\n',
    )
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"run_shud_analysis": "secret_url.sbatch"},
            workspace_dir=str(tmp_path / "workspace"),
        )
    )

    def fake_resolve_template_path(job_type: str):
        del job_type
        raise AssertionError("_resolve_template_path must not be called for secret URL values")

    monkeypatch.setattr(gateway, "_resolve_template_path", fake_resolve_template_path)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.render_template(
            "run_shud_analysis",
            {
                **_production_manifest(tmp_path, "run_shud_analysis"),
                "metadata": {"callback_uri": secret_url},
            },
        )

    details = json.dumps(exc_info.value.details)
    assert secret_url not in details
    assert "supersecret" not in details
    assert "secret-token" not in details


@pytest.mark.parametrize(
    "slurm_env",
    [
        {"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"},
        {"NHMS_PROFILE": "https://user:supersecret@example.com/profile"},
        {"OBJECT_STORE_PREFIX": "s3://bucket/prod?token=supersecret"},
    ],
)
def test_submit_job_rejects_secret_slurm_env_before_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    slurm_env: dict[str, str],
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for rejected slurm_env")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError):
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="download_source_cycle",
                manifest={
                    **_production_manifest(tmp_path, "download_source_cycle"),
                    "slurm_env": slurm_env,
                    "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                },
            )
        )


@pytest.mark.parametrize(
    "slurm_env",
    [
        {"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"},
        {"NHMS_PROFILE": "https://user:supersecret@example.com/profile"},
        {"OBJECT_STORE_PREFIX": "s3://bucket/prod?password=supersecret"},
    ],
)
def test_submit_job_array_rejects_secret_slurm_env_before_manifest_or_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    slurm_env: dict[str, str],
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for rejected slurm_env")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError):
        gateway.submit_job_array(
            job_type="run_shud_forecast_array",
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=[_fake_array_task("run_001", "model_001")],
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                "slurm_env": slurm_env,
            },
        )
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


@pytest.mark.parametrize(
    "reserved_key",
    [
        "NHMS_MANIFEST_INDEX",
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "NHMS_RUN_ID",
        "NHMS_MODEL_ID",
        "NHMS_CYCLE_ID",
        "NHMS_JOB_TYPE",
        "SHUD_THREADS",
        "OMP_NUM_THREADS",
        "SLURM_ARRAY_TASK_ID",
        "OBJECT_STORE_PREFIX",
    ],
)
def test_submit_job_rejects_reserved_slurm_env_before_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reserved_key: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for reserved slurm_env")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="download_source_cycle",
                manifest={
                    **_production_manifest(tmp_path, "download_source_cycle"),
                    "slurm_env": {reserved_key: "override"},
                    "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                },
            )
        )

    assert exc_info.value.details["field"] == f"slurm_env.{reserved_key}"


def test_submit_job_array_rejects_reserved_slurm_env_before_manifest_or_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for reserved slurm_env")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job_array(
            job_type="run_shud_forecast_array",
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=[_fake_array_task("run_001", "model_001")],
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                "slurm_env": {"NHMS_MANIFEST_INDEX": "/tmp/evil.json"},
            },
        )

    assert exc_info.value.details["field"] == "slurm_env.NHMS_MANIFEST_INDEX"
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


@pytest.mark.parametrize(
    ("manifest_update", "secret_text"),
    [
        (
            {"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"},
            "supersecret",
        ),
        ({"database_dsn": "postgresql://nhms@db.prod.example/nhms"}, "database_dsn"),
        ({"metadata": {"callback_uri": "https://user:supersecret@example.com/notify"}}, "supersecret"),
        ({"output_uri": "s3://bucket/prod?token=supersecret"}, "supersecret"),
    ],
)
def test_submit_job_rejects_secret_manifest_fields_before_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_update: dict[str, object],
    secret_text: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret manifest fields")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="download_source_cycle",
                manifest={
                    **_production_manifest(tmp_path, "download_source_cycle"),
                    **manifest_update,
                    "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                },
            )
        )

    assert secret_text not in json.dumps(exc_info.value.details)


@pytest.mark.parametrize(
    ("manifest_update", "secret_text"),
    [
        (
            {"https://user:supersecret@example.com/callback": "notify"},
            "https://user:supersecret@example.com/callback",
        ),
        (
            {"metadata": {"s3://bucket/path?token=supersecret": "signed"}},
            "s3://bucket/path?token=supersecret",
        ),
        ({"metadata": {"database_dsn": "postgresql://nhms@db.prod.example/nhms"}}, "database_dsn"),
    ],
)
def test_submit_job_rejects_secret_manifest_keys_without_raw_error_or_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_update: dict[str, object],
    secret_text: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret manifest keys")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="download_source_cycle",
                manifest={
                    **_production_manifest(tmp_path, "download_source_cycle"),
                    **manifest_update,
                    "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                },
            )
        )

    details = json.dumps(exc_info.value.details)
    assert exc_info.value.details["findings"][0]["field"].endswith("[redacted]")
    assert secret_text not in details
    assert "supersecret" not in details


def test_submit_job_rejects_secret_unsafe_manifest_key_before_unsafe_field_echo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway = _production_gateway(tmp_path)
    raw_key = "https://user:supersecret@example.com/callback"

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret unsafe manifest keys")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="download_source_cycle",
                manifest={
                    **_production_manifest(tmp_path, "download_source_cycle"),
                    raw_key: "notify",
                    "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                },
            )
        )

    assert exc_info.value.message == "Manifest rejects secret-bearing fields and URL values."
    details = json.dumps(exc_info.value.details)
    assert raw_key not in details
    assert "supersecret" not in details
    assert exc_info.value.details["findings"][0] == {"field": "manifest.[redacted]", "reason": "url_userinfo"}


def test_submit_job_allows_safe_nested_metadata_keys_and_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        assert Path(command[0]).name == "sbatch"
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job(
        SubmitJobRequest(
            run_id="run_001",
            model_id="model_001",
            job_type="download_source_cycle",
            manifest={
                **_production_manifest(tmp_path, "download_source_cycle"),
                "metadata": {"callback_uri": "https://example.com/notify", "safe_key": "safe/value"},
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
            },
        )
    )

    assert record.job_id == "12345"
    assert record.manifest["metadata"]["callback_uri"] == "https://example.com/notify"


@pytest.mark.parametrize(
    ("manifest_update", "secret_text"),
    [
        (
            {"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"},
            "supersecret",
        ),
        ({"database_uri": "postgresql://nhms@db.prod.example/nhms"}, "database_uri"),
        ({"metadata": {"callback_uri": "https://user:supersecret@example.com/notify"}}, "supersecret"),
        ({"object_store_root": "s3://bucket/prod?signature=supersecret"}, "supersecret"),
    ],
)
def test_submit_job_array_rejects_secret_manifest_fields_before_manifest_or_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_update: dict[str, object],
    secret_text: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret manifest fields")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job_array(
            job_type="run_shud_forecast_array",
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=[_fake_array_task("run_001", "model_001")],
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                **manifest_update,
            },
        )

    assert secret_text not in json.dumps(exc_info.value.details)
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


@pytest.mark.parametrize(
    ("manifest_update", "secret_text"),
    [
        (
            {"https://user:supersecret@example.com/callback": "notify"},
            "https://user:supersecret@example.com/callback",
        ),
        (
            {"metadata": {"s3://bucket/path?token=supersecret": "signed"}},
            "s3://bucket/path?token=supersecret",
        ),
        ({"metadata": {"database_uri": "postgresql://nhms@db.prod.example/nhms"}}, "database_uri"),
    ],
)
def test_submit_job_array_rejects_secret_manifest_keys_before_manifest_or_sbatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifest_update: dict[str, object],
    secret_text: str,
) -> None:
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret manifest keys")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job_array(
            job_type="run_shud_forecast_array",
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=[_fake_array_task("run_001", "model_001")],
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
                **manifest_update,
            },
        )

    details = json.dumps(exc_info.value.details)
    assert secret_text not in details
    assert "supersecret" not in details
    assert exc_info.value.details["findings"][0]["field"].endswith("[redacted]")
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


def test_mock_gateway_rejects_secret_manifest_keys_without_recording_job(tmp_path: Path) -> None:
    gateway = MockSlurmGateway(SlurmGatewaySettings(backend="mock", workspace_dir=str(tmp_path / "workspace")))
    raw_key = "s3://bucket/path?token=supersecret"

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job(
            SubmitJobRequest(
                run_id="run_001",
                model_id="model_001",
                job_type="run_shud_analysis",
                manifest={"run_id": "run_001", "model_id": "model_001", raw_key: "signed"},
            )
        )

    details = json.dumps(exc_info.value.details)
    assert raw_key not in details
    assert "supersecret" not in details
    assert gateway._jobs == {}


def test_mock_gateway_rejects_array_secret_manifest_keys_without_recording_job(tmp_path: Path) -> None:
    gateway = MockSlurmGateway(SlurmGatewaySettings(backend="mock", workspace_dir=str(tmp_path / "workspace")))
    raw_key = "https://user:supersecret@example.com/callback"

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job_array(
            {
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [_fake_array_task("run_001", "model_001")],
                "manifest": {"run_id": "cycle_001", "model_id": "model_001", "metadata": {raw_key: "notify"}},
            }
        )

    details = json.dumps(exc_info.value.details)
    assert raw_key not in details
    assert "supersecret" not in details
    assert gateway._jobs == {}


def test_mock_gateway_rejects_array_over_entry_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from packages.common import manifest_index as manifest_index_module

    gateway = MockSlurmGateway(SlurmGatewaySettings(backend="mock", workspace_dir=str(tmp_path / "workspace")))
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_ENTRIES", 1)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job_array(
            {
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [
                    _fake_array_task("run_001", "model_001"),
                    _fake_array_task("run_002", "model_002"),
                ],
            }
        )

    assert exc_info.value.details["entry_limit"] == 1


def test_manifest_injection_rejected(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid manifests")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError):
        gateway.submit_job(
            SubmitJobRequest(run_id="run_001;rm", model_id="model_001", job_type="run_shud_analysis")
        )


def test_array_capable_job_type_rejected_from_single_submit(monkeypatch, tmp_path) -> None:
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for array-capable single submit")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmValidationError) as exc_info:
        gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="run_shud_forecast_array"))

    assert exc_info.value.details["endpoint"] == "/api/v1/slurm/job-arrays"


def test_unsupported_legacy_job_type_rejected_before_submission(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for unsupported job_type")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TemplateNotFoundError):
        gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="legacy_unsupported"))


def test_hindcast_single_job_type_is_rejected_before_sbatch(monkeypatch, tmp_path):
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

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for hindcast single submit")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmValidationError):
        gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="hindcast"))



def test_submit_hindcast_slurm_payload_writes_real_forcing_manifest_index(monkeypatch, tmp_path: Path) -> None:
    with _hindcast_store() as session:
        _insert_hindcast_forcing_version(session, 1993, "object://forcing/package/1993")
        gateway = _production_gateway(tmp_path)
        monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@db.prod.example/nhms")
        workspace_root = tmp_path / "workspace"
        object_store_root = workspace_root / "object-store"
        workspace_root.mkdir()
        object_store_root.mkdir()

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
                workspace_root=workspace_root,
                object_store_root=object_store_root,
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
        gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="run_shud_analysis"))


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
    healthy = gateway.health()
    assert healthy.backend == "slurm"
    assert healthy.status == "healthy"
    assert healthy.healthy is True
    assert healthy.version == "slurm 24.05.1"
    assert healthy.error is None
    assert set(healthy.binaries) == {"sbatch", "squeue", "sacct", "scancel"}
    assert all(probe.resolved and probe.executable for probe in healthy.binaries.values())

    def failure(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing binary")

    monkeypatch.setattr(subprocess, "run", failure)
    response = gateway.health()
    assert response.backend == "slurm"
    assert response.status == "unhealthy"
    assert response.healthy is False
    assert response.version == ""
    assert response.error
    assert all(not probe.executable for probe in response.binaries.values())


def test_health_survives_non_slurm_probe_exception(monkeypatch, tmp_path):
    # A probe failure that is NOT a SlurmGatewayError (e.g. PermissionError raised
    # below the wrapping layer) must still degrade to a structured healthy=false
    # response so /api/v1/slurm/health never returns a 500.
    gateway = _gateway(tmp_path)

    def boom(command, **kwargs):
        del kwargs
        raise PermissionError("secret-token leaked in message")

    monkeypatch.setattr(subprocess, "run", boom)
    response = gateway.health()
    assert response.status == "unhealthy"
    assert response.healthy is False
    assert all(not probe.executable for probe in response.binaries.values())


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


def test_fetch_logs_refuses_symlink_swap_between_validation_and_open(monkeypatch, tmp_path):
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
    secret_path.write_text("target-secret", encoding="utf-8")
    log_dir = tmp_path / "workspace" / "run_001" / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "12345.out"
    log_path.write_text("safe log", encoding="utf-8")

    original_stat = os.stat
    swapped = False

    def swapping_stat(path, *args, **kwargs):
        nonlocal swapped
        result = original_stat(path, *args, **kwargs)
        if not swapped and path == log_path.name and kwargs.get("dir_fd") is not None:
            swapped = True
            log_path.unlink()
            log_path.symlink_to(secret_path)
        return result

    monkeypatch.setattr(os, "stat", swapping_stat)

    response = gateway.fetch_logs("12345")

    assert swapped is True
    assert response.logs == ""
    assert response.truncated is False
    assert "target-secret" not in response.model_dump_json()


def test_fetch_logs_regular_file_is_bounded_and_marked_truncated(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"))
    monkeypatch.setattr("services.slurm_gateway.real_backend.MAX_LOG_BYTES", 8)
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
    (log_dir / "12345.out").write_text("0123456789abcdef", encoding="utf-8")

    response = gateway.fetch_logs("12345")

    assert response.logs == "01234567" + LOG_TRUNCATION_MARKER
    assert response.truncated is True


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


def test_slurm_command_failure_details_are_bounded_and_redacted(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    secret_stdout = "token=supersecret " + ("x" * 300_000)
    secret_stderr = "https://user:supersecret@example.com/path?token=abc " + ("y" * 300_000)

    def fake_run(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 1, stdout=secret_stdout, stderr=secret_stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmGatewayError) as exc_info:
        gateway.get_job_status("12345")

    details_text = json.dumps(exc_info.value.details)
    assert exc_info.value.code == "SLURM_COMMAND_ERROR"
    assert len(details_text) < 8000
    assert "supersecret" not in details_text
    assert "token=abc" not in details_text
    assert exc_info.value.details["stdout"]["truncated"] is True
    assert exc_info.value.details["stderr"]["truncated"] is True


def test_oversized_sacct_output_is_bounded_and_redacted_before_parse(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    malformed = "malformed|token=supersecret|" + ("x" * 300_000)

    def fake_run(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 0, stdout=malformed, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmGatewayError) as exc_info:
        gateway.list_jobs(limit=1, offset=0)

    details_text = json.dumps(exc_info.value.details)
    assert exc_info.value.code == "SLURM_COMMAND_ERROR"
    assert len(details_text) < 8000
    assert "supersecret" not in details_text
    assert exc_info.value.details["stdout"]["truncated"] is True


def test_sacct_parse_error_details_are_bounded_and_redacted(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    malformed = "malformed|token=supersecret|"

    def fake_run(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 0, stdout=malformed, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmParseError) as exc_info:
        gateway.list_jobs(limit=1, offset=0)

    details_text = json.dumps(exc_info.value.details)
    assert len(details_text) < 8000
    assert "supersecret" not in details_text
    assert exc_info.value.details["stdout"]["truncated"] is False


def test_sacct_status_and_array_normal_parsing_survives_output_bounding(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        if "--format=JobID,State,ExitCode,Start,End,Elapsed,MaxRSS,AveRSS,AllocTRES" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "12345|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|"
                    "00:05:00|1024K|512K|cpu=2,mem=4G\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="12345_0|COMPLETED|0:0|00:01:00|256K|128K|cpu=1,mem=1G\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    status = gateway.get_job_status("12345")
    tasks = gateway.get_array_task_results("12345")

    assert status.status == SlurmJobStatus.SUCCEEDED
    assert status.max_rss == "1024K"
    assert tasks[0]["status"] == "succeeded"
    assert tasks[0]["resource_metrics"]["alloc_tres"] == "cpu=1,mem=1G"


def test_fake_slurm_command_matrix_for_production_job_types(monkeypatch, tmp_path) -> None:
    gateway = _production_gateway(tmp_path)
    commands: list[list[str]] = []
    submitted_job_ids = iter(("12345", "12346"))

    def fake_run(command, **kwargs):
        del kwargs
        commands.append(command)
        executable = Path(command[0]).name
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="slurm 24.05.1\n", stderr="")
        if executable == "sbatch":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"Submitted batch job {next(submitted_job_ids)}\n",
                stderr="",
            )
        status_format = "--format=JobID,State,ExitCode,Start,End,Elapsed,MaxRSS,AveRSS,AllocTRES"
        if executable == "sacct" and status_format in command:
            requested_job = next(arg.removeprefix("--jobs=") for arg in command if arg.startswith("--jobs="))
            state = "CANCELLED" if requested_job == hindcast_job_id else "COMPLETED"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f"{requested_job}|{state}|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00|"
                    "00:05:00|1024K|512K|cpu=2,mem=4G\n"
                ),
                stderr="",
            )
        if executable == "sacct" and "--format=JobID,State,ExitCode,Start,End" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="12345|COMPLETED|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00\n",
                stderr="",
            )
        if executable == "sacct" and "--format=JobID,State,ExitCode,Elapsed,MaxRSS,AveRSS,AllocTRES" in command:
            requested_job = next(arg.removeprefix("--jobs=") for arg in command if arg.startswith("--jobs="))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f"{requested_job}_0|COMPLETED|0:0|00:01:00|256K|128K|cpu=1,mem=1G\n"
                    f"{requested_job}_1|FAILED|1:0|00:02:00|512K|256K|cpu=1,mem=1G\n"
                ),
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
    hindcast_job_id = "12346"
    hindcast_record = gateway.submit_job_array(
        {
            "job_type": "hindcast",
            "cycle_id": "hindcast_cycle_001",
            "stage_name": "hindcast",
            "manifest": _production_manifest(tmp_path, "hindcast"),
            "tasks": [
                {
                    **_fake_array_task("hindcast_run_001", "model_001"),
                    "workspace_dir": str(tmp_path / "workspace"),
                    "source_id": "ERA5",
                    "year": 1993,
                },
                {
                    **_fake_array_task("hindcast_run_002", "model_001"),
                    "workspace_dir": str(tmp_path / "workspace"),
                    "source_id": "ERA5",
                    "year": 1994,
                },
            ],
        }
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
    assert {"sbatch", "squeue", "sacct", "scancel"}.issubset({Path(command[0]).name for command in commands})


def test_partition_override_redirects_resolved_partition(tmp_path: Path) -> None:
    base = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )
    assert base.resolve_resource_profile("any_model")["partition"] == "compute"

    overridden = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            partition_override="CPU",
        )
    )
    # Per-deployment override targets the cluster's real partition without editing
    # the shared canonical profile (which still defaults to "compute").
    assert overridden.resolve_resource_profile("any_model")["partition"] == "CPU"


@pytest.mark.parametrize(
    "malicious_override",
    [
        "CPU; rm -rf /",
        "CPU\n#SBATCH x",
        "-X",
    ],
)
def test_partition_override_rejects_injection(tmp_path: Path, malicious_override: str) -> None:
    # A partition override carrying shell metacharacters, a newline, or a leading
    # dash must be rejected by validate_resource_profile->validate_slurm_identifier
    # at resolution time, never reaching an sbatch invocation.
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            partition_override=malicious_override,
        )
    )
    with pytest.raises(ConfigurationError):
        gateway.resolve_resource_profile("any_model")
