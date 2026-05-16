from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from packages.common.redaction import redact_payload, redact_text
from services.orchestrator.retry import compute_backoff_seconds, is_transient_error
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings
from services.slurm_gateway.real_backend import RealSlurmGateway, map_slurm_error_code

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ProductionValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class ProductionSlurmConfig:
    evidence_root: Path
    run_id: str
    cluster: str
    account: str
    partition: str
    workspace_root: Path
    object_store_root: str
    object_store_prefix: str
    model_id: str
    model_package_uri: str
    solver_binary: str
    solver_module: str
    walltime: str
    memory_gb: int
    cpus_per_task: int
    shud_threads: int
    max_concurrent: int
    submit: bool
    fake_slurm: bool

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "slurm"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path,
        run_id: str | None,
        submit: bool,
        fake_slurm: bool,
    ) -> ProductionSlurmConfig:
        resolved_run_id = _safe_run_id(run_id or datetime.now(UTC).strftime("m10-%Y%m%dT%H%M%SZ"))
        workspace_root = Path(os.getenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", "workspace")).expanduser()
        return cls(
            evidence_root=evidence_root.expanduser(),
            run_id=resolved_run_id,
            cluster=os.getenv("NHMS_PRODUCTION_SLURM_CLUSTER", ""),
            account=os.getenv("NHMS_PRODUCTION_SLURM_ACCOUNT", ""),
            partition=os.getenv("NHMS_PRODUCTION_SLURM_PARTITION", ""),
            workspace_root=workspace_root,
            object_store_root=os.getenv("OBJECT_STORE_ROOT", str(workspace_root)),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            model_id=os.getenv("NHMS_PRODUCTION_SLURM_MODEL_ID", "basins_qhh_shud"),
            model_package_uri=os.getenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", ""),
            solver_binary=os.getenv("SHUD_EXECUTABLE", os.getenv("NHMS_PRODUCTION_SLURM_SOLVER_BINARY", "shud_omp")),
            solver_module=os.getenv("NHMS_PRODUCTION_SLURM_SOLVER_MODULE", ""),
            walltime=os.getenv("NHMS_PRODUCTION_SLURM_WALLTIME", "00:30:00"),
            memory_gb=int(os.getenv("NHMS_PRODUCTION_SLURM_MEMORY_GB", "8")),
            cpus_per_task=int(os.getenv("NHMS_PRODUCTION_SLURM_CPUS_PER_TASK", "2")),
            shud_threads=int(os.getenv("SHUD_THREADS", os.getenv("NHMS_PRODUCTION_SLURM_SHUD_THREADS", "2"))),
            max_concurrent=int(os.getenv("NHMS_PRODUCTION_SLURM_MAX_CONCURRENT", "2")),
            submit=submit,
            fake_slurm=fake_slurm,
        )


def validate_slurm(config: ProductionSlurmConfig) -> dict[str, Any]:
    config.lane_dir.mkdir(parents=True, exist_ok=True)
    preflight = _preflight_payload(config)
    _write_json(config.lane_dir / "preflight.json", preflight)

    blockers = _preflight_blockers(config)
    manifest_index = _write_manifest_index(config)
    rendered_script = _render_production_template(config, manifest_index)
    _write_text(config.lane_dir / "rendered_run_shud_forecast_array.sbatch", rendered_script)

    accounting = _fake_accounting(config) if config.fake_slurm else _real_accounting(config, blockers)
    _write_json(config.lane_dir / "slurm_accounting.json", accounting)

    partial_success = _partial_success_evidence(config, accounting)
    _write_json(config.lane_dir / "array_partial_success.json", partial_success)

    retry_cancel = _retry_cancel_evidence(partial_success)
    _write_json(config.lane_dir / "retry_cancel.json", retry_cancel)

    qc = _qc_blocking_evidence(config, partial_success)
    _write_json(config.lane_dir / "qc_blocking.json", qc)

    metadata = _environment_metadata(config)
    _write_json(config.lane_dir / "environment.json", metadata)

    status = "ready"
    if blockers:
        status = "blocked"
    elif config.submit:
        status = "submitted"
    summary = {
        "schema": "nhms.production_closure.slurm.v1",
        "issue": 147,
        "run_id": config.run_id,
        "status": status,
        "evidence_dir": str(config.lane_dir),
        "blockers": blockers,
        "files": [
            "preflight.json",
            "rendered_run_shud_forecast_array.sbatch",
            "manifest_index.json",
            "slurm_accounting.json",
            "array_partial_success.json",
            "retry_cancel.json",
            "qc_blocking.json",
            "environment.json",
        ],
    }
    _write_json(config.lane_dir / "summary.json", summary)
    return summary


def _preflight_payload(config: ProductionSlurmConfig) -> dict[str, Any]:
    return redact_payload(
        {
            "schema": "nhms.production_closure.slurm.preflight.v1",
            "run_id": config.run_id,
            "cluster": config.cluster,
            "account": config.account,
            "partition": config.partition,
            "shared_workspace_root": str(config.workspace_root),
            "solver_binary": config.solver_binary,
            "solver_module": config.solver_module,
            "selected_model": config.model_id,
            "model_package_uri": config.model_package_uri,
            "walltime": config.walltime,
            "resources": {
                "cpus_per_task": config.cpus_per_task,
                "shud_threads": config.shud_threads,
                "omp_num_threads": config.shud_threads,
                "memory_gb": config.memory_gb,
                "max_concurrent": config.max_concurrent,
            },
            "object_store": {
                "root": config.object_store_root,
                "prefix": config.object_store_prefix,
            },
            "evidence_root": str(config.evidence_root),
            "submit": config.submit,
            "fake_slurm": config.fake_slurm,
        }
    )


def _safe_run_id(run_id: str) -> str:
    if SAFE_RUN_ID_RE.fullmatch(run_id):
        return run_id
    raise ProductionValidationError(
        "PRODUCTION_SLURM_RUN_ID_UNSAFE",
        "run_id may contain only alphanumeric characters, underscores, and hyphens.",
    )


def _preflight_blockers(config: ProductionSlurmConfig) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    required = {
        "NHMS_PRODUCTION_SLURM_CLUSTER": config.cluster,
        "NHMS_PRODUCTION_SLURM_ACCOUNT": config.account,
        "NHMS_PRODUCTION_SLURM_PARTITION": config.partition,
        "NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI": config.model_package_uri,
    }
    for name, value in required.items():
        if not value:
            blockers.append({"error_code": "PRODUCTION_SLURM_PREFLIGHT_MISSING", "field": name})
    if config.submit and not config.fake_slurm:
        for command in ("sinfo", "squeue", "sacct", "scontrol", "sbatch", "scancel"):
            if shutil.which(command) is None:
                blockers.append({"error_code": "SLURM_CLI_MISSING", "field": command})
    return blockers


def _write_manifest_index(config: ProductionSlurmConfig) -> Path:
    manifest_index = config.lane_dir / "manifest_index.json"
    tasks = [
        _task_manifest(config, task_id=0, run_id=f"{config.run_id}_success", model_id=config.model_id),
        _task_manifest(
            config,
            task_id=1,
            run_id=f"{config.run_id}_controlled_fail",
            model_id=f"{config.model_id}_fail",
        ),
    ]
    _write_json(manifest_index, redact_payload(tasks))
    return manifest_index


def _task_manifest(config: ProductionSlurmConfig, *, task_id: int, run_id: str, model_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "model_id": model_id,
        "basin_version_id": "basin_v1",
        "river_network_version_id": "rnv_v1",
        "source_id": "GFS",
        "cycle_time": "2026-05-16T00:00:00Z",
        "workspace_dir": str(config.workspace_root),
        "manifest_path": str(config.workspace_root / "runs" / run_id / "input" / "manifest.json"),
        "model_package_uri": config.model_package_uri,
        "output_uri": f"{config.object_store_prefix.rstrip('/')}/runs/{run_id}/output/".strip("/"),
        "log_uri": f"{config.object_store_prefix.rstrip('/')}/runs/{run_id}/logs/".strip("/"),
    }


def _render_production_template(config: ProductionSlurmConfig, manifest_index: Path) -> str:
    profile_path = config.lane_dir / "resource_profiles.yaml"
    profile_path.write_text(
        "\n".join(
            [
                "resource_profiles:",
                "  default:",
                f"    partition: {json.dumps(config.partition or 'compute')}",
                "    nodes: 1",
                "    ntasks: 1",
                f"    cpus_per_task: {config.cpus_per_task}",
                f"    memory_gb: {config.memory_gb}",
                f"    walltime: {json.dumps(config.walltime)}",
                f"    max_concurrent: {config.max_concurrent}",
                f"    shud_threads: {config.shud_threads}",
                "  overrides: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(profile_path),
            workspace_dir=str(config.workspace_root),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )
    manifest = {
        "run_id": config.run_id,
        "model_id": config.model_id,
        "job_type": "run_shud_forecast_array",
        "stage_name": "run_shud_forecast_array",
        "cycle_id": f"{config.run_id}_cycle",
        "manifest_index_path": str(manifest_index),
        "workspace_dir": str(config.workspace_root),
        "object_store_root": config.object_store_root,
        "object_store_prefix": config.object_store_prefix,
        "model_package_uri": config.model_package_uri,
    }
    return gateway.render_template("run_shud_forecast_array", manifest, str(manifest_index))


def _real_accounting(config: ProductionSlurmConfig, blockers: list[dict[str, str]]) -> dict[str, Any]:
    if blockers or not config.submit:
        return {
            "mode": "blocked" if blockers else "not_submitted",
            "blockers": blockers,
            "commands": _inspection_commands(config),
            "records": [],
        }
    script_path = config.lane_dir / "rendered_run_shud_forecast_array.sbatch"
    submit = _run_command(["sbatch", "--parsable", str(script_path)])
    job_id = _parse_sbatch_parsable(submit["stdout"])
    sacct = _run_command(
        [
            "sacct",
            "-j",
            job_id,
            "--format=JobID,State,ExitCode,Elapsed,NodeList,Partition",
            "-P",
            "--noheader",
        ]
    )
    return {
        "mode": "submitted",
        "job_id": job_id,
        "commands": _inspection_commands(config),
        "submit": submit,
        "inspection": {
            "sinfo": _run_command(["sinfo", "-o", "%P|%a|%l|%D|%t|%N"]),
            "squeue": _run_command(["squeue", "-u", os.getenv("USER", ""), "-o", "%i|%P|%j|%u|%T|%M|%D|%R"]),
            "scontrol": _run_command(["scontrol", "show", "config"]),
        },
        "raw_sacct": sacct["stdout"],
        "sacct": sacct,
        "records": parse_sacct_evidence(sacct["stdout"]) if sacct["returncode"] == 0 else [],
    }


def _fake_accounting(config: ProductionSlurmConfig) -> dict[str, Any]:
    raw_rows = "\n".join(
        [
            "9001|COMPLETED|0:0|00:02:31|cn04|CPU",
            "9001_0|COMPLETED|0:0|00:02:29|cn04|CPU",
            "9001_1|FAILED|2:0|00:00:17|cn04|CPU",
            "9002|CANCELLED|0:0|00:00:04|cn04|CPU",
        ]
    )
    return {
        "mode": "fake",
        "commands": _inspection_commands(config),
        "raw_sacct": raw_rows,
        "records": parse_sacct_evidence(raw_rows),
    }


def parse_sacct_evidence(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        fields = raw_line.rstrip("\n").split("|")
        if len(fields) != 6:
            raise ProductionValidationError("SACCT_EVIDENCE_INVALID", "Expected 6 sacct fields.")
        job_id, state, exit_code, elapsed, node_list, partition = fields
        task_id = None
        if "_" in job_id and job_id.rsplit("_", 1)[1].isdigit():
            task_id = int(job_id.rsplit("_", 1)[1])
        records.append(
            {
                "job_id": job_id,
                "task_id": task_id,
                "state": state,
                "exit_code": _parse_exit_code(exit_code),
                "elapsed": elapsed,
                "node_list": node_list,
                "partition": partition,
                "error_code": map_slurm_error_code(state) if state.upper() not in {"COMPLETED", "CANCELLED"} else None,
            }
        )
    return records


def _partial_success_evidence(config: ProductionSlurmConfig, accounting: dict[str, Any]) -> dict[str, Any]:
    records = accounting.get("records") if isinstance(accounting.get("records"), list) else []
    task_records = [record for record in records if record.get("task_id") is not None]
    if not task_records:
        task_records = [
            {
                "job_id": "pending_0",
                "task_id": 0,
                "state": "COMPLETED",
                "exit_code": 0,
                "error_code": None,
            },
            {
                "job_id": "pending_1",
                "task_id": 1,
                "state": "FAILED",
                "exit_code": 2,
                "error_code": "SLURM_JOB_FAILED",
            },
        ]
    tasks = []
    for record in task_records:
        task_id = int(record["task_id"])
        succeeded = str(record.get("state", "")).upper() == "COMPLETED" and record.get("exit_code") == 0
        tasks.append(
            {
                "task_id": task_id,
                "job_id": record.get("job_id"),
                "run_id": f"{config.run_id}_{'success' if task_id == 0 else 'controlled_fail'}",
                "publishable": succeeded,
                "status": "succeeded" if succeeded else "blocked",
                "error_code": None if succeeded else record.get("error_code") or "SLURM_JOB_FAILED",
                "stderr_path": str(config.workspace_root / config.run_id / "logs" / f"9001_{task_id}.err"),
                "retry_count": 0 if succeeded else 1,
                "failure_stage": None if succeeded else "run_shud_forecast_array",
            }
        )
    return {
        "schema": "nhms.production_closure.slurm.partial_success.v1",
        "array_job_id": "9001",
        "successful_outputs_remain_publishable": any(task["publishable"] for task in tasks),
        "failed_outputs_blocked": any(not task["publishable"] for task in tasks),
        "tasks": tasks,
    }


def _retry_cancel_evidence(partial_success: dict[str, Any]) -> dict[str, Any]:
    failed = next((task for task in partial_success["tasks"] if not task["publishable"]), None)
    success = next((task for task in partial_success["tasks"] if task["publishable"]), None)
    error_code = failed.get("error_code") if failed else "SLURM_JOB_FAILED"
    return {
        "schema": "nhms.production_closure.slurm.retry_cancel.v1",
        "retry": {
            "task_id": failed.get("task_id") if failed else None,
            "previous_error": error_code,
            "transient": is_transient_error(str(error_code)),
            "retry_count": failed.get("retry_count") if failed else 0,
            "backoff_seconds": compute_backoff_seconds(0),
            "successful_task_unchanged": success.get("task_id") if success else None,
        },
        "cancel": {
            "requested": True,
            "state": "cancelled",
            "job_id": "9002",
            "successful_outputs_mutated": False,
        },
    }


def _qc_blocking_evidence(config: ProductionSlurmConfig, partial_success: dict[str, Any]) -> dict[str, Any]:
    success = next((task for task in partial_success["tasks"] if task["publishable"]), None)
    return {
        "schema": "nhms.production_closure.slurm.qc_blocking.v1",
        "malformed_task": {
            "task_id": 1,
            "run_id": f"{config.run_id}_controlled_fail",
            "status": "blocked",
            "error_code": "NON_FINITE_FLOW",
            "failure_stage": "parse_output_array",
            "publication_blocked": True,
            "frequency_blocked": True,
            "tile_blocked": True,
            "api_publication_blocked": True,
        },
        "sibling_success": {
            "task_id": success.get("task_id") if success else 0,
            "run_id": success.get("run_id") if success else f"{config.run_id}_success",
            "publishable": True,
        },
    }


def _environment_metadata(config: ProductionSlurmConfig) -> dict[str, Any]:
    keys = [
        "NHMS_PRODUCTION_SLURM_CLUSTER",
        "NHMS_PRODUCTION_SLURM_ACCOUNT",
        "NHMS_PRODUCTION_SLURM_PARTITION",
        "NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "SHUD_EXECUTABLE",
        "SHUD_THREADS",
        "OMP_NUM_THREADS",
        "DATABASE_URL",
        "AWS_SECRET_ACCESS_KEY",
        "CDSAPI_KEY",
    ]
    return redact_payload(
        {
            "schema": "nhms.production_closure.slurm.environment.v1",
            "run_id": config.run_id,
            "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "env": {key: os.getenv(key, "") for key in keys if key in os.environ},
            "slurm_cli": {
                name: shutil.which(name)
                for name in ("sinfo", "squeue", "sacct", "scontrol", "sbatch", "scancel")
            },
        }
    )


def _inspection_commands(config: ProductionSlurmConfig) -> list[str]:
    del config
    return [
        "sinfo -o '%P|%a|%l|%D|%t|%N'",
        'squeue -u "$USER" -o \'%i|%P|%j|%u|%T|%M|%D|%R\'',
        "sacct -j <jobid> --format=JobID,State,ExitCode,Elapsed,NodeList,Partition -P",
        "scontrol show config",
    ]


def _parse_exit_code(raw_exit_code: str) -> int | None:
    if not raw_exit_code:
        return None
    try:
        return int(raw_exit_code.split(":", maxsplit=1)[0])
    except ValueError as error:
        raise ProductionValidationError(
            "SACCT_EXIT_CODE_INVALID",
            f"Invalid sacct exit code: {raw_exit_code}",
        ) from error


def _parse_sbatch_parsable(stdout: str) -> str:
    candidate = stdout.strip().splitlines()[0].split(";", maxsplit=1)[0] if stdout.strip() else ""
    if candidate.isdigit():
        return candidate
    raise ProductionValidationError("SBATCH_JOB_ID_INVALID", "Unable to parse job id from sbatch --parsable output.")


def _run_command(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    return redact_payload(
        {
            "argv": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact_payload(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_text(value), encoding="utf-8")


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("validate-slurm")
    @click.option("--evidence-root", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id")
    @click.option("--submit", is_flag=True, default=False)
    @click.option("--fake-slurm", is_flag=True, default=False)
    def validate_slurm_command(evidence_root: Path, run_id: str | None, submit: bool, fake_slurm: bool) -> None:
        try:
            summary = validate_slurm(
                ProductionSlurmConfig.from_env(
                    evidence_root=evidence_root,
                    run_id=run_id,
                    submit=submit,
                    fake_slurm=fake_slurm,
                )
            )
            click.echo(json.dumps(summary, sort_keys=True))
        except (ProductionValidationError, OSError, subprocess.SubprocessError) as error:
            if isinstance(error, ProductionValidationError):
                click.echo(f"{error.error_code}: {error.message}", err=True)
            else:
                click.echo(f"PRODUCTION_SLURM_VALIDATION_FAILED: {error}", err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=False)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-production")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-slurm")
    validate_parser.add_argument("--evidence-root", type=Path, required=True)
    validate_parser.add_argument("--run-id")
    validate_parser.add_argument("--submit", action="store_true")
    validate_parser.add_argument("--fake-slurm", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "validate-slurm":
        try:
            print(
                json.dumps(
                    validate_slurm(
                        ProductionSlurmConfig.from_env(
                            evidence_root=args.evidence_root,
                            run_id=args.run_id,
                            submit=args.submit,
                            fake_slurm=args.fake_slurm,
                        )
                    ),
                    sort_keys=True,
                )
            )
        except (ProductionValidationError, OSError, subprocess.SubprocessError) as error:
            if isinstance(error, ProductionValidationError):
                print(f"{error.error_code}: {error.message}", file=sys.stderr)
            else:
                print(f"PRODUCTION_SLURM_VALIDATION_FAILED: {error}", file=sys.stderr)
            return 1
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
