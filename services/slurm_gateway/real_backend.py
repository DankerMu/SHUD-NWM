from __future__ import annotations

import logging
import os
import re
import selectors
import shlex
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from packages.common.manifest_index import ManifestValidationError as CommonManifestValidationError
from packages.common.manifest_index import serialize_manifest_index
from packages.common.redaction import redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    ensure_directory_no_follow,
    list_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
    write_bytes_no_follow_exclusive,
)
from packages.common.slurm_env import (
    is_sensitive_slurm_env_key,
    iter_secret_manifest_findings,
    reserved_slurm_env_reason,
    secret_bearing_url_reason,
    secret_manifest_key_reason,
)
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings
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
    SLURM_HEALTH_BINARIES,
    TERMINAL_STATUSES,
    ArraySubmitJobRequest,
    ResetRequest,
    ResetResponse,
    SlurmBinaryProbe,
    SlurmHealthResponse,
    SlurmJobRecord,
    SlurmJobStatus,
    SlurmLogsResponse,
    SubmitJobRequest,
)
from services.slurm_gateway.resource_validation import (
    ResourceProfileValidationError,
    validate_resource_profile,
    validate_sbatch_directive_context,
)

LOGGER = logging.getLogger(__name__)
_ORIGINAL_SUBPROCESS_RUN = subprocess.run

SBATCH_JOB_ID_RE = re.compile(r"Submitted batch job (\d+)")
SLURM_JOB_ID_RE = re.compile(r"^\d+(_\d+)?$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SAFE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:=,@+\-]*$")
SAFE_TEMPLATE_DETAIL_RE = re.compile(r"^[A-Za-z0-9_.-]+\.sbatch$")
SHELL_META_RE = re.compile(r"[;|&$`<>\n\r]")
MAX_RENDERED_STRING_BYTES = 4096
MAX_SLURM_ENV_VALUE_LENGTH = 1024
MAX_SLURM_COMMAND_OUTPUT_BYTES = 256 * 1024
MAX_SLURM_ERROR_SNIPPET_BYTES = 2048
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

ARRAY_CAPABLE_JOB_TYPES = {
    "hindcast",
    "produce_forcing_array",
    "run_shud_forecast_array",
    "parse_output_array",
    "compute_frequency_array",
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


def _sacct_metric_fields(fields: Sequence[str]) -> dict[str, Any]:
    names = ("elapsed", "max_rss", "ave_rss", "alloc_tres", "max_disk_read", "max_disk_write")
    return {name: value for name, value in zip(names, fields, strict=False) if value not in (None, "")}


def _normalize_slurm_state(raw_state: str) -> str:
    normalized = raw_state.strip().upper().split()[0].rstrip("+")
    return normalized if re.fullmatch(r"[A-Z0-9_]+", normalized) else "UNKNOWN"


class RealSlurmGateway(SlurmGateway):
    def __init__(self, settings: SlurmGatewaySettings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._jobs: dict[str, SlurmJobRecord] = {}

    def submit_job(self, request: SubmitJobRequest) -> SlurmJobRecord:
        manifest = request.normalized_manifest()
        self._validate_manifest_secret_scan(manifest)
        run_id = request.resolved_run_id()
        model_id = request.resolved_model_id()
        job_type = request.resolved_job_type()
        if job_type and job_type not in self.settings.job_type_templates:
            raise TemplateNotFoundError(job_type)
        if job_type in ARRAY_CAPABLE_JOB_TYPES:
            raise SlurmValidationError(
                "Array-capable job types must be submitted through the array endpoint.",
                {"job_type": job_type, "endpoint": "/api/v1/slurm/job-arrays"},
            )
        self._validate_requested_template_mapping(str(job_type or ""), manifest)
        if run_id:
            manifest["run_id"] = run_id
        if model_id:
            manifest["model_id"] = model_id
        if job_type:
            manifest["job_type"] = job_type
        self._require_manifest_fields(manifest, ["run_id", "model_id", "job_type"])
        manifest["slurm_env"] = self._validate_slurm_env(manifest.get("slurm_env") or {})
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
        self._validate_manifest_secret_scan(base_manifest)
        if not task_list:
            raise SlurmValidationError("Cannot submit array job with 0 tasks")
        self._validate_requested_template_mapping(job_type, base_manifest)
        slurm_env = self._validate_slurm_env(base_manifest.get("slurm_env") or {})
        self._validate_manifest(base_manifest)
        self._validate_manifest_index_bounds(task_list)
        for index, task in enumerate(task_list):
            task_manifest = dict(task)
            task_manifest.setdefault("task_id", index)
            self._validate_manifest(task_manifest)

        requested_workspace = str(base_manifest.get("workspace_dir") or "").strip()
        workspace_root = self._resolve_submission_workspace_dir(requested_workspace)
        task_list = self._tasks_with_submission_workspace(
            task_list,
            workspace_root,
            require_match=bool(requested_workspace),
        )
        first_task = dict(task_list[0])
        model_id = str(first_task.get("model_id") or base_manifest.get("model_id") or "")
        profile = self.resolve_resource_profile(model_id)
        max_concurrent = int(profile["max_concurrent"])
        if max_concurrent < 1:
            raise SlurmValidationError("max_concurrent must be \u2265 1")
        task_count = len(task_list)
        effective_max_concurrent = min(max_concurrent, task_count)
        profile["max_concurrent"] = effective_max_concurrent
        manifest_index_path = self.write_manifest_index(cycle_id, stage_name, task_list, workspace_dir=workspace_root)
        render_manifest = {
            **base_manifest,
            "job_type": job_type,
            "stage_name": stage_name,
            "cycle_id": cycle_id,
            "run_id": first_task.get("run_id", base_manifest.get("run_id", f"{cycle_id}_{stage_name}")),
            "model_id": model_id,
            "manifest_index_path": str(manifest_index_path),
            "workspace_dir": str(workspace_root),
            "slurm_env": slurm_env,
        }
        self._require_manifest_fields(render_manifest, ["run_id", "model_id", "job_type", "cycle_id", "stage_name"])
        self._validate_manifest(render_manifest)
        rendered_script = self.render_template(job_type, render_manifest, str(manifest_index_path), profile=profile)

        array_spec = f"0-{task_count - 1}%{effective_max_concurrent}"
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
                "--format=JobID,State,ExitCode,Start,End,Elapsed,MaxRSS,AveRSS,AllocTRES",
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
                "--format=JobID,State,ExitCode,Elapsed,MaxRSS,AveRSS,AllocTRES",
                f"--jobs={job_id}",
            ]
        )
        return self._parse_sacct_array_tasks(result.stdout, job_id)

    def cancel_job(self, job_id: str) -> SlurmJobRecord:
        self._validate_job_id(job_id)
        try:
            self._run_command([self._slurm_command("scancel"), job_id])
        except SlurmCommandError as exc:
            stderr_detail = (exc.details or {}).get("stderr", "")
            stderr_snippet = _safe_output_snippet(stderr_detail)
            if "invalid job" in stderr_snippet.lower() or "not found" in stderr_snippet.lower():
                safe_stderr = (
                    stderr_detail
                    if isinstance(stderr_detail, Mapping)
                    else _safe_output_detail(str(stderr_detail), truncated=False)
                )
                raise SlurmJobNotFoundError(job_id, {"job_id": job_id, "stderr": safe_stderr}) from exc
            raise

        try:
            record = self.get_job_status(job_id)
        except SlurmGatewayError as exc:
            raise SlurmGatewayError(
                409,
                "SLURM_CANCELLATION_GAP",
                "Slurm accepted scancel but cancellation could not be proven from authoritative job state.",
                {"job_id": job_id, "reason": exc.code, "details": exc.details or {}},
            ) from exc
        if record.status != SlurmJobStatus.CANCELLED:
            raise SlurmGatewayError(
                409,
                "SLURM_CANCELLATION_PENDING",
                "Slurm accepted scancel but the job has not reached CANCELLED state.",
                {
                    "job_id": job_id,
                    "status": record.status.value,
                    "slurm_raw_state": record.manifest.get("slurm_raw_state"),
                    "cancellation_proven": False,
                },
            )
        proven = record.model_copy(
            update={
                "manifest": {
                    **dict(record.manifest),
                    "cancellation_proven": True,
                }
            },
            deep=True,
        )
        self._jobs[job_id] = proven
        return proven.model_copy(deep=True)

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
        if log_path is not None:
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
        binaries: dict[str, SlurmBinaryProbe] = {}
        version = ""
        errors: list[str] = []
        for name in SLURM_HEALTH_BINARIES:
            probe, probe_version = self._probe_binary(name)
            binaries[name] = probe
            if probe_version and not version:
                version = probe_version
            if not probe.executable and probe.detail:
                errors.append(f"{name}: {probe.detail}")

        healthy = all(probe.executable for probe in binaries.values())
        version = version or self.settings.version
        if healthy:
            return SlurmHealthResponse(
                backend="slurm",
                version=version,
                status="healthy",
                healthy=True,
                binaries=binaries,
            )
        return SlurmHealthResponse(
            backend="slurm",
            version="",
            status="unhealthy",
            error="; ".join(errors) or "One or more required Slurm binaries are unavailable.",
            healthy=False,
            binaries=binaries,
        )

    def _probe_binary(self, name: str) -> tuple[SlurmBinaryProbe, str]:
        """Probe a single Slurm client binary for resolvability and executability.

        ``resolved`` is True when the binary can be located (via the configured
        ``slurm_bin_path``/PATH or when a light ``--version`` probe succeeds).
        ``executable`` requires the probe command to return successfully without
        submitting any work. Any error is captured as a redacted detail string.
        """

        command_path = self._slurm_command(name)
        which_resolved = shutil.which(command_path) is not None
        try:
            result = self._run_command([command_path, "--version"])
        except SlurmGatewayError as exc:
            detail = exc.message
            file_missing = isinstance(exc, SlurmCommandError) and (exc.details or {}).get("returncode") is None
            resolved = which_resolved and not file_missing
            return SlurmBinaryProbe(resolved=resolved, executable=False, detail=detail), ""
        except Exception as exc:  # noqa: BLE001
            # Any unexpected probe failure (e.g. subprocess.TimeoutExpired, OSError)
            # must not surface as a 500 from /api/v1/slurm/health. Degrade this one
            # binary to not-executable with a redacted detail so health() always
            # returns a structured healthy=false response.
            return (
                SlurmBinaryProbe(resolved=which_resolved, executable=False, detail=redact_text(str(exc))),
                "",
            )
        probe_version = result.stdout.strip()
        return (
            SlurmBinaryProbe(resolved=True, executable=True, detail=None),
            probe_version,
        )

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

        # Per-deployment partition override: the canonical profile default partition
        # ("compute") may not exist on a given cluster; SLURM_GATEWAY_PARTITION_OVERRIDE
        # lets a deployment target its real partition without editing shared config.
        partition_override = str(getattr(self.settings, "partition_override", "") or "").strip()
        if partition_override:
            resolved["partition"] = partition_override

        try:
            return validate_resource_profile(resolved, model_id=model_id)
        except ResourceProfileValidationError as exc:
            raise ConfigurationError(
                "Resolved resource profile contains invalid Slurm directive values.",
                exc.details,
            ) from exc

    def write_manifest_index(
        self,
        cycle_id: str,
        stage_name: str,
        tasks: Sequence[Mapping[str, Any]],
        *,
        workspace_dir: Path | str | None = None,
    ) -> Path:
        if not tasks:
            raise SlurmValidationError("Cannot submit array job with 0 tasks")
        self._validate_manifest({"cycle_id": cycle_id, "stage_name": stage_name})
        self._validate_manifest_index_bounds(tasks)
        workspace_root = self._resolve_submission_workspace_dir(workspace_dir)

        entries: list[dict[str, Any]] = []
        for index, task in enumerate(tasks):
            entry = dict(task)
            entry["task_id"] = index
            if not entry.get("workspace_dir"):
                entry["workspace_dir"] = str(workspace_root)
            self._require_manifest_fields(
                entry,
                ["task_id", "model_id", "basin_version_id", "river_network_version_id",
                 "run_id", "source_id", "cycle_time", "workspace_dir"],
            )
            self._validate_manifest(entry)
            self._ensure_within_workspace(Path(str(entry["workspace_dir"])), workspace_dir=workspace_root)
            entries.append(entry)

        try:
            content = serialize_manifest_index(entries)
        except CommonManifestValidationError as exc:
            raise ManifestValidationError(exc.message, exc.details) from exc

        try:
            ensure_directory_no_follow(workspace_root)
        except (OSError, SafeFilesystemError) as exc:
            raise SlurmValidationError(
                "Unable to create a safe Slurm gateway workspace directory.",
                {"workspace_dir": str(workspace_root), "error": str(exc)},
            ) from exc

        output_dir = workspace_root / cycle_id / "manifests"
        self._ensure_within_workspace(output_dir, workspace_dir=workspace_root)
        try:
            ensure_directory_no_follow(output_dir, containment_root=workspace_root)
        except (OSError, SafeFilesystemError) as exc:
            raise SlurmValidationError(
                "Unable to create a safe manifest index directory.",
                {"cycle_id": cycle_id, "stage_name": stage_name, "error": str(exc)},
            ) from exc

        for attempt in range(10):
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            suffix = "" if attempt == 0 else f"_{attempt}"
            output_path = output_dir / f"{stage_name}_index_{timestamp}{suffix}.json"
            try:
                write_bytes_no_follow_exclusive(
                    output_path,
                    content,
                    containment_root=workspace_root,
                )
                return output_path
            except FileExistsError:
                continue
            except (OSError, SafeFilesystemError) as exc:
                raise SlurmValidationError(
                    "Unable to create a safe manifest index path.",
                    {"cycle_id": cycle_id, "stage_name": stage_name, "path": str(output_path), "error": str(exc)},
                ) from exc

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
        manifest_dict = dict(manifest)
        manifest_dict["job_type"] = job_type
        manifest_dict.setdefault("stage_name", manifest_dict.get("stage") or job_type)
        manifest_dict.setdefault("workspace_dir", str(Path(self.settings.workspace_dir)))
        manifest_dict.setdefault("manifest_index_path", manifest_index_path)
        self._validate_manifest(manifest_dict)
        slurm_env = self._validate_slurm_env(manifest_dict.get("slurm_env") or {})
        resource_profile = dict(profile or self.resolve_resource_profile(str(manifest_dict.get("model_id") or "")))
        try:
            resource_profile = validate_resource_profile(resource_profile)
            validate_sbatch_directive_context({**manifest_dict, **resource_profile})
        except ResourceProfileValidationError as exc:
            raise ConfigurationError(
                "Resolved resource profile contains invalid Slurm directive values.",
                exc.details,
            ) from exc
        template_path = self._resolve_template_path(job_type)
        context = {**manifest_dict, **resource_profile}
        context["manifest"] = manifest_dict
        context["manifest_index_path"] = manifest_index_path or str(manifest_dict.get("manifest_index_path") or "")
        context["slurm_env"] = slurm_env
        context["slurm_env_exports"] = [
            f"export {key}={shlex.quote(value)}" for key, value in sorted(slurm_env.items())
        ]
        context["export_lines"] = self._template_export_lines(context)

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
        base_manifest["job_type"] = resolved_job_type
        base_manifest["cycle_id"] = resolved_cycle_id
        base_manifest["stage_name"] = resolved_stage_name
        base_manifest["tasks"] = task_list

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

    def _validate_requested_template_mapping(self, job_type: str, manifest: Mapping[str, Any]) -> None:
        mapping = manifest.get("slurm_job_type_templates")
        if mapping in (None, ""):
            return
        if not isinstance(mapping, Mapping):
            raise ManifestValidationError(
                "Slurm job type template contract must be a mapping.",
                {"field": "slurm_job_type_templates", "job_type": job_type},
            )
        requested_template = mapping.get(job_type)
        configured_template = self.settings.job_type_templates.get(job_type)
        expected_template = DEFAULT_JOB_TYPE_TEMPLATES.get(job_type)
        if requested_template != configured_template or requested_template != expected_template:
            raise TemplateSecurityError(
                "Slurm template contract does not match the gateway template configured for this job type.",
                {
                    "job_type": self._safe_identifier_detail(job_type),
                    "requested_template": self._safe_template_detail(requested_template),
                    "configured_template": self._safe_template_detail(configured_template),
                    "expected_template": self._safe_template_detail(expected_template),
                    "requested_matches_configured": requested_template == configured_template,
                    "requested_matches_expected": requested_template == expected_template,
                },
            )

    def _safe_identifier_detail(self, value: Any) -> str | None | dict[str, str]:
        if value is None:
            return None
        if not isinstance(value, str):
            return {"type": type(value).__name__}
        if secret_manifest_key_reason(value) is not None:
            return "[redacted]"
        if SAFE_IDENTIFIER_RE.fullmatch(value):
            return value
        return "[redacted]"

    def _safe_template_detail(self, value: Any) -> str | None | dict[str, str]:
        if value is None:
            return None
        if not isinstance(value, str):
            return {"type": type(value).__name__}
        if secret_bearing_url_reason(value) is not None:
            return "[redacted]"
        if SAFE_TEMPLATE_DETAIL_RE.fullmatch(value):
            return value
        return "[redacted]"

    def _validate_slurm_env(self, value: Any) -> dict[str, str]:
        if value in (None, ""):
            return {}
        if not isinstance(value, Mapping):
            raise ManifestValidationError("slurm_env must be a mapping.", {"field": "slurm_env"})
        result: dict[str, str] = {}
        for key, raw_value in value.items():
            key_text = str(key)
            value_text = str(raw_value)
            key_secret_reason = secret_manifest_key_reason(key_text)
            if key_secret_reason is not None:
                raise ManifestValidationError(
                    "Slurm environment exports reject secret-shaped keys.",
                    {"field": "slurm_env.[redacted]", "reason": key_secret_reason},
                )
            if not SAFE_ENV_KEY_RE.fullmatch(key_text):
                raise ManifestValidationError(
                    "Slurm environment keys must be uppercase shell identifiers.",
                    {"field": f"slurm_env.{key_text}"},
                )
            reserved_reason = reserved_slurm_env_reason(key_text)
            if reserved_reason is not None:
                raise ManifestValidationError(
                    "Slurm environment exports cannot override reserved runtime variables.",
                    {"field": f"slurm_env.{key_text}", "reason": reserved_reason},
                )
            if is_sensitive_slurm_env_key(key_text):
                raise ManifestValidationError(
                    "Slurm environment exports reject secret-shaped keys.",
                    {"field": f"slurm_env.{key_text}"},
                )
            if len(value_text) > MAX_SLURM_ENV_VALUE_LENGTH:
                raise ManifestValidationError(
                    "Slurm environment values must be bounded.",
                    {"field": f"slurm_env.{key_text}", "max_length": MAX_SLURM_ENV_VALUE_LENGTH},
                )
            secret_url_reason = secret_bearing_url_reason(value_text)
            if secret_url_reason is not None:
                raise ManifestValidationError(
                    "Slurm environment exports reject secret-bearing URL values.",
                    {"field": f"slurm_env.{key_text}", "reason": secret_url_reason},
                )
            if SHELL_META_RE.search(value_text) or not SAFE_ENV_VALUE_RE.fullmatch(value_text):
                raise ManifestValidationError(
                    "Slurm environment values must be shell-safe.",
                    {"field": f"slurm_env.{key_text}"},
                )
            result[key_text] = value_text
        return result

    def _validate_manifest_index_bounds(self, tasks: Sequence[Mapping[str, Any]]) -> None:
        try:
            serialize_manifest_index(tasks)
        except CommonManifestValidationError as exc:
            raise ManifestValidationError(exc.message, exc.details) from exc

    def _template_export_lines(self, context: Mapping[str, Any]) -> list[str]:
        export_fields = {
            "WORKSPACE_ROOT": context.get("workspace_dir", ""),
            "OBJECT_STORE_ROOT": context.get("object_store_root", context.get("workspace_dir", "")),
            "OBJECT_STORE_PREFIX": context.get("object_store_prefix", ""),
            "NHMS_RUN_ID": context.get("run_id", ""),
            "NHMS_MODEL_ID": context.get("model_id", ""),
            "NHMS_SOURCE_ID": context.get("source_id", "GFS"),
            "NHMS_CYCLE_ID": context.get("cycle_id", ""),
            "NHMS_CYCLE_TIME": context.get("cycle_time", ""),
            "NHMS_START_TIME": context.get("start_time", ""),
            "NHMS_END_TIME": context.get("end_time", ""),
            "NHMS_BASIN_VERSION_ID": context.get("basin_version_id", ""),
            "NHMS_RIVER_NETWORK_VERSION_ID": context.get("river_network_version_id", ""),
            "NHMS_FORCING_VERSION_ID": context.get("forcing_version_id", ""),
            "NHMS_FORCING_PACKAGE_URI": context.get("forcing_package_uri", ""),
            "NHMS_JOB_TYPE": context.get("job_type", ""),
            "NHMS_RUN_MANIFEST_URI": context.get("run_manifest_uri", ""),
            "NHMS_MANIFEST_INDEX": context.get("manifest_index_path", ""),
            "NHMS_MAX_CONCURRENT": context.get("max_concurrent", ""),
            "SHUD_THREADS": context.get("shud_threads", ""),
            "OMP_NUM_THREADS": context.get("shud_threads", ""),
        }
        return [f"export {key}={shlex.quote(str(value or ''))}" for key, value in export_fields.items()]

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
        if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
            return self._run_command_via_subprocess_run(command)
        stdout = ""
        stderr = ""
        truncated = {"stdout": False, "stderr": False}
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=False,
            )
            stdout_bytes, stderr_bytes, truncated = self._communicate_bounded(
                process,
                timeout_seconds=self.settings.subprocess_timeout_seconds,
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            result = subprocess.CompletedProcess(command, int(process.returncode or 0), stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired as exc:
            LOGGER.error("Slurm command timed out: %s", command[0], exc_info=True)
            raise SlurmTimeoutError(
                f"Slurm command {Path(command[0]).name} timed out after "
                f"{self.settings.subprocess_timeout_seconds} seconds.",
                {
                    "command": command,
                    "timeout_seconds": self.settings.subprocess_timeout_seconds,
                    "stdout": _safe_output_detail(stdout, truncated=truncated["stdout"]),
                    "stderr": _safe_output_detail(stderr, truncated=truncated["stderr"]),
                },
            ) from exc
        except FileNotFoundError as exc:
            LOGGER.error("Slurm command was not found: %s", command[0], exc_info=True)
            raise SlurmCommandError(
                f"Slurm command {Path(command[0]).name} was not found.",
                {
                    "command": command,
                    "stderr": _safe_output_detail(str(exc), truncated=False),
                    "returncode": None,
                },
            ) from exc

        if result.returncode != 0:
            LOGGER.error(
                "Slurm command failed: %s returncode=%s stdout=%s stderr=%s",
                command[0],
                result.returncode,
                _safe_output_detail(result.stdout, truncated=truncated["stdout"]),
                _safe_output_detail(result.stderr, truncated=truncated["stderr"]),
            )
            raise SlurmCommandError(
                f"Slurm command {Path(command[0]).name} failed with exit code {result.returncode}.",
                {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout": _safe_output_detail(result.stdout, truncated=truncated["stdout"]),
                    "stderr": _safe_output_detail(result.stderr, truncated=truncated["stderr"]),
                },
            )
        if truncated["stdout"] or truncated["stderr"]:
            raise SlurmCommandError(
                f"Slurm command {Path(command[0]).name} produced output above the safe capture limit.",
                {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout": _safe_output_detail(result.stdout, truncated=truncated["stdout"]),
                    "stderr": _safe_output_detail(result.stderr, truncated=truncated["stderr"]),
                },
            )
        return result

    def _run_command_via_subprocess_run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
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
                {
                    "command": command,
                    "stderr": _safe_output_detail(str(exc), truncated=False),
                    "returncode": None,
                },
            ) from exc
        stdout = str(result.stdout or "")
        stderr = str(result.stderr or "")
        truncated = {
            "stdout": len(stdout.encode("utf-8")) > MAX_SLURM_COMMAND_OUTPUT_BYTES,
            "stderr": len(stderr.encode("utf-8")) > MAX_SLURM_COMMAND_OUTPUT_BYTES,
        }
        if truncated["stdout"]:
            stdout = _truncate_bytes(stdout, MAX_SLURM_COMMAND_OUTPUT_BYTES)
        if truncated["stderr"]:
            stderr = _truncate_bytes(stderr, MAX_SLURM_COMMAND_OUTPUT_BYTES)
        if result.returncode != 0:
            raise SlurmCommandError(
                f"Slurm command {Path(command[0]).name} failed with exit code {result.returncode}.",
                {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout": _safe_output_detail(stdout, truncated=truncated["stdout"]),
                    "stderr": _safe_output_detail(stderr, truncated=truncated["stderr"]),
                },
            )
        if truncated["stdout"] or truncated["stderr"]:
            raise SlurmCommandError(
                f"Slurm command {Path(command[0]).name} produced output above the safe capture limit.",
                {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout": _safe_output_detail(stdout, truncated=truncated["stdout"]),
                    "stderr": _safe_output_detail(stderr, truncated=truncated["stderr"]),
                },
            )
        return subprocess.CompletedProcess(command, result.returncode, stdout=stdout, stderr=stderr)

    def _communicate_bounded(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout_seconds: int,
    ) -> tuple[bytes, bytes, dict[str, bool]]:
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("Slurm command pipes were not configured.")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}
        deadline = self._now().timestamp() + timeout_seconds
        try:
            while selector.get_map():
                remaining = deadline - self._now().timestamp()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(process.args, timeout_seconds)
                events = selector.select(timeout=min(0.1, remaining))
                if not events:
                    if process.poll() is not None:
                        break
                    continue
                for key, _mask in events:
                    chunk = key.fileobj.read1(8192) if hasattr(key.fileobj, "read1") else key.fileobj.read(8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    stream_name = str(key.data)
                    stream_buffer = buffers[stream_name]
                    remaining_bytes = max(MAX_SLURM_COMMAND_OUTPUT_BYTES - len(stream_buffer), 0)
                    if len(stream_buffer) < MAX_SLURM_COMMAND_OUTPUT_BYTES:
                        stream_buffer.extend(chunk[:remaining_bytes])
                    if len(chunk) > remaining_bytes:
                        truncated[stream_name] = True
                        process.kill()
                if process.poll() is not None:
                    break
            process.wait(timeout=max(deadline - self._now().timestamp(), 0.001))
        finally:
            selector.close()
        return bytes(buffers["stdout"]), bytes(buffers["stderr"]), truncated

    def _parse_sbatch_job_id(self, stdout: str) -> str:
        match = SBATCH_JOB_ID_RE.search(stdout)
        if not match:
            LOGGER.error("Failed to parse sbatch stdout: %s", _safe_output_detail(stdout, truncated=False))
            raise SlurmParseError(
                "Unable to parse sbatch job id from stdout.",
                {"stdout": _safe_output_detail(stdout, truncated=False)},
            )
        return match.group(1)

    def _parse_sacct_status(self, stdout: str, job_id: str) -> SlurmJobRecord:
        matching_fields: list[str] | None = None
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                continue
            fields = raw_line.rstrip("\n").split("|")
            if len(fields) < 5:
                LOGGER.error("Failed to parse sacct status output: %s", _safe_output_detail(stdout, truncated=False))
                raise SlurmParseError(
                    "Unable to parse sacct status output.",
                    {"stdout": _safe_output_detail(stdout, truncated=False)},
                )
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
                LOGGER.error("Failed to parse sacct list output: %s", _safe_output_detail(stdout, truncated=False))
                raise SlurmParseError(
                    "Unable to parse sacct list output.",
                    {"stdout": _safe_output_detail(stdout, truncated=False)},
                )
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
            if len(fields) < 3:
                LOGGER.error(
                    "Failed to parse sacct array task output: %s",
                    _safe_output_detail(stdout, truncated=False),
                )
                raise SlurmParseError(
                    "Unable to parse sacct array task output.",
                    {"stdout": _safe_output_detail(stdout, truncated=False)},
                )
            task_job_id, state, raw_exit_code = fields[0], fields[1], fields[2]
            match = task_pattern.fullmatch(task_job_id)
            if match is None:
                continue
            metrics = _sacct_metric_fields(fields[3:])
            results.append(
                {
                    "task_id": int(match.group(1)),
                    "job_id": task_job_id,
                    "state": state,
                    "status": self._map_slurm_state(state).value,
                    "exit_code": self._parse_exit_code(raw_exit_code),
                    "elapsed": metrics.get("elapsed"),
                    "max_rss": metrics.get("max_rss"),
                    "resource_metrics": metrics,
                    "accounting": metrics,
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
        metrics = _sacct_metric_fields(fields[5:])
        if metrics:
            manifest["slurm_accounting"] = metrics
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
            elapsed=metrics.get("elapsed"),
            max_rss=metrics.get("max_rss"),
            resource_metrics=metrics,
        )

    def _map_slurm_state(self, raw_state: str) -> SlurmJobStatus:
        normalized = _normalize_slurm_state(raw_state)
        status = SLURM_STATE_MAP.get(normalized)
        if status is None:
            LOGGER.warning("Unmapped Slurm state: %r, treating as FAILED", raw_state)
            return SlurmJobStatus.FAILED
        return status

    def _resolve_log_path(self, job_id: str, run_id: str, record: SlurmJobRecord | None) -> Path | None:
        workspace_dir = Path(self.settings.workspace_dir).expanduser().resolve()
        candidates = [
            workspace_dir / run_id / "logs" / f"{job_id}.out",
            workspace_dir / "logs" / f"{job_id}.out",
        ]
        if record is None:
            candidates.extend(self._discover_workspace_logs(job_id, "out"))
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
            if self._log_path_exists(candidate):
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
        workspace_dir = Path(self.settings.workspace_dir).expanduser().resolve()
        log_dirs = [workspace_dir / run_id / "logs", workspace_dir / "logs"]
        if record is None:
            discovered_dirs = {
                candidate.parent
                for suffix in ("out", "err")
                for candidate in self._discover_workspace_logs(job_id, suffix, array_tasks=True)
            }
            log_dirs.extend(sorted(discovered_dirs))
        if record is not None:
            manifest_run_id = record.manifest.get("run_id")
            if manifest_run_id and str(manifest_run_id) != run_id:
                log_dirs.insert(0, workspace_dir / str(manifest_run_id) / "logs")

        task_ids: set[int] = set()
        for log_dir in log_dirs:
            for name in self._list_log_dir(log_dir):
                match = re.fullmatch(rf"{re.escape(job_id)}_(\d+)\.(?:out|err)", name)
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
            if self._log_path_exists(candidate):
                return self._read_log_file(candidate)
        return "", False

    def _parse_exit_code(self, raw_exit_code: str) -> int | None:
        if not raw_exit_code:
            return None
        try:
            return int(raw_exit_code.split(":", maxsplit=1)[0])
        except ValueError as exc:
            raise SlurmParseError(
                "Unable to parse Slurm exit code.",
                {"exit_code": _safe_output_detail(raw_exit_code, truncated=False)},
            ) from exc

    def _parse_slurm_datetime(self, raw_value: str) -> datetime | None:
        value = raw_value.strip()
        if value in {"", "Unknown", "None", "N/A"}:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SlurmParseError(
                "Unable to parse Slurm timestamp.",
                {"timestamp": _safe_output_detail(raw_value, truncated=False)},
            ) from exc

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

        self._validate_manifest_secret_scan(value, path)

        for key, nested in value.items():
            if not isinstance(key, str) or not SAFE_KEY_RE.fullmatch(key):
                raise ManifestValidationError("Manifest contains an unsafe field name.", {"field": f"{path}.[unsafe]"})

            field_path = f"{path}.{key}"
            if key in STRICT_IDENTIFIER_FIELDS:
                self._validate_identifier_field(nested, field_path)
            elif isinstance(nested, str) and key not in FREEFORM_STRING_FIELDS:
                self._validate_rendered_string(nested, field_path)

    def _validate_manifest_secret_scan(self, value: Any, path: str = "manifest") -> None:
        secret_findings = iter_secret_manifest_findings(value, path)
        if secret_findings:
            raise ManifestValidationError(
                "Manifest rejects secret-bearing fields and URL values.",
                {"findings": secret_findings},
            )

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
        if len(value.encode("utf-8")) > MAX_RENDERED_STRING_BYTES:
            raise ManifestValidationError(
                "Manifest field exceeds safe rendered string length.",
                {"field": path, "max_bytes": MAX_RENDERED_STRING_BYTES},
            )
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
        workspace_root = Path(self.settings.workspace_dir).expanduser().resolve()
        try:
            data = read_bytes_limited_no_follow(
                log_path,
                max_bytes=MAX_LOG_BYTES,
                containment_root=workspace_root,
            )
        except FileNotFoundError:
            return "", False
        except (OSError, SafeFilesystemError) as exc:
            LOGGER.warning("Refusing to read unsafe Slurm log path %s: %s", log_path, exc)
            return "", False
        truncated = len(data) > MAX_LOG_BYTES
        if truncated:
            data = data[:MAX_LOG_BYTES]
        logs = data.decode("utf-8", errors="replace")
        if truncated:
            logs += LOG_TRUNCATION_MARKER
        return logs, truncated

    def _log_path_exists(self, path: Path) -> bool:
        workspace_root = Path(self.settings.workspace_dir).expanduser().resolve()
        try:
            path.relative_to(workspace_root)
            stat_no_follow(path, containment_root=workspace_root)
        except (FileNotFoundError, OSError, SafeFilesystemError, ValueError):
            return False
        return True

    def _list_log_dir(self, log_dir: Path) -> list[str]:
        workspace_root = Path(self.settings.workspace_dir).expanduser().resolve()
        try:
            return list_directory_no_follow(log_dir, containment_root=workspace_root)
        except (FileNotFoundError, NotADirectoryError, OSError, SafeFilesystemError):
            return []

    def _discover_workspace_logs(self, job_id: str, suffix: str, *, array_tasks: bool = False) -> list[Path]:
        workspace_root = Path(self.settings.workspace_dir).expanduser().resolve()
        pattern = (
            re.compile(rf"^{re.escape(job_id)}_\d+\.{re.escape(suffix)}$")
            if array_tasks
            else re.compile(rf"^{re.escape(job_id)}\.{re.escape(suffix)}$")
        )
        matches: list[Path] = []
        for name in self._list_log_dir(workspace_root):
            run_log_dir = workspace_root / name / "logs"
            for log_name in self._list_log_dir(run_log_dir):
                if pattern.fullmatch(log_name):
                    matches.append(run_log_dir / log_name)
        return sorted(matches)

    def _resolve_submission_workspace_dir(self, workspace_dir: Path | str | None = None) -> Path:
        requested = str(workspace_dir or "").strip()
        configured = Path(self.settings.workspace_dir).expanduser().resolve()
        if not requested:
            return configured
        path = Path(requested).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        self._ensure_within_workspace(path, workspace_dir=configured)
        return path

    def _tasks_with_submission_workspace(
        self,
        tasks: Sequence[Mapping[str, Any]],
        workspace_dir: Path,
        *,
        require_match: bool,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for task in tasks:
            entry = dict(task)
            task_workspace = str(entry.get("workspace_dir") or "").strip()
            if task_workspace:
                resolved_task_workspace = Path(task_workspace).expanduser()
                if not resolved_task_workspace.is_absolute():
                    resolved_task_workspace = (Path.cwd() / resolved_task_workspace).resolve()
                else:
                    resolved_task_workspace = resolved_task_workspace.resolve()
                if require_match and resolved_task_workspace != workspace_dir:
                    raise SlurmValidationError(
                        "Array task workspace_dir must match the submitted scheduler workspace.",
                        {
                            "task_workspace_dir": str(resolved_task_workspace),
                            "workspace_dir": str(workspace_dir),
                        },
                    )
                entry["workspace_dir"] = str(workspace_dir if require_match else resolved_task_workspace)
            else:
                entry["workspace_dir"] = str(workspace_dir)
            normalized.append(entry)
        return normalized

    def _ensure_within_workspace(self, path: Path, *, workspace_dir: Path | None = None) -> None:
        workspace_root = (workspace_dir or Path(self.settings.workspace_dir)).expanduser().resolve()
        resolved_path = path.expanduser()
        if not resolved_path.is_absolute():
            resolved_path = (Path.cwd() / resolved_path).resolve()
        else:
            resolved_path = resolved_path.resolve()
        try:
            resolved_path.relative_to(workspace_root)
        except ValueError as exc:
            raise SlurmValidationError(
                "Resolved Slurm gateway path is outside the configured workspace directory.",
                {"path": str(path), "workspace_dir": str(workspace_root)},
            ) from exc

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)


def _safe_output_detail(value: str, *, truncated: bool) -> dict[str, Any]:
    raw_snippet = _truncate_bytes(value, MAX_SLURM_ERROR_SNIPPET_BYTES)
    snippet = redact_text(raw_snippet)
    return {
        "snippet": snippet,
        "truncated": truncated or len(value.encode("utf-8")) > MAX_SLURM_ERROR_SNIPPET_BYTES,
        "original_bytes": len(value.encode("utf-8")),
        "snippet_bytes": len(snippet.encode("utf-8")),
    }


def _safe_output_snippet(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("snippet", ""))
    return _safe_output_detail(str(value), truncated=False)["snippet"]


def _truncate_bytes(value: str, limit: int) -> str:
    data = value.encode("utf-8")
    if len(data) <= limit:
        return value
    return data[:limit].decode("utf-8", errors="ignore") + "...[truncated]"
