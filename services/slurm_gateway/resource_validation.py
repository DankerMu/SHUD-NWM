from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SAFE_SLURM_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SLURM_WALLTIME_RE = re.compile(
    r"^(?:(?P<days>\d{1,3})-)?(?P<hours>\d{1,3}):(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)$"
)
SAFE_DIRECTIVE_PATH_RE = re.compile(r"^(?:[A-Za-z0-9_./:@+=,-]+)$")
SHELL_DIRECTIVE_UNSAFE_RE = re.compile(r"[\s\"'#\\;|&$`<>\n\r]")

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
OPTIONAL_RESOURCE_FIELDS = {"account"}
RESOURCE_PROFILE_FIELDS = REQUIRED_RESOURCE_FIELDS | OPTIONAL_RESOURCE_FIELDS

RESOURCE_LIMITS = {
    "nodes": 128,
    "ntasks": 4096,
    "cpus_per_task": 256,
    "memory_gb": 4096,
    "max_concurrent": 10000,
    "shud_threads": 256,
}
MAX_WALLTIME_SECONDS = 30 * 24 * 60 * 60


class ResourceProfileValidationError(ValueError):
    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


def validate_resource_profile(
    profile: Mapping[str, Any],
    *,
    model_id: str | None = None,
    path: str = "resource_profile",
    require_required: bool = True,
) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        raise ResourceProfileValidationError(
            "Resolved resource profile must be a mapping.",
            {"field": path, "type": type(profile).__name__},
        )

    normalized = dict(profile)
    unknown_fields = sorted(str(field) for field in set(normalized) - RESOURCE_PROFILE_FIELDS)
    if unknown_fields:
        raise ResourceProfileValidationError(
            "Resolved resource profile contains unsupported fields.",
            {
                "field": path,
                "reason": "unsupported_resource_profile_fields",
                "unsupported_fields": [_safe_resource_profile_field(field) for field in unknown_fields],
            },
        )
    if require_required:
        missing = sorted(REQUIRED_RESOURCE_FIELDS - set(normalized))
        if missing:
            raise ResourceProfileValidationError(
                "Resolved resource profile is missing required fields.",
                {"model_id": model_id, "missing_fields": missing},
            )

    for field_name in ("partition", "account"):
        if field_name not in normalized:
            continue
        normalized[field_name] = validate_slurm_identifier(
            normalized[field_name],
            f"{path}.{field_name}",
            allow_empty=field_name == "account",
        )

    for field_name, maximum in RESOURCE_LIMITS.items():
        if field_name not in normalized:
            continue
        normalized[field_name] = validate_positive_int(
            normalized[field_name],
            f"{path}.{field_name}",
            maximum=maximum,
        )

    if "walltime" in normalized:
        normalized["walltime"] = validate_walltime(normalized["walltime"], f"{path}.walltime")

    return normalized


def validate_sbatch_directive_context(context: Mapping[str, Any]) -> None:
    if "workspace_dir" in context:
        validate_directive_path(context["workspace_dir"], "manifest.workspace_dir")
    for field_name in ("run_id", "stage_name"):
        if field_name in context:
            validate_directive_token(context[field_name], f"manifest.{field_name}")
    if "account" in context and context.get("account") not in (None, ""):
        validate_slurm_identifier(context["account"], "resource_profile.account", allow_empty=True)


def validate_slurm_identifier(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if value is None:
        if allow_empty:
            return ""
        raise ResourceProfileValidationError(
            "Slurm resource identifier is required.",
            {"field": field, "reason": "required"},
        )
    if not isinstance(value, str):
        raise ResourceProfileValidationError(
            "Slurm resource identifiers must be strings.",
            {"field": field, "type": type(value).__name__},
        )
    text = value.strip()
    if text == "" and allow_empty:
        return ""
    if text != value or text.startswith("-") or SHELL_DIRECTIVE_UNSAFE_RE.search(text):
        raise ResourceProfileValidationError(
            "Slurm resource identifier contains unsafe directive characters.",
            {"field": field, "reason": "unsafe_directive_value"},
        )
    if not SAFE_SLURM_IDENTIFIER_RE.fullmatch(text):
        raise ResourceProfileValidationError(
            "Slurm resource identifier contains unsupported characters.",
            {"field": field, "reason": "invalid_identifier"},
        )
    return text


def validate_directive_token(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ResourceProfileValidationError(
            "Slurm directive tokens must be strings.",
            {"field": field, "type": type(value).__name__},
        )
    if value.startswith("-") or SHELL_DIRECTIVE_UNSAFE_RE.search(value):
        raise ResourceProfileValidationError(
            "Slurm directive token contains unsafe characters.",
            {"field": field, "reason": "unsafe_directive_value"},
        )
    if not SAFE_SLURM_IDENTIFIER_RE.fullmatch(value):
        raise ResourceProfileValidationError(
            "Slurm directive token contains unsupported characters.",
            {"field": field, "reason": "invalid_identifier"},
        )
    return value


def _safe_resource_profile_field(field: str) -> str:
    if SAFE_SLURM_IDENTIFIER_RE.fullmatch(field):
        return field
    return "[redacted]"


def validate_directive_path(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ResourceProfileValidationError(
            "Slurm directive paths must be strings.",
            {"field": field, "type": type(value).__name__},
        )
    if not value or value.startswith("-") or SHELL_DIRECTIVE_UNSAFE_RE.search(value):
        raise ResourceProfileValidationError(
            "Slurm directive path contains unsafe characters.",
            {"field": field, "reason": "unsafe_directive_value"},
        )
    if not SAFE_DIRECTIVE_PATH_RE.fullmatch(value):
        raise ResourceProfileValidationError(
            "Slurm directive path contains unsupported characters.",
            {"field": field, "reason": "invalid_path"},
        )
    return value


def validate_positive_int(value: Any, field: str, *, maximum: int) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ResourceProfileValidationError(
            "Slurm resource values must be positive bounded integers.",
            {"field": field, "type": type(value).__name__, "minimum": 1, "maximum": maximum},
        )
    if isinstance(value, int):
        integer = value
    elif isinstance(value, str) and value.isdecimal():
        integer = int(value)
    else:
        raise ResourceProfileValidationError(
            "Slurm resource values must be positive bounded integers.",
            {"field": field, "type": type(value).__name__, "minimum": 1, "maximum": maximum},
        )
    if integer < 1 or integer > maximum:
        raise ResourceProfileValidationError(
            "Slurm resource value is outside the allowed range.",
            {"field": field, "minimum": 1, "maximum": maximum},
        )
    return integer


def validate_walltime(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ResourceProfileValidationError(
            "Slurm walltime must be a string.",
            {"field": field, "type": type(value).__name__},
        )
    if value.strip() != value or value.startswith("-") or SHELL_DIRECTIVE_UNSAFE_RE.search(value):
        raise ResourceProfileValidationError(
            "Slurm walltime contains unsafe directive characters.",
            {"field": field, "reason": "unsafe_directive_value"},
        )
    match = SLURM_WALLTIME_RE.fullmatch(value)
    if not match:
        raise ResourceProfileValidationError(
            "Slurm walltime must use [days-]HH:MM:SS with minute and second fields below 60.",
            {"field": field, "reason": "invalid_walltime"},
        )
    days = int(match.group("days") or 0)
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    total_seconds = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    if total_seconds < 1 or total_seconds > MAX_WALLTIME_SECONDS:
        raise ResourceProfileValidationError(
            "Slurm walltime must be a positive bounded duration.",
            {"field": field, "reason": "walltime_out_of_range"},
        )
    return value
