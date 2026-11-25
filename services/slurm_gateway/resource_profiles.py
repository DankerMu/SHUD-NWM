"""Resource profile file loading shared by the Slurm gateway and the scheduler.

A job's profile resolves as ``default``, then ``overrides[model_id]``, then
``job_type_overrides[job_type]``, then the per-deployment overlay
(``partition_override`` / ``exclude_nodes``); each later layer wins.  The
gateway resolves an array's cpus/mem/walltime from task 0's model only, so the
scheduler splits cohorts by the ``overrides`` key set (#2543) and must read the
exact same file with the exact same fail-closed semantics; both sides go
through this module.  ``job_type_overrides`` never changes that split: it is
keyed by job type, not model (#3344).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from services.slurm_gateway.gateway import ConfigurationError
from services.slurm_gateway.resource_validation import (
    SAFE_SLURM_IDENTIFIER_RE,
    ResourceProfileValidationError,
    validate_resource_profile,
)

# The only fields a job-type entry may set.  Because the job type wins over the
# model, anything else (max_concurrent, walltime, partition, ...) would silently
# undo a model's own value; cores and threads must move together.
JOB_TYPE_OVERRIDE_FIELDS = frozenset({"cpus_per_task", "memory_gb", "shud_threads"})
_JOB_TYPE_PAIRED_FIELDS = ("cpus_per_task", "shud_threads")


def load_resource_profiles(path: str | Path) -> dict[str, Any]:
    resolved_path = Path(path).expanduser()
    if not resolved_path.exists():
        raise ConfigurationError(
            "Resource profile configuration file does not exist.",
            {"resource_profiles_path": str(resolved_path)},
        )
    try:
        data = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            "Resource profile configuration is not valid YAML.",
            {"resource_profiles_path": str(resolved_path)},
        ) from exc
    if not isinstance(data, dict):
        raise ConfigurationError("Resource profile configuration must include a default section.")
    profiles = data.get("resource_profiles", data)
    if not isinstance(profiles, dict) or not isinstance(profiles.get("default"), dict):
        raise ConfigurationError("Resource profile configuration must include a default section.")
    overrides = profiles.get("overrides", {})
    if overrides is None:
        profiles["overrides"] = {}
    elif not isinstance(overrides, dict):
        raise ConfigurationError("Resource profile overrides must be a mapping.")
    job_type_overrides = profiles.get("job_type_overrides")
    if job_type_overrides is None:
        profiles["job_type_overrides"] = {}
    elif not isinstance(job_type_overrides, dict):
        raise ConfigurationError("Resource profile job_type_overrides must be a mapping.")
    else:
        for job_type, entry in job_type_overrides.items():
            _validate_job_type_override(_safe_name(job_type), entry)
    return profiles


def _safe_name(name: Any) -> str:
    text = str(name)
    return text if SAFE_SLURM_IDENTIFIER_RE.fullmatch(text) else "[redacted]"


def _validate_job_type_override(job_type: str, entry: Any) -> None:
    """Refuse a malformed ``job_type_overrides`` entry; name keys, never values."""

    prefix = f"Resource profile job_type_overrides entry {job_type}"
    if not isinstance(entry, dict):
        raise ConfigurationError(
            f"{prefix} must be a mapping.",
            {"job_type": job_type, "reason": "job_type_override_not_a_mapping"},
        )
    unsupported = sorted(_safe_name(key) for key in entry if key not in JOB_TYPE_OVERRIDE_FIELDS)
    if unsupported:
        raise ConfigurationError(
            f"{prefix} sets {', '.join(unsupported)}; only cpus_per_task, memory_gb and shud_threads are allowed.",
            {
                "job_type": job_type,
                "reason": "unsupported_job_type_override_fields",
                "unsupported_fields": unsupported,
            },
        )
    paired = [field for field in _JOB_TYPE_PAIRED_FIELDS if field in entry]
    if len(paired) == 1:
        raise ConfigurationError(
            f"{prefix} sets {paired[0]} alone; cpus_per_task and shud_threads must be set together.",
            {"job_type": job_type, "reason": "job_type_override_unpaired_threads", "fields": paired},
        )
    try:
        validate_resource_profile(entry, path=f"job_type_overrides.{job_type}", require_required=False)
    except ResourceProfileValidationError as exc:
        field = str(exc.details.get("field", "")).rsplit(".", 1)[-1]
        raise ConfigurationError(
            f"{prefix} has an invalid {field}: {exc.message}",
            {"job_type": job_type, **exc.details},
        ) from exc


def resource_profile_override_model_ids(path: str | Path) -> frozenset[str]:
    """Model ids whose gateway profile differs from ``default``.

    Mirrors ``resolve_model_resource_profile``: only mapping-valued
    override entries are applied by the gateway, so only those count.
    """

    overrides = load_resource_profiles(path).get("overrides") or {}
    return frozenset(str(model_id) for model_id, profile in overrides.items() if isinstance(profile, dict))


def resolve_model_resource_profile(
    profiles: Mapping[str, Any],
    model_id: str | None,
    *,
    job_type: str | None = None,
    partition_override: Any = "",
    exclude_nodes: Any = "",
) -> dict[str, Any]:
    """Resolve and validate one job's profile from already-loaded ``profiles``.

    ``default``, then a mapping-valued ``overrides[model_id]`` (the same key set
    ``resource_profile_override_model_ids`` counts), then a mapping-valued
    ``job_type_overrides[job_type]`` (``job_type=None`` applies none), then the
    per-deployment ``partition_override`` / ``exclude_nodes`` overlay, then
    ``validate_resource_profile``.  Non-mapping entries are ignored here; the
    loader is what refuses a malformed file.
    """

    resolved = dict(profiles["default"])
    overrides = profiles.get("overrides") or {}
    if model_id and isinstance(overrides.get(model_id), dict):
        resolved.update(overrides[model_id])
    job_type_overrides = profiles.get("job_type_overrides") or {}
    if job_type and isinstance(job_type_overrides, Mapping) and isinstance(job_type_overrides.get(job_type), dict):
        resolved.update(job_type_overrides[job_type])

    # Per-deployment partition override: the canonical profile default partition
    # ("compute") may not exist on a given cluster; SLURM_GATEWAY_PARTITION_OVERRIDE
    # lets a deployment target its real partition without editing shared config.
    partition = str(partition_override or "").strip()
    if partition:
        resolved["partition"] = partition
    excluded = str(exclude_nodes or "").strip()
    if excluded:
        resolved["exclude_nodes"] = excluded

    try:
        return validate_resource_profile(resolved, model_id=model_id)
    except ResourceProfileValidationError as exc:
        raise ConfigurationError(
            "Resolved resource profile contains invalid Slurm directive values.",
            exc.details,
        ) from exc
