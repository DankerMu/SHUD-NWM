from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from datetime import UTC, datetime, timedelta
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
    ArraySubmitJobRequest,
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
SLURM_JOB_ID_RE = re.compile(r"^\d+(_\d+)?$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHELL_META_RE = re.compile(r"[;|&$`<>\n\r]")
DEFAULT_LIST_LOOKBACK_HOURS = 24
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_TRUNCATION_MARKER = "\n\n[truncated: log exceeded 10485760 bytes]\n"

FREEFORM_STRING_FIELDS = {
    "script",
}

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
    "CONFIGURING": SlurmJobStatus.SUBMITTED,
    "RUNNING": SlurmJobStatus.RUNNING,
    "COMPLETING": SlurmJobStatus.RUNNING,
    "SUSPENDED": SlurmJobStatus.RUNNING,
    "COMPLETED": SlurmJobStatus.SUCCEEDED,
    "FAILED": SlurmJobStatus.FAILED,
    "TIMEOUT": SlurmJobStatus.FAILED,
    "NODE_FAIL": SlurmJobStatus.FAILED,
    "OUT_OF_MEMORY": SlurmJobStatus.FAILED,
    "PREEMPTED": SlurmJobStatus.FAILED,
    "DEADLINE": SlurmJobStatus.FAILED,
    "CANCELLED": SlurmJobStatus.CANCELLED,
}


def map_slurm_error_code(raw_state: str) -> str:
    normalized = _normalize_slurm_state(raw_state)
    if normalized == "TIMEOUT":
        return "SLURM_TIMEOUT"
    if normalized in {"NODE_FAIL", "PREEMPTED"}:
        return "NODE_FAILURE"
    if normalized == "OUT_OF_MEMORY":
        return "OUT_OF_MEMORY"
    return "SLURM_JOB_FAILED"


def _normalize_slurm_state(raw_state: str) -> str:
    return raw_state.strip().upper().split()[0].rstrip("+")


class RealSlurmGateway(SlurmGateway):
    def __init__(self, settings: SlurmGatewaySettings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._jobs: dict[str, SlurmJobRecord] = {}

    def submit_job(self, request: SubmitJobRequest) -> SlurmJobRecord:
        manifest = request.normalized_manifest()
        run_id = request.resolved_run_id()
        model_id = request.resolved_model_id()
        job_type = request.resolved_job_type()
        if job_type and job_type not in self.settings.job_type_templates:
            raise TemplateNotFoundError(job_type)
        if run_id:
            manifest["run_id"] = run_id
        if model_id:
            manifest["model_id"] = model_id
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
        job_type: str | SubmitJobRequest | ArraySubmitJobRequest | Mapping[str, Any],
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
        self._validate_job_id(job_id)
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

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        self._validate_job_id(job_id)
        result = self._run_command(
            [
                self._slurm_command("sacct"),
                "--parsable2",
                "--noheader",
                "--format=JobID,State,ExitCode",
                f"--jobs={job_id}",
            ]
        )
        return self._parse_sacct_array_tasks(result.stdout, job_id)

    def cancel_job(self, job_id: str) -> SlurmJobRecord:
        self._validate_job_id(job_id)
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
        if not start_time:
            start_time = (self._now() - timedelta(hours=DEFAULT_LIST_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
        command.append(f"--starttime={start_time}")
        if end_time:
            command.append(f"--endtime={end_time}")
        result = self._run_command(command)
        records = self._parse_sacct_list(result.stdout)
        return [record.model_copy(deep=True) for record in records[offset : offset + limit]]

    def fetch_logs(self, job_id: str) -> SlurmLogsResponse:
        self._validate_job_id(job_id)
        record = self._jobs.get(job_id)
        metadata_complete = record is not None
        if record is None:
            try:
                record = self.get_job_status(job_id)
                metadata_complete = True
            except SlurmGatewayError:
                record = None
                metadata_complete = False

        run_id = record.run_id if record and record.run_id else job_id
        log_path = self._resolve_log_path(job_id, run_id, record)
        logs = ""
        truncated = False
        if log_path is not None and log_path.exists():
            logs, truncated = self._read_log_file(log_path)

        array_task_logs = self._collect_array_task_logs(job_id, run_id, record)
        complete = bool(record and record.status in TERMINAL_STATUSES)
        return SlurmLogsResponse(
            job_id=job_id,
            run_id=run_id,
            logs=logs,
            complete=complete,
            truncated=truncated,
            metadata_complete=metadata_complete,
            array_task_logs=array_task_logs,
        )

    def reset(self, request: ResetRequest | None = None) -> ResetResponse:
        del request
        cleared = len(self._jobs)
        self._jobs.clear()
        return ResetResponse(status="ok", cleared=cleared, next_job_id="")

    def health(self) -> SlurmHealthResponse:
        try:
            result = self._run_command([self._slurm_command("sinfo"), "--version"])
        except SlurmGatewayError as exc:
            return SlurmHealthResponse(backend="slurm", version="", status="unhealthy", error=exc.message)
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
            self._require_manifest_fields(
                entry,
                ["task_id", "model_id", "basin_version_id", "river_network_version_id",
                 "run_id", "source_id", "cycle_time", "workspace_dir"],
            )
            self._validate_manifest(entry)
            entries.append(entry)

        output_dir = Path(self.settings.workspace_dir) / cycle_id / "manifests"
        self._ensure_within_workspace(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_within_workspace(output_dir)

        for _ in range(10):
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            output_path = output_dir / f"{stage_name}_index_{timestamp}.json"
            try:
                with output_path.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(entries, indent=2, sort_keys=True))
                return output_path
            except FileExistsError:
                continue

        raise SlurmValidationError(
            "Unable to create a unique manifest index path.",
            {"cycle_id": cycle_id, "stage_name": stage_name},
        )

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
        job_type: str | SubmitJobRequest | ArraySubmitJobRequest | Mapping[str, Any],
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
        elif isinstance(job_type, ArraySubmitJobRequest):
            base_manifest.update(job_type.manifest)
            if job_type.model_extra:
                base_manifest.update(job_type.model_extra)
            base_manifest["job_type"] = job_type.job_type
            base_manifest["cycle_id"] = job_type.cycle_id
            if job_type.stage_name is not None:
                base_manifest["stage_name"] = job_type.stage_name
            base_manifest["tasks"] = [dict(item) for item in job_type.tasks]
            resolved_job_type = job_type.job_type
        elif isinstance(job_type, Mapping):
            request_mapping = dict(job_type)
            nested_manifest = request_mapping.get("manifest")
            if nested_manifest is not None:
                if not isinstance(nested_manifest, Mapping):
                    raise ManifestValidationError(
                        "Array job request manifest must be a mapping.",
                        {"field": "manifest", "type": type(nested_manifest).__name__},
                    )
                base_manifest.update(nested_manifest)
            else:
                base_manifest.update(request_mapping)

            for key in ("job_type", "cycle_id", "stage_name", "stage", "tasks", "basins"):
                if key in request_mapping and request_mapping[key] is not None:
                    base_manifest[key] = request_mapping[key]
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

    def _parse_sacct_array_tasks(self, stdout: str, job_id: str) -> list[dict[str, Any]]:
        task_pattern = re.compile(rf"^{re.escape(job_id)}_(\d+)$")
        results: list[dict[str, Any]] = []
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                continue
            fields = raw_line.rstrip("\n").split("|")
            if len(fields) != 3:
                LOGGER.error("Failed to parse sacct array task output: %r", stdout)
                raise SlurmParseError("Unable to parse sacct array task output.", {"stdout": stdout})
            task_job_id, state, raw_exit_code = fields
            match = task_pattern.fullmatch(task_job_id)
            if match is None:
                continue
            results.append(
                {
                    "task_id": int(match.group(1)),
                    "job_id": task_job_id,
                    "state": state,
                    "exit_code": self._parse_exit_code(raw_exit_code),
                }
            )
        return sorted(results, key=lambda result: int(result["task_id"]))

    def _record_from_sacct_fields(
        self,
        fields: Sequence[str],
        job_id: str,
        job_name: str | None = None,
    ) -> SlurmJobRecord:
        raw_state = fields[1]
        state = self._map_slurm_state(raw_state)
        exit_code = self._parse_exit_code(fields[2])
        started_at = self._parse_slurm_datetime(fields[3])
        finished_at = self._parse_slurm_datetime(fields[4]) if state in TERMINAL_STATUSES else None
        existing = self._jobs.get(job_id)
        now = self._now()
        manifest = dict(existing.manifest) if existing else {}
        normalized_state = _normalize_slurm_state(raw_state)
        manifest["slurm_raw_state"] = normalized_state
        if job_name:
            manifest["job_name"] = job_name
        error_code = map_slurm_error_code(raw_state) if state == SlurmJobStatus.FAILED else None
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
            error_code=error_code,
            manifest=manifest,
        )

    def _map_slurm_state(self, raw_state: str) -> SlurmJobStatus:
        normalized = _normalize_slurm_state(raw_state)
        status = SLURM_STATE_MAP.get(normalized)
        if status is None:
            LOGGER.warning("Unmapped Slurm state: %r, treating as FAILED", raw_state)
            return SlurmJobStatus.FAILED
        return status

    def _resolve_log_path(self, job_id: str, run_id: str, record: SlurmJobRecord | None) -> Path | None:
        workspace_dir = Path(self.settings.workspace_dir)
        candidates = [
            workspace_dir / run_id / "logs" / f"{job_id}.out",
            workspace_dir / "logs" / f"{job_id}.out",
        ]
        if record is None:
            candidates.extend(sorted(workspace_dir.glob(f"*/logs/{job_id}.out")))
        if record is not None:
            manifest_run_id = record.manifest.get("run_id")
            if manifest_run_id and str(manifest_run_id) != run_id:
                candidates.insert(0, workspace_dir / str(manifest_run_id) / "logs" / f"{job_id}.out")
        if "_" in job_id:
            master_job_id, task_id = job_id.rsplit("_", maxsplit=1)
            if task_id.isdigit():
                candidates.extend(
                    [
                        workspace_dir / run_id / "logs" / f"{master_job_id}_{task_id}.out",
                        workspace_dir / "logs" / f"{master_job_id}_{task_id}.out",
                    ]
                )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0] if candidates else None

    def _collect_array_task_logs(
        self,
        job_id: str,
        run_id: str,
        record: SlurmJobRecord | None,
    ) -> list[dict[str, Any]] | None:
        if "_" in job_id:
            return None
        workspace_dir = Path(self.settings.workspace_dir)
        log_dirs = [workspace_dir / run_id / "logs", workspace_dir / "logs"]
        if record is None:
            discovered_dirs = {
                candidate.parent for candidate in workspace_dir.glob(f"*/logs/{job_id}_*.out")
            } | {
                candidate.parent for candidate in workspace_dir.glob(f"*/logs/{job_id}_*.err")
            }
            log_dirs.extend(sorted(discovered_dirs))
        if record is not None:
            manifest_run_id = record.manifest.get("run_id")
            if manifest_run_id and str(manifest_run_id) != run_id:
                log_dirs.insert(0, workspace_dir / str(manifest_run_id) / "logs")

        task_ids: set[int] = set()
        for log_dir in log_dirs:
            if not log_dir.exists():
                continue
            for candidate in log_dir.glob(f"{job_id}_*.out"):
                match = re.fullmatch(rf"{re.escape(job_id)}_(\d+)\.out", candidate.name)
                if match:
                    task_ids.add(int(match.group(1)))
            for candidate in log_dir.glob(f"{job_id}_*.err"):
                match = re.fullmatch(rf"{re.escape(job_id)}_(\d+)\.err", candidate.name)
                if match:
                    task_ids.add(int(match.group(1)))

        if not task_ids:
            return None

        max_task_logs = 200
        entries: list[dict[str, Any]] = []
        for task_id in sorted(task_ids)[:max_task_logs]:
            stdout, stdout_truncated = self._read_first_existing_task_log(log_dirs, job_id, task_id, "out")
            stderr, stderr_truncated = self._read_first_existing_task_log(log_dirs, job_id, task_id, "err")
            entries.append(
                {
                    "task_id": task_id,
                    "stdout": stdout,
                    "stderr": stderr,
                    "truncated": stdout_truncated or stderr_truncated,
                    "missing_stdout": not stdout and not stdout_truncated,
                    "missing_stderr": not stderr and not stderr_truncated,
                }
            )
        return entries

    def _read_first_existing_task_log(
        self,
        log_dirs: Sequence[Path],
        job_id: str,
        task_id: int,
        suffix: str,
    ) -> tuple[str, bool]:
        filename = f"{job_id}_{task_id}.{suffix}"
        for log_dir in log_dirs:
            candidate = log_dir / filename
            if candidate.exists():
                return self._read_log_file(candidate)
        return "", False

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
        except ValueError as exc:
            raise SlurmParseError("Unable to parse Slurm timestamp.", {"timestamp": raw_value}) from exc

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
        if not isinstance(value, Mapping):
            raise ManifestValidationError(
                "Manifest must be a mapping.",
                {"field": path, "type": type(value).__name__},
            )

        for key, nested in value.items():
            if not isinstance(key, str) or not SAFE_KEY_RE.fullmatch(key):
                raise ManifestValidationError("Manifest contains an unsafe field name.", {"field": str(key)})

            field_path = f"{path}.{key}"
            if key in STRICT_IDENTIFIER_FIELDS:
                self._validate_identifier_field(nested, field_path)
            elif isinstance(nested, str) and key not in FREEFORM_STRING_FIELDS:
                self._validate_rendered_string(nested, field_path)

    def _validate_identifier_field(self, value: Any, path: str) -> None:
        if value is None or isinstance(value, bool | int | float):
            return
        if not isinstance(value, str):
            raise ManifestValidationError(
                "Manifest identifier field contains an unsupported value type.",
                {"field": path, "type": type(value).__name__},
            )
        if SHELL_META_RE.search(value) or not SAFE_IDENTIFIER_RE.fullmatch(value):
            raise ManifestValidationError(
                "Manifest identifier fields may contain only alphanumeric characters, underscores, and hyphens.",
                {"field": path, "value": value},
            )

    def _validate_rendered_string(self, value: str, path: str) -> None:
        if SHELL_META_RE.search(value):
            raise ManifestValidationError(
                "Manifest field contains shell metacharacters.",
                {"field": path, "value": value},
            )

    def _validate_job_id(self, job_id: str) -> None:
        if SLURM_JOB_ID_RE.fullmatch(job_id):
            return
        raise SlurmGatewayError(
            400,
            "INVALID_JOB_ID",
            "Slurm job_id must be a numeric job id optionally followed by an array task suffix.",
            {"job_id": job_id},
        )

    def _read_log_file(self, log_path: Path) -> tuple[str, bool]:
        if log_path.is_symlink():
            LOGGER.warning("Refusing to read symlinked Slurm log path: %s", log_path)
            return "", False

        try:
            resolved_log_path = log_path.resolve(strict=True)
            self._ensure_within_workspace(resolved_log_path)
        except (FileNotFoundError, SlurmGatewayError):
            LOGGER.warning("Refusing to read Slurm log path outside workspace: %s", log_path)
            return "", False

        with resolved_log_path.open("rb") as handle:
            data = handle.read(MAX_LOG_BYTES + 1)
        truncated = len(data) > MAX_LOG_BYTES
        if truncated:
            data = data[:MAX_LOG_BYTES]
        logs = data.decode("utf-8", errors="replace")
        if truncated:
            logs += LOG_TRUNCATION_MARKER
        return logs, truncated

    def _ensure_within_workspace(self, path: Path) -> None:
        workspace_dir = Path(self.settings.workspace_dir).expanduser().resolve()
        resolved_path = path.expanduser().resolve()
        try:
            resolved_path.relative_to(workspace_dir)
        except ValueError as exc:
            raise SlurmValidationError(
                "Resolved Slurm gateway path is outside the configured workspace directory.",
                {"path": str(path), "workspace_dir": str(workspace_dir)},
            ) from exc

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)
