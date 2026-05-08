from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from services.slurm_gateway.config import SlurmGatewaySettings
from services.slurm_gateway.gateway import (
    ConfigurationError,
    ManifestValidationError,
    SlurmCommandError,
    SlurmGateway,
    SlurmGatewayError,
    SlurmJobNotFoundError,
    SlurmParseError,
    SlurmTimeoutError,
    SlurmValidationError,
    TemplateNotFoundError,
    TemplateSecurityError,
)
from services.slurm_gateway.models import (
    TERMINAL_STATUSES,
    ResetRequest,
    ResetResponse,
    SlurmHealthResponse,
    SlurmJobRecord,
    SlurmJobStatus,
    SlurmLogsResponse,
    SubmitJobRequest,
)

LOGGER = logging.getLogger(__name__)

SBATCH_JOB_ID_RE = re.compile(r"Submitted batch job (\d+)")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHELL_META_RE = re.compile(r"[;|&$`<>\n\r]")

STRICT_IDENTIFIER_FIELDS = {
    "basin_id",
    "basin_version_id",
    "cycle_id",
    "job_type",
    "model_id",
    "river_network_version_id",
    "run_id",
    "source_id",
    "stage",
    "stage_name",
    "task_id",
}

REQUIRED_RESOURCE_FIELDS = {
    "partition",
    "nodes",
    "ntasks",
    "cpus_per_task",
    "memory_gb",
    "walltime",
    "max_concurrent",
    "shud_threads",
}

SLURM_STATE_MAP = {
    "PENDING": SlurmJobStatus.SUBMITTED,
    "REQUEUED": SlurmJobStatus.SUBMITTED,
    "RUNNING": SlurmJobStatus.RUNNING,
    "COMPLETED": SlurmJobStatus.SUCCEEDED,
    "FAILED": SlurmJobStatus.FAILED,
    "TIMEOUT": SlurmJobStatus.FAILED,
    "NODE_FAIL": SlurmJobStatus.FAILED,
    "OUT_OF_MEMORY": SlurmJobStatus.FAILED,
    "CANCELLED": SlurmJobStatus.CANCELLED,
}


class RealSlurmGateway(SlurmGateway):
    def __init__(self, settings: SlurmGatewaySettings) -> None:
        self.settings = settings
        self._jobs: dict[str, SlurmJobRecord] = {}

    def submit_job(self, request: SubmitJobRequest) -> SlurmJobRecord:
        manifest = request.normalized_manifest()
        job_type = request.resolved_job_type()
        if job_type:
            manifest["job_type"] = job_type
        self._require_manifest_fields(manifest, ["run_id", "model_id", "job_type"])
        self._validate_manifest(manifest)

        rendered_script = self.render_template(str(manifest["job_type"]), manifest)
        job_id = self._submit_rendered_script(rendered_script)
        now = self._now()
        record = SlurmJobRecord(
            job_id=job_id,
            run_id=str(manifest["run_id"]),
            model_id=str(manifest["model_id"]),
            status=SlurmJobStatus.SUBMITTED,
            submitted_at=now,
            updated_at=now,
            manifest=manifest,
        )
        self._jobs[job_id] = record
        return record.model_copy(deep=True)

    def submit_job_array(
        self,
        job_type: str | SubmitJobRequest | Mapping[str, Any],
        cycle_id: str | None = None,
        stage_name: str | None = None,
        tasks: Sequence[Mapping[str, Any]] | None = None,
        manifests: Sequence[Mapping[str, Any]] | None = None,
        manifest: Mapping[str, Any] | None = None,
    ) -> SlurmJobRecord:
        job_type, cycle_id, stage_name, task_list, base_manifest = self._normalize_array_request(
            job_type=job_type,
            cycle_id=cycle_id,
            stage_name=stage_name,
            tasks=tasks,
            manifests=manifests,
            manifest=manifest,
        )
        if not task_list:
            raise SlurmValidationError("Cannot submit array job with 0 tasks")

        manifest_index_path = self.write_manifest_index(cycle_id, stage_name, task_list)
        first_task = dict(task_list[0])
        model_id = str(first_task.get("model_id") or base_manifest.get("model_id") or "")
        profile = self.resolve_resource_profile(model_id)
        max_concurrent = int(profile["max_concurrent"])
        if max_concurrent < 1:
            raise SlurmValidationError("max_concurrent must be \u2265 1")
        task_count = len(task_list)
        effective_max_concurrent = min(max_concurrent, task_count)
        profile["max_concurrent"] = effective_max_concurrent

        render_manifest = {
            **base_manifest,
            "job_type": job_type,
            "stage_name": stage_name,
            "cycle_id": cycle_id,
            "run_id": first_task.get("run_id", base_manifest.get("run_id", f"{cycle_id}_{stage_name}")),
            "model_id": model_id,
            "manifest_index_path": str(manifest_index_path),
            "workspace_dir": str(Path(self.settings.workspace_dir)),
        }
        self._require_manifest_fields(render_manifest, ["run_id", "model_id", "job_type", "cycle_id", "stage_name"])
        self._validate_manifest(render_manifest)
        rendered_script = self.render_template(job_type, render_manifest, str(manifest_index_path), profile=profile)

        array_spec = None if task_count == 1 else f"0-{task_count - 1}%{effective_max_concurrent}"
        job_id = self._submit_rendered_script(rendered_script, array_spec=array_spec)
        now = self._now()
        record = SlurmJobRecord(
            job_id=job_id,
            run_id=str(render_manifest["run_id"]),
            model_id=model_id,
            status=SlurmJobStatus.SUBMITTED,
            submitted_at=now,
            updated_at=now,
            manifest={
                **render_manifest,
                "array_task_count": task_count,
                "manifest_index_path": str(manifest_index_path),
                "max_concurrent": effective_max_concurrent,
            },
        )
        self._jobs[job_id] = record
        return record.model_copy(deep=True)

    def get_job_status(self, job_id: str) -> SlurmJobRecord:
        result = self._run_command(
            [
                self._slurm_command("sacct"),
                "--parsable2",
                "--noheader",
                "--format=JobID,State,ExitCode,Start,End",
                f"--jobs={job_id}",
            ]
        )
        record = self._parse_sacct_status(result.stdout, job_id)
        self._jobs[job_id] = record
        return record.model_copy(deep=True)

    def cancel_job(self, job_id: str) -> SlurmJobRecord:
        try:
            self._run_command([self._slurm_command("scancel"), job_id])
        except SlurmCommandError as exc:
            stderr = str((exc.details or {}).get("stderr", ""))
            if "invalid job" in stderr.lower() or "not found" in stderr.lower():
                raise SlurmJobNotFoundError(job_id, {"job_id": job_id, "stderr": stderr}) from exc
            raise

        now = self._now()
        existing = self._jobs.get(job_id)
        if existing is None:
            existing = SlurmJobRecord(
                job_id=job_id,
                run_id="",
                model_id="",
                status=SlurmJobStatus.CANCELLED,
                submitted_at=now,
                finished_at=now,
                updated_at=now,
                exit_code=-1,
            )
        else:
            existing = existing.model_copy(
                update={
                    "status": SlurmJobStatus.CANCELLED,
                    "finished_at": now,
                    "updated_at": now,
                    "exit_code": -1,
                },
                deep=True,
            )
        self._jobs[job_id] = existing
        return existing.model_copy(deep=True)

    def list_jobs(
        self,
        limit: int,
        offset: int,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[SlurmJobRecord]:
        command = [
            self._slurm_command("sacct"),
            "--parsable2",
            "--noheader",
            "--format=JobID,JobName,State,ExitCode,Start,End",
        ]
        if start_time:
            command.append(f"--starttime={start_time}")
        if end_time:
            command.append(f"--endtime={end_time}")
        result = self._run_command(command)
        records = self._parse_sacct_list(result.stdout)
        return [record.model_copy(deep=True) for record in records[offset : offset + limit]]

    def fetch_logs(self, job_id: str) -> SlurmLogsResponse:
        record = self._jobs.get(job_id)
        if record is None:
            try:
                record = self.get_job_status(job_id)
            except SlurmGatewayError:
                record = None

        run_id = record.run_id if record and record.run_id else job_id
        log_path = Path(self.settings.workspace_dir) / run_id / "logs" / f"{job_id}.out"
        logs = ""
        if log_path.exists():
            logs = log_path.read_text(encoding="utf-8")

        complete = bool(record and record.status in TERMINAL_STATUSES)
        return SlurmLogsResponse(job_id=job_id, run_id=run_id, logs=logs, complete=complete)

    def reset(self, request: ResetRequest | None = None) -> ResetResponse:
        del request
        cleared = len(self._jobs)
        self._jobs.clear()
        return ResetResponse(status="ok", cleared=cleared, next_job_id="")

    def health(self) -> SlurmHealthResponse:
        try:
            result = self._run_command([self._slurm_command("sinfo"), "--version"])
        except SlurmGatewayError as exc:
            return SlurmHealthResponse(backend="slurm", version=exc.message, status="unhealthy")
        version = result.stdout.strip() or self.settings.version
        return SlurmHealthResponse(backend="slurm", version=version, status="healthy")

    def load_resource_profiles(self) -> dict[str, Any]:
        path = Path(self.settings.resource_profiles_path).expanduser()
        if not path.exists():
            raise ConfigurationError(
                "Resource profile configuration file does not exist.",
                {"resource_profiles_path": str(path)},
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        profiles = data.get("resource_profiles", data)
        if not isinstance(profiles, dict) or not isinstance(profiles.get("default"), dict):
            raise ConfigurationError("Resource profile configuration must include a default section.")
        overrides = profiles.get("overrides", {})
        if overrides is None:
            profiles["overrides"] = {}
        elif not isinstance(overrides, dict):
            raise ConfigurationError("Resource profile overrides must be a mapping.")
        return profiles

    def resolve_resource_profile(self, model_id: str | None) -> dict[str, Any]:
        profiles = self.load_resource_profiles()
        resolved = dict(profiles["default"])
        overrides = profiles.get("overrides") or {}
        if model_id and isinstance(overrides.get(model_id), dict):
            resolved.update(overrides[model_id])

        missing = sorted(REQUIRED_RESOURCE_FIELDS - set(resolved))
        if missing:
            raise ConfigurationError(
                "Resolved resource profile is missing required fields.",
                {"model_id": model_id, "missing_fields": missing},
            )
        return resolved

    def write_manifest_index(
        self,
        cycle_id: str,
        stage_name: str,
        tasks: Sequence[Mapping[str, Any]],
    ) -> Path:
        if not tasks:
            raise SlurmValidationError("Cannot submit array job with 0 tasks")
        self._validate_manifest({"cycle_id": cycle_id, "stage_name": stage_name})

        entries: list[dict[str, Any]] = []
        for index, task in enumerate(tasks):
            entry = dict(task)
            entry["task_id"] = index
            entry.setdefault("workspace_dir", str(Path(self.settings.workspace_dir)))
            self._require_manifest_fields(entry, ["task_id", "model_id", "basin_version_id", "run_id", "workspace_dir"])
            self._validate_manifest(entry)
            entries.append(entry)

        output_dir = Path(self.settings.workspace_dir) / cycle_id / "manifests"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{stage_name}_index.json"
        output_path.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
        return output_path

    def render_template(
        self,
        job_type: str,
        manifest: Mapping[str, Any],
        manifest_index_path: str = "",
        profile: Mapping[str, Any] | None = None,
    ) -> str:
        template_path = self._resolve_template_path(job_type)
        manifest_dict = dict(manifest)
        manifest_dict.setdefault("job_type", job_type)
        manifest_dict.setdefault("stage_name", manifest_dict.get("stage") or job_type)
        manifest_dict.setdefault("workspace_dir", str(Path(self.settings.workspace_dir)))
        manifest_dict.setdefault("manifest_index_path", manifest_index_path)
        self._validate_manifest(manifest_dict)

        resource_profile = dict(profile or self.resolve_resource_profile(str(manifest_dict.get("model_id") or "")))
        context = {**manifest_dict, **resource_profile}
        context["manifest"] = manifest_dict
        context["manifest_index_path"] = manifest_index_path or str(manifest_dict.get("manifest_index_path") or "")

        environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        template = environment.from_string(template_path.read_text(encoding="utf-8"))
        return template.render(**context)

    def _normalize_array_request(
        self,
        job_type: str | SubmitJobRequest | Mapping[str, Any],
        cycle_id: str | None,
        stage_name: str | None,
        tasks: Sequence[Mapping[str, Any]] | None,
        manifests: Sequence[Mapping[str, Any]] | None,
        manifest: Mapping[str, Any] | None,
    ) -> tuple[str, str, str, list[Mapping[str, Any]], dict[str, Any]]:
        base_manifest: dict[str, Any] = dict(manifest or {})
        if isinstance(job_type, SubmitJobRequest):
            base_manifest.update(job_type.normalized_manifest())
            resolved_job_type = job_type.resolved_job_type()
        elif isinstance(job_type, Mapping):
            base_manifest.update(job_type)
            resolved_job_type = str(base_manifest.get("job_type") or "")
        else:
            resolved_job_type = job_type

        task_source = tasks or manifests or base_manifest.get("tasks") or base_manifest.get("basins")
        if task_source is None:
            task_list: list[Mapping[str, Any]] = []
        else:
            task_list = [dict(item) for item in task_source]

        resolved_cycle_id = cycle_id or base_manifest.get("cycle_id")
        resolved_stage_name = (
            stage_name or base_manifest.get("stage_name") or base_manifest.get("stage") or resolved_job_type
        )
        if not resolved_job_type:
            raise ManifestValidationError("Array job request is missing job_type.", {"missing_fields": ["job_type"]})
        if not resolved_cycle_id:
            raise ManifestValidationError("Array job request is missing cycle_id.", {"missing_fields": ["cycle_id"]})
        if not resolved_stage_name:
            raise ManifestValidationError(
                "Array job request is missing stage_name.",
                {"missing_fields": ["stage_name"]},
            )

        return resolved_job_type, str(resolved_cycle_id), str(resolved_stage_name), task_list, base_manifest

    def _resolve_template_path(self, job_type: str) -> Path:
        template_name = self.settings.job_type_templates.get(job_type)
        if not template_name:
            raise TemplateNotFoundError(job_type)

        template_dir = Path(self.settings.template_dir).expanduser().resolve()
        candidate = (template_dir / template_name).resolve()
        try:
            candidate.relative_to(template_dir)
        except ValueError as exc:
            raise TemplateSecurityError(
                "Resolved sbatch template path is outside the configured template directory.",
                {"job_type": job_type, "template_name": template_name},
            ) from exc
        if not candidate.exists() or not candidate.is_file():
            raise TemplateNotFoundError(job_type)
        return candidate

    def _submit_rendered_script(self, rendered_script: str, array_spec: str | None = None) -> str:
        script_path = self._write_temp_script(rendered_script)
        command = [self._slurm_command("sbatch")]
        if array_spec:
            command.append(f"--array={array_spec}")
        command.append(str(script_path))
        try:
            result = self._run_command(command)
            return self._parse_sbatch_job_id(result.stdout)
        finally:
            script_path.unlink(missing_ok=True)

    def _write_temp_script(self, rendered_script: str) -> Path:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="nhms_",
            suffix=".sbatch",
            delete=False,
        ) as handle:
            handle.write(rendered_script)
            temp_path = Path(handle.name)
        os.chmod(temp_path, 0o700)
        return temp_path

    def _run_command(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.settings.subprocess_timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            LOGGER.error("Slurm command timed out: %s", command[0], exc_info=True)
            raise SlurmTimeoutError(
                f"Slurm command {Path(command[0]).name} timed out after "
                f"{self.settings.subprocess_timeout_seconds} seconds.",
                {"command": command, "timeout_seconds": self.settings.subprocess_timeout_seconds},
            ) from exc
        except FileNotFoundError as exc:
            LOGGER.error("Slurm command was not found: %s", command[0], exc_info=True)
            raise SlurmCommandError(
                f"Slurm command {Path(command[0]).name} was not found.",
                {"command": command, "stderr": str(exc), "returncode": None},
            ) from exc

        if result.returncode != 0:
            LOGGER.error(
                "Slurm command failed: %s returncode=%s stdout=%r stderr=%r",
                command[0],
                result.returncode,
                result.stdout,
                result.stderr,
            )
            raise SlurmCommandError(
                f"Slurm command {Path(command[0]).name} failed with exit code {result.returncode}.",
                {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
            )
        return result

    def _parse_sbatch_job_id(self, stdout: str) -> str:
        match = SBATCH_JOB_ID_RE.search(stdout)
        if not match:
            LOGGER.error("Failed to parse sbatch stdout: %r", stdout)
            raise SlurmParseError("Unable to parse sbatch job id from stdout.", {"stdout": stdout})
        return match.group(1)

    def _parse_sacct_status(self, stdout: str, job_id: str) -> SlurmJobRecord:
        matching_fields: list[str] | None = None
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                continue
            fields = raw_line.rstrip("\n").split("|")
            if len(fields) != 5:
                LOGGER.error("Failed to parse sacct status output: %r", stdout)
                raise SlurmParseError("Unable to parse sacct status output.", {"stdout": stdout})
            if fields[0] == job_id:
                matching_fields = fields
                break

        if matching_fields is None:
            raise SlurmJobNotFoundError(job_id)
        return self._record_from_sacct_fields(matching_fields, job_id)

    def _parse_sacct_list(self, stdout: str) -> list[SlurmJobRecord]:
        records: list[SlurmJobRecord] = []
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                continue
            fields = raw_line.rstrip("\n").split("|")
            if len(fields) != 6:
                LOGGER.error("Failed to parse sacct list output: %r", stdout)
                raise SlurmParseError("Unable to parse sacct list output.", {"stdout": stdout})
            job_id = fields[0]
            if "." in job_id:
                continue
            record = self._record_from_sacct_fields(
                [fields[0], fields[2], fields[3], fields[4], fields[5]],
                job_id,
                job_name=fields[1],
            )
            records.append(record)
        return records

    def _record_from_sacct_fields(
        self,
        fields: Sequence[str],
        job_id: str,
        job_name: str | None = None,
    ) -> SlurmJobRecord:
        state = self._map_slurm_state(fields[1])
        exit_code = self._parse_exit_code(fields[2])
        started_at = self._parse_slurm_datetime(fields[3])
        finished_at = self._parse_slurm_datetime(fields[4]) if state in TERMINAL_STATUSES else None
        existing = self._jobs.get(job_id)
        now = self._now()
        manifest = dict(existing.manifest) if existing else {}
        if job_name:
            manifest["job_name"] = job_name
        return SlurmJobRecord(
            job_id=job_id,
            run_id=existing.run_id if existing else "",
            model_id=existing.model_id if existing else "",
            status=state,
            submitted_at=existing.submitted_at if existing else started_at or now,
            started_at=started_at,
            finished_at=finished_at,
            updated_at=now,
            exit_code=exit_code,
            manifest=manifest,
        )

    def _map_slurm_state(self, raw_state: str) -> SlurmJobStatus:
        normalized = raw_state.strip().upper().split()[0].rstrip("+")
        status = SLURM_STATE_MAP.get(normalized)
        if status is None:
            LOGGER.error("Unsupported Slurm state: %r", raw_state)
            raise SlurmParseError("Unsupported Slurm state in sacct output.", {"state": raw_state})
        return status

    def _parse_exit_code(self, raw_exit_code: str) -> int | None:
        if not raw_exit_code:
            return None
        try:
            return int(raw_exit_code.split(":", maxsplit=1)[0])
        except ValueError as exc:
            raise SlurmParseError("Unable to parse Slurm exit code.", {"exit_code": raw_exit_code}) from exc

    def _parse_slurm_datetime(self, raw_value: str) -> datetime | None:
        value = raw_value.strip()
        if value in {"", "Unknown", "None", "N/A"}:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _slurm_command(self, name: str) -> str:
        if not self.settings.slurm_bin_path:
            return name
        return str(Path(self.settings.slurm_bin_path) / name)

    def _require_manifest_fields(self, manifest: Mapping[str, Any], fields: Sequence[str]) -> None:
        missing = [field for field in fields if manifest.get(field) in (None, "")]
        if missing:
            raise ManifestValidationError(
                "Job manifest is missing required fields.",
                {"missing_fields": missing},
            )

    def _validate_manifest(self, value: Any, path: str = "manifest") -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if not isinstance(key, str) or not SAFE_KEY_RE.fullmatch(key):
                    raise ManifestValidationError("Manifest contains an unsafe field name.", {"field": str(key)})
                self._validate_manifest(nested, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, nested in enumerate(value):
                self._validate_manifest(nested, f"{path}[{index}]")
            return
        if isinstance(value, str):
            if SHELL_META_RE.search(value):
                raise ManifestValidationError(
                    "Manifest field contains shell metacharacters.",
                    {"field": path, "value": value},
                )
            field_name = path.rsplit(".", maxsplit=1)[-1]
            if field_name in STRICT_IDENTIFIER_FIELDS and not SAFE_IDENTIFIER_RE.fullmatch(value):
                raise ManifestValidationError(
                    "Manifest identifier fields may contain only alphanumeric characters, underscores, and hyphens.",
                    {"field": path, "value": value},
                )
            return
        if value is None or isinstance(value, bool | int | float):
            return
        raise ManifestValidationError(
            "Manifest contains an unsupported value type.",
            {"field": path, "type": type(value).__name__},
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)
