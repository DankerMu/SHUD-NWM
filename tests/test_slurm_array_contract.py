from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

import pytest

from packages.common import state_cli
from packages.common.manifest_index import ManifestValidationError, load_manifest_entry, resolve_task_id
from services.orchestrator import cli as orchestrator_cli
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings
from services.slurm_gateway.gateway import ManifestValidationError as GatewayManifestValidationError
from services.slurm_gateway.real_backend import RealSlurmGateway
from workers.canonical_converter import cli as canonical_cli
from workers.data_adapters import cli as data_cli
from workers.forcing_producer import cli as forcing_cli
from workers.output_parser import cli as output_cli
from workers.shud_runtime import cli as runtime_cli


def _write_profiles(tmp_path: Path) -> Path:
    path = tmp_path / "resource_profiles.yaml"
    path.write_text(
        """
resource_profiles:
  default:
    partition: compute
    nodes: 1
    ntasks: 1
    cpus_per_task: 8
    memory_gb: 32
    walltime: "01:00:00"
    max_concurrent: 2
    shud_threads: 8
  overrides: {}
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _gateway(tmp_path: Path) -> RealSlurmGateway:
    return RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )


def _render_manifest(tmp_path: Path, job_type: str) -> dict[str, Any]:
    return {
        "run_id": "run_001",
        "model_id": "model_001",
        "basin_version_id": "basin_001",
        "river_network_version_id": "river_001",
        "job_type": job_type,
        "stage": job_type,
        "stage_name": job_type,
        "cycle_id": "cycle_001",
        "source_id": "GFS",
        "cycle_time": "2026051200",
        "workspace_dir": str(tmp_path / "workspace"),
        "manifest_index_path": str(tmp_path / "manifest_index.json"),
    }


def _manifest_index(tmp_path: Path) -> Path:
    path = tmp_path / "manifest_index.json"
    entries = [
        {
            "task_id": 0,
            "model_id": "model_001",
            "basin_version_id": "basin_001",
            "river_network_version_id": "river_001",
            "run_id": "run_001",
            "workspace_dir": str(tmp_path / "workspace"),
            "source_id": "GFS",
            "cycle_time": "2026051200",
        },
        {
            "task_id": 1,
            "model_id": "model_002",
            "basin_version_id": "basin_002",
            "river_network_version_id": "river_002",
            "run_id": "run_002",
            "workspace_dir": str(tmp_path / "workspace"),
            "source_id": "IFS",
            "cycle_time": "2026051212",
        },
    ]
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _invoke_main(main: Callable[[Sequence[str]], int], argv: Sequence[str]) -> None:
    try:
        result = main(argv)
    except SystemExit as exc:
        assert exc.code in (0, None)
    else:
        assert result == 0


def _invoke_main_expect_failure(main: Callable[[Sequence[str]], int], argv: Sequence[str]) -> None:
    try:
        result = main(argv)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        assert result == 1


def _main_exit_code(main: Callable[[Sequence[str]], int], argv: Sequence[str]) -> int:
    try:
        result = main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return int(result)


def _rendered_command_argv(rendered: str, *, manifest_index_path: Path) -> list[str]:
    command = [
        line.strip()
        for line in rendered.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "export "))
    ][-1]
    substitutions = {
        "$NHMS_MANIFEST_INDEX": str(manifest_index_path),
        "${SLURM_ARRAY_TASK_ID:-0}": "0",
    }
    return [substitutions.get(arg, arg) for arg in shlex.split(command)]


def _mock_array_cli_dependencies(monkeypatch: pytest.MonkeyPatch, executable: str) -> Callable[[Sequence[str]], int]:
    if executable == "nhms-forcing":
        monkeypatch.setattr(forcing_cli.ForcingProducer, "from_env", staticmethod(lambda: _FakeProducer()))
        return forcing_cli.main
    if executable == "nhms-shud-runtime":
        monkeypatch.setattr(runtime_cli.SHUDRuntime, "from_env", staticmethod(lambda dry_run=False: _FakeRuntime()))
        return runtime_cli.main
    if executable == "nhms-parse":
        monkeypatch.setattr(output_cli.OutputParser, "from_env", staticmethod(lambda: _FakeParser()))
        return output_cli.main
    raise AssertionError(f"Unsupported executable: {executable}")


class _FakeProducer:
    def produce(self, **kwargs):
        return SimpleNamespace(
            status="succeeded",
            forcing_version_id="forcing_001",
            forcing_package_uri="file:///forcing",
            checksum="abc",
            station_count=1,
            timestep_count=2,
        )


class _FakeRuntime:
    def execute_manifest_path(self, manifest: str):
        return SimpleNamespace(
            run_id="run_001",
            status="succeeded",
            output_uri="file:///output",
            log_uri="file:///log",
            rivqdown_file="rivqdown.txt",
        )


class _FakeParser:
    def parse_run(self, run_id: str):
        return SimpleNamespace(
            run_id=run_id,
            status="parsed",
            source_file="rivqdown.txt",
            rows_written=10,
            qc_passed=True,
            max_value_m3s=12.5,
        )


@pytest.mark.parametrize(
    ("job_type", "expected_command"),
    [
        (
            "produce_forcing_array",
            'nhms-forcing produce --manifest-index "$NHMS_MANIFEST_INDEX" --task-id "${SLURM_ARRAY_TASK_ID:-0}"',
        ),
        (
            "run_shud_forecast_array",
            'nhms-shud-runtime execute --manifest-index "$NHMS_MANIFEST_INDEX" --task-id "${SLURM_ARRAY_TASK_ID:-0}"',
        ),
        (
            "parse_output_array",
            'nhms-parse shud-output --manifest-index "$NHMS_MANIFEST_INDEX" --task-id "${SLURM_ARRAY_TASK_ID:-0}"',
        ),
        (
            "save_state_snapshot_array",
            'nhms-state save --manifest-index "$NHMS_MANIFEST_INDEX" --task-id "${SLURM_ARRAY_TASK_ID:-0}"',
        ),
        ("publish_tiles", 'nhms-pipeline publish-tiles --cycle-id "$NHMS_CYCLE_ID"'),
        (
            "convert_canonical",
            'nhms-canonical convert --source-id "${NHMS_SOURCE_ID:-GFS}" --cycle-time "$NHMS_CYCLE_TIME"',
        ),
    ],
)
def test_real_templates_render_supported_cli_commands(tmp_path, job_type, expected_command):
    rendered = _gateway(tmp_path).render_template(
        job_type,
        _render_manifest(tmp_path, job_type),
        str(tmp_path / "manifest_index.json"),
    )

    assert expected_command in rendered


def test_state_save_array_template_exports_db_free_state_index_env(tmp_path: Path) -> None:
    registry_manifest = tmp_path / "object-store" / "db-free" / "registry-manifest.json"
    readiness_index = tmp_path / "object-store" / "db-free" / "canonical-readiness-index.json"
    state_index = tmp_path / "object-store" / "db-free" / "state-index.json"
    allowed_roots = os.pathsep.join((str(tmp_path / "workspace"), str(tmp_path / "object-store")))
    rendered = _gateway(tmp_path).render_template(
        "save_state_snapshot_array",
        {
            **_render_manifest(tmp_path, "save_state_snapshot_array"),
            "scheduler_db_free_required": "true",
            "scheduler_allowed_roots": allowed_roots,
            "scheduler_registry_backend": "file",
            "scheduler_registry_manifest": str(registry_manifest),
            "scheduler_canonical_readiness_backend": "file",
            "scheduler_canonical_readiness_index": str(readiness_index),
            "scheduler_state_index_backend": "file",
            "scheduler_state_index": str(state_index),
        },
        str(tmp_path / "manifest_index.json"),
    )

    assert "export NHMS_SCHEDULER_DB_FREE_REQUIRED=true" in rendered
    assert f"export NHMS_SCHEDULER_ALLOWED_ROOTS={shlex.quote(allowed_roots)}" in rendered
    assert "export NHMS_SCHEDULER_REGISTRY_BACKEND=file" in rendered
    assert f"export NHMS_SCHEDULER_REGISTRY_MANIFEST={shlex.quote(str(registry_manifest))}" in rendered
    assert "export NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND=file" in rendered
    assert f"export NHMS_SCHEDULER_CANONICAL_READINESS_INDEX={shlex.quote(str(readiness_index))}" in rendered
    assert "export NHMS_SCHEDULER_STATE_INDEX_BACKEND=file" in rendered
    assert f"export NHMS_SCHEDULER_STATE_INDEX={shlex.quote(str(state_index))}" in rendered


def test_state_save_array_template_does_not_fallback_to_secret_state_index_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "NHMS_SCHEDULER_STATE_INDEX",
        "s3://nhms/scheduler/state-index.json?X-Amz-Signature=super-secret",
    )

    rendered = _gateway(tmp_path).render_template(
        "save_state_snapshot_array",
        _render_manifest(tmp_path, "save_state_snapshot_array"),
        str(tmp_path / "manifest_index.json"),
    )

    assert "super-secret" not in rendered
    assert "X-Amz-Signature" not in rendered
    assert "export NHMS_SCHEDULER_STATE_INDEX=''" in rendered


@pytest.mark.parametrize(
    "key",
    [
        "NHMS_SCHEDULER_DB_FREE_REQUIRED",
        "NHMS_SCHEDULER_ALLOWED_ROOTS",
        "NHMS_SCHEDULER_STATE_INDEX_BACKEND",
        "NHMS_SCHEDULER_STATE_INDEX",
    ],
)
def test_state_save_array_slurm_env_cannot_override_db_free_state_index_env(
    tmp_path: Path,
    key: str,
) -> None:
    with pytest.raises(GatewayManifestValidationError) as exc_info:
        _gateway(tmp_path).render_template(
            "save_state_snapshot_array",
            {
                **_render_manifest(tmp_path, "save_state_snapshot_array"),
                "slurm_env": {key: "override"},
            },
            str(tmp_path / "manifest_index.json"),
        )

    assert exc_info.value.details["field"] == f"slurm_env.{key}"
    assert exc_info.value.details["reason"] == "canonical_runtime_env"


def test_publish_tiles_template_does_not_render_database_url_secret(tmp_path: Path) -> None:
    secret_database_url = "postgresql://nhms:secret@example.invalid/nhms"
    manifest = {
        **_render_manifest(tmp_path, "publish_tiles"),
        "database_url": secret_database_url,
    }

    with pytest.raises(GatewayManifestValidationError) as exc_info:
        _gateway(tmp_path).render_template(
            "publish_tiles",
            manifest,
            str(tmp_path / "manifest_index.json"),
    )

    assert secret_database_url not in json.dumps(exc_info.value.details)
    assert exc_info.value.details["findings"][0]["field"] == "manifest.[redacted]"


def test_run_shud_forecast_template_rejects_secret_manifest_values_before_render(tmp_path: Path) -> None:
    secret_uri = "s3://user:pass@bucket/prod?token=secret&X-Amz-Signature=abc"
    manifest = {
        **_render_manifest(tmp_path, "run_shud_forecast_array"),
        "object_store_root": secret_uri,
        "object_store_prefix": "s3://user:pass@bucket/prefix?token=secret",
        "account": "friends",
    }

    with pytest.raises(GatewayManifestValidationError) as exc_info:
        _gateway(tmp_path).render_template(
            "run_shud_forecast_array",
            manifest,
            str(tmp_path / "manifest_index.json"),
        )

    assert secret_uri not in json.dumps(exc_info.value.details)
    assert "user:pass@" not in json.dumps(exc_info.value.details)
    assert "token=secret" not in json.dumps(exc_info.value.details)


def test_run_shud_forecast_template_uses_shared_logs_resources_manifest_contract(tmp_path: Path) -> None:
    manifest = {
        **_render_manifest(tmp_path, "run_shud_forecast_array"),
        "object_store_root": str(tmp_path / "object-store"),
        "object_store_prefix": "forecast/cycle_001",
        "account": "friends",
    }

    rendered = _gateway(tmp_path).render_template(
        "run_shud_forecast_array",
        manifest,
        str(tmp_path / "manifest_index.json"),
    )

    assert "#SBATCH --output=" in rendered
    assert "/logs/%A_%a.out" in rendered
    assert "#SBATCH --error=" in rendered
    assert "/logs/%A_%a.err" in rendered
    assert "#SBATCH --cpus-per-task=8" in rendered
    assert "#SBATCH --account=friends" in rendered
    assert "#SBATCH --mem=32G" in rendered
    assert "#SBATCH --time=01:00:00" in rendered
    assert "export SHUD_THREADS=8" in rendered
    assert "export OMP_NUM_THREADS=8" in rendered
    assert "export NHMS_MANIFEST_INDEX=" in rendered
    assert (
        'nhms-shud-runtime execute --manifest-index "$NHMS_MANIFEST_INDEX" '
        '--task-id "${SLURM_ARRAY_TASK_ID:-0}"'
    ) in rendered


def test_download_source_cycle_cli_accepts_template_args(monkeypatch):
    class FakeAdapter:
        config = SimpleNamespace(source_id="gfs")

        def build_manifest(self, cycle_time):
            return {"cycle_time": cycle_time}

        def download_plan(self, manifest):
            return SimpleNamespace(status="succeeded", total_bytes_written=0, retry_count=0, files=[])

    monkeypatch.setattr(data_cli.GFSAdapter, "from_env", staticmethod(lambda: FakeAdapter()))

    _invoke_main(data_cli.main, ["download", "--cycle-time", "2026010100"])


def test_convert_canonical_cli_accepts_template_args(monkeypatch):
    class FakeConverter:
        config = SimpleNamespace(source_id="gfs")
        object_store = SimpleNamespace(uri_for_key=lambda key: f"file:///{key}")

        def convert_manifest_uri(self, manifest_uri):
            return SimpleNamespace(status="succeeded", products=[])

    monkeypatch.setattr(canonical_cli.CanonicalConverter, "from_env", staticmethod(lambda: FakeConverter()))

    _invoke_main(canonical_cli.main, ["convert", "--source-id", "GFS", "--cycle-time", "2026010100"])


def test_run_shud_forecast_sbatch_preflight_uses_shared_manifest_loader(tmp_path: Path) -> None:
    manifest_index_path = _manifest_index(tmp_path)
    rendered = _gateway(tmp_path).render_template(
        "run_shud_forecast_array",
        _render_manifest(tmp_path, "run_shud_forecast_array"),
        str(manifest_index_path),
    )

    assert "from packages.common.manifest_index import load_manifest_entry, resolve_task_id" in rendered
    assert "task_id = resolve_task_id(None)" in rendered
    assert 'entry = load_manifest_entry(os.environ["NHMS_MANIFEST_INDEX"], task_id)' in rendered
    assert 'with open(os.environ["NHMS_MANIFEST_INDEX"]' not in rendered
    assert "json.load(" not in rendered


@pytest.mark.parametrize(
    "job_type",
    ["produce_forcing_array", "run_shud_forecast_array", "parse_output_array"],
)
def test_production_array_templates_do_not_pre_read_manifest_index_with_plain_open(
    tmp_path: Path,
    job_type: str,
) -> None:
    rendered = _gateway(tmp_path).render_template(
        job_type,
        _render_manifest(tmp_path, job_type),
        str(_manifest_index(tmp_path)),
    )

    assert 'with open(os.environ["NHMS_MANIFEST_INDEX"]' not in rendered
    assert "json.load(" not in rendered


@pytest.mark.parametrize(
    ("job_type", "expected_executable"),
    [
        ("produce_forcing_array", "nhms-forcing"),
        ("run_shud_forecast_array", "nhms-shud-runtime"),
        ("parse_output_array", "nhms-parse"),
    ],
)
def test_rendered_template_command_parses_without_error(monkeypatch, tmp_path, job_type, expected_executable):
    manifest_index_path = _manifest_index(tmp_path)
    rendered = _gateway(tmp_path).render_template(
        job_type,
        _render_manifest(tmp_path, job_type),
        str(manifest_index_path),
    )
    argv = _rendered_command_argv(rendered, manifest_index_path=manifest_index_path)

    assert argv[0] == expected_executable
    main = _mock_array_cli_dependencies(monkeypatch, argv[0])
    _invoke_main(main, argv[1:])


def test_forcing_array_cli_accepts_manifest_index(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    class FakeProducer:
        def produce(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="succeeded",
                forcing_version_id="forcing_001",
                forcing_package_uri="file:///forcing",
                checksum="abc",
                station_count=1,
                timestep_count=2,
            )

    monkeypatch.setattr(forcing_cli.ForcingProducer, "from_env", staticmethod(lambda: FakeProducer()))

    _invoke_main(forcing_cli.main, ["produce", "--manifest-index", str(_manifest_index(tmp_path)), "--task-id", "0"])

    assert captured["source_id"] == "gfs"
    assert captured["cycle_time"] == "2026051200"
    assert captured["model_id"] == "model_001"


def test_manifest_index_uses_slurm_array_task_id_when_no_explicit_task_id(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    class FakeProducer:
        def produce(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="succeeded",
                forcing_version_id="forcing_001",
                forcing_package_uri="file:///forcing",
                checksum="abc",
                station_count=1,
                timestep_count=2,
            )

    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "1")
    monkeypatch.setattr(forcing_cli.ForcingProducer, "from_env", staticmethod(lambda: FakeProducer()))

    _invoke_main(forcing_cli.main, ["produce", "--manifest-index", str(_manifest_index(tmp_path))])

    assert captured["source_id"] == "IFS"
    assert captured["model_id"] == "model_002"


def test_manifest_index_defaults_to_zero_when_no_task_id_source(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    class FakeProducer:
        def produce(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="succeeded",
                forcing_version_id="forcing_001",
                forcing_package_uri="file:///forcing",
                checksum="abc",
                station_count=1,
                timestep_count=2,
            )

    monkeypatch.delenv("SLURM_ARRAY_TASK_ID", raising=False)
    monkeypatch.setattr(forcing_cli.ForcingProducer, "from_env", staticmethod(lambda: FakeProducer()))

    _invoke_main(forcing_cli.main, ["produce", "--manifest-index", str(_manifest_index(tmp_path))])

    assert captured["source_id"] == "gfs"
    assert captured["model_id"] == "model_001"


def test_manifest_index_invalid_slurm_array_task_id_raises(monkeypatch):
    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "abc")

    with pytest.raises(ManifestValidationError):
        resolve_task_id(None)


def test_runtime_array_cli_accepts_manifest_index(monkeypatch, tmp_path):
    captured: dict[str, str] = {}

    class FakeRuntime:
        def execute_manifest_path(self, manifest: str):
            captured["manifest"] = manifest
            return SimpleNamespace(
                run_id="run_001",
                status="succeeded",
                output_uri="file:///output",
                log_uri="file:///log",
                rivqdown_file="rivqdown.txt",
            )

    monkeypatch.setattr(runtime_cli.SHUDRuntime, "from_env", staticmethod(lambda dry_run=False: FakeRuntime()))

    _invoke_main(runtime_cli.main, ["execute", "--manifest-index", str(_manifest_index(tmp_path)), "--task-id", "0"])

    assert captured["manifest"] == str(tmp_path / "workspace" / "runs" / "run_001" / "input" / "manifest.json")


def test_runtime_array_cli_fails_stably_when_runtime_manifest_is_missing(tmp_path, capsys):
    manifest_index = _manifest_index(tmp_path)

    _invoke_main_expect_failure(
        runtime_cli.main,
        ["execute", "--manifest-index", str(manifest_index), "--task-id", "0", "--dry-run"],
    )

    captured = capsys.readouterr()
    assert "RUNTIME_MANIFEST_MISSING:" in captured.err
    assert "run_001" in captured.err


def test_runtime_array_cli_rejects_symlinked_runtime_manifest(tmp_path, capsys):
    manifest_index = _manifest_index(tmp_path)
    manifest_path = tmp_path / "workspace" / "runs" / "run_001" / "input" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    target = tmp_path / "outside_manifest.json"
    target.write_text("{}", encoding="utf-8")
    try:
        manifest_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is not supported: {exc}")

    _invoke_main_expect_failure(
        runtime_cli.main,
        ["execute", "--manifest-index", str(manifest_index), "--task-id", "0", "--dry-run"],
    )

    captured = capsys.readouterr()
    assert "WORKSPACE_PATH_UNSAFE:" in captured.err
    assert "symlink" in captured.err


def test_runtime_array_cli_uses_manifest_path_override(monkeypatch, tmp_path):
    captured: dict[str, str] = {}
    manifest_path = tmp_path / "workspace" / "runs" / "run_001" / "input" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")
    index_path = tmp_path / "manifest_index.json"
    index_path.write_text(
        json.dumps(
            [
                {
                    "task_id": 0,
                    "model_id": "model_001",
                    "basin_version_id": "basin_001",
                    "river_network_version_id": "river_001",
                    "run_id": "run_001",
                    "workspace_dir": str(tmp_path / "workspace"),
                    "source_id": "GFS",
                    "cycle_time": "2026051200",
                    "manifest_path": str(manifest_path),
                }
            ]
        ),
        encoding="utf-8",
    )

    class FakeRuntime:
        def execute_manifest_path(self, manifest: str):
            captured["manifest"] = manifest
            return SimpleNamespace(
                run_id="run_001",
                status="succeeded",
                output_uri="file:///output",
                log_uri="file:///log",
                rivqdown_file="rivqdown.txt",
            )

    monkeypatch.setattr(runtime_cli.SHUDRuntime, "from_env", staticmethod(lambda dry_run=False: FakeRuntime()))

    _invoke_main(runtime_cli.main, ["execute", "--manifest-index", str(index_path), "--task-id", "0"])

    assert captured["manifest"] == str(manifest_path)


def test_parse_array_cli_accepts_manifest_index(monkeypatch, tmp_path):
    captured: dict[str, str] = {}

    class FakeParser:
        def parse_run(self, run_id: str):
            captured["run_id"] = run_id
            return SimpleNamespace(
                run_id=run_id,
                status="parsed",
                source_file="rivqdown.txt",
                rows_written=10,
                qc_passed=True,
                max_value_m3s=12.5,
            )

    monkeypatch.setattr(output_cli.OutputParser, "from_env", staticmethod(lambda: FakeParser()))

    _invoke_main(output_cli.main, ["shud-output", "--manifest-index", str(_manifest_index(tmp_path)), "--task-id", "0"])

    assert captured["run_id"] == "run_001"


def test_state_save_array_cli_accepts_manifest_index(monkeypatch, tmp_path):
    captured: dict[str, str] = {}

    def fake_save_state_for_run(run_id: str) -> dict[str, object]:
        captured["run_id"] = run_id
        return {"run_id": run_id, "status": "saved"}

    monkeypatch.setattr(state_cli, "save_state_for_run", fake_save_state_for_run)

    _invoke_main(state_cli.main, ["save", "--manifest-index", str(_manifest_index(tmp_path)), "--task-id", "0"])

    assert captured["run_id"] == "run_001"


def test_state_save_array_cli_accepts_legacy_run_id_only_manifest_index(monkeypatch, tmp_path):
    captured: dict[str, str] = {}
    path = tmp_path / "legacy_manifest_index.json"
    path.write_text(json.dumps([{"task_id": 0, "run_id": "run_legacy_001"}]), encoding="utf-8")
    monkeypatch.delenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", raising=False)
    monkeypatch.delenv("NHMS_SCHEDULER_STATE_INDEX_BACKEND", raising=False)

    def fake_save_state_for_run(run_id: str) -> dict[str, object]:
        captured["run_id"] = run_id
        return {"run_id": run_id, "status": "saved"}

    monkeypatch.setattr(state_cli, "save_state_for_run", fake_save_state_for_run)

    _invoke_main(state_cli.main, ["save", "--manifest-index", str(path), "--task-id", "0"])

    assert captured["run_id"] == "run_legacy_001"


def test_worker_does_not_call_downstream_on_manifest_validation_error(monkeypatch, tmp_path):
    path = tmp_path / "manifest_index.json"
    path.write_text(
        json.dumps(
            [
                {
                    "task_id": 0,
                    "model_id": "model_001",
                    "basin_version_id": "basin_001",
                    "run_id": "run_001",
                    "workspace_dir": str(tmp_path / "workspace"),
                    "source_id": "GFS",
                    "cycle_time": "2026051200",
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        forcing_cli.ForcingProducer,
        "from_env",
        staticmethod(lambda: (_ for _ in ()).throw(AssertionError("should not be called"))),
    )

    _invoke_main_expect_failure(forcing_cli.main, ["produce", "--manifest-index", str(path)])


def test_different_task_ids_invoke_worker_with_different_contexts(monkeypatch, tmp_path):
    captured: list[dict[str, Any]] = []

    class FakeProducer:
        def produce(self, **kwargs):
            captured.append(dict(kwargs))
            return SimpleNamespace(
                status="succeeded",
                forcing_version_id="forcing_001",
                forcing_package_uri="file:///forcing",
                checksum="abc",
                station_count=1,
                timestep_count=2,
            )

    monkeypatch.setattr(forcing_cli.ForcingProducer, "from_env", staticmethod(lambda: FakeProducer()))
    manifest_index = _manifest_index(tmp_path)

    _invoke_main(forcing_cli.main, ["produce", "--manifest-index", str(manifest_index), "--task-id", "0"])
    _invoke_main(forcing_cli.main, ["produce", "--manifest-index", str(manifest_index), "--task-id", "1"])

    assert captured[0]["source_id"] == "gfs"
    assert captured[1]["source_id"] == "IFS"
    assert captured[0]["model_id"] == "model_001"
    assert captured[1]["model_id"] == "model_002"


def test_non_array_templates_use_existing_cli_args(tmp_path):
    gateway = _gateway(tmp_path)

    convert = gateway.render_template(
        "convert_canonical",
        _render_manifest(tmp_path, "convert_canonical"),
        str(tmp_path / "manifest_index.json"),
    )

    assert 'nhms-canonical convert --source-id "${NHMS_SOURCE_ID:-GFS}" --cycle-time "$NHMS_CYCLE_TIME"' in convert


def test_manifest_validation_rejects_missing_required_field(tmp_path):
    path = tmp_path / "manifest_index.json"
    path.write_text(
        json.dumps(
            [
                {
                    "task_id": 0,
                    "model_id": "model_001",
                    "run_id": "run_001",
                    "workspace_dir": str(tmp_path / "workspace"),
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError):
        load_manifest_entry(str(path), 0)


def test_manifest_validation_rejects_out_of_range_task_id(tmp_path):
    with pytest.raises(ManifestValidationError):
        load_manifest_entry(str(_manifest_index(tmp_path)), 2)


def test_manifest_validation_rejects_empty_manifest_index(tmp_path):
    path = tmp_path / "manifest_index.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ManifestValidationError):
        load_manifest_entry(str(path), 0)


def test_manifest_index_rejects_symlink(tmp_path):
    target = _manifest_index(tmp_path)
    link = tmp_path / "manifest_index_link.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is not supported: {exc}")

    with pytest.raises(ManifestValidationError, match="symlink"):
        load_manifest_entry(str(link), 0)


def test_manifest_index_rejects_oversized_file(tmp_path):
    path = tmp_path / "manifest_index.json"
    with path.open("wb") as file:
        file.truncate(50_000_001)

    with pytest.raises(ManifestValidationError, match="size limit"):
        load_manifest_entry(str(path), 0)


def test_manifest_entry_rejects_path_traversal_run_id(tmp_path):
    path = tmp_path / "manifest_index.json"
    path.write_text(
        json.dumps(
            [
                {
                    "task_id": 0,
                    "model_id": "model_001",
                    "basin_version_id": "basin_001",
                    "river_network_version_id": "river_001",
                    "run_id": "../evil",
                    "workspace_dir": str(tmp_path / "workspace"),
                    "source_id": "GFS",
                    "cycle_time": "2026051200",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError, match="unsafe characters"):
        load_manifest_entry(str(path), 0)


def test_manifest_entry_rejects_path_traversal_manifest_path(tmp_path):
    path = tmp_path / "manifest_index.json"
    path.write_text(
        json.dumps(
            [
                {
                    "task_id": 0,
                    "model_id": "model_001",
                    "basin_version_id": "basin_001",
                    "river_network_version_id": "river_001",
                    "run_id": "run_001",
                    "workspace_dir": str(tmp_path / "workspace"),
                    "source_id": "GFS",
                    "cycle_time": "2026051200",
                    "manifest_path": "runs/run_001/../evil/manifest.json",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError, match="path traversal"):
        load_manifest_entry(str(path), 0)


def test_manifest_validation_returns_valid_entry(tmp_path):
    entry = load_manifest_entry(str(_manifest_index(tmp_path)), 0)

    assert entry["run_id"] == "run_001"
    assert entry["model_id"] == "model_001"


def test_two_array_task_ids_consume_different_manifest_entries(tmp_path):
    first = load_manifest_entry(str(_manifest_index(tmp_path)), 0)
    second = load_manifest_entry(str(_manifest_index(tmp_path)), 1)

    assert first["run_id"] == "run_001"
    assert second["run_id"] == "run_002"
    assert first["model_id"] != second["model_id"]
    assert first["run_id"] != second["run_id"]


def test_explicit_task_id_overrides_slurm_array_task_id(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    class FakeProducer:
        def produce(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="succeeded",
                forcing_version_id="forcing_001",
                forcing_package_uri="file:///forcing",
                checksum="abc",
                station_count=1,
                timestep_count=2,
            )

    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "1")
    monkeypatch.setattr(forcing_cli.ForcingProducer, "from_env", staticmethod(lambda: FakeProducer()))

    _invoke_main(forcing_cli.main, ["produce", "--manifest-index", str(_manifest_index(tmp_path)), "--task-id", "0"])

    assert captured["model_id"] == "model_001"


def test_publish_tiles_command_exists(capsys):
    exit_code = _main_exit_code(orchestrator_cli.main, ["publish-tiles", "--cycle-id", "missing_cycle"])

    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["status"] == "failed_publish"
    assert payload["cycle_id"] == "missing_cycle"
    assert payload["error_code"] in {"WORKSPACE_ROOT_MISSING", "OBJECT_STORE_ROOT_MISSING", "DATABASE_URL_MISSING"}
    assert payload["layers"] == []


def test_publish_tiles_compute_control_without_database_url_defers_to_node27(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "compute_control")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    exit_code = _main_exit_code(orchestrator_cli.main, ["publish-tiles", "--cycle-id", "ifs_2026062712"])

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["status"] == "deferred_to_node27_ingest"
    assert payload["cycle_id"] == "ifs_2026062712"
    assert payload["layers"] == []
    assert payload["artifacts"] == []
    assert payload["lineage"]["reason_code"] == "NODE22_DB_FREE_PUBLISH_DEFERRED"
    assert payload["lineage"]["deferred_to"] == "node27_autopipeline"


def test_publish_tiles_without_database_url_defers_even_without_service_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.delenv("NHMS_SERVICE_ROLE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    exit_code = _main_exit_code(orchestrator_cli.main, ["publish-tiles", "--cycle-id", "ifs_2026062712"])

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["status"] == "deferred_to_node27_ingest"
    assert payload["lineage"]["reason_code"] == "NODE22_DB_FREE_PUBLISH_DEFERRED"
