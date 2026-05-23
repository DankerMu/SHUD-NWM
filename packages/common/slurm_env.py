from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlsplit

SENSITIVE_SLURM_ENV_KEY_RE = re.compile(
    r"(TOKEN|PASSWORD|PASSWD|PWD|SECRET|CREDENTIAL|API_?KEY|ACCESS_?KEY|SESSION_?KEY|SIGNATURE)",
    re.IGNORECASE,
)
DATABASE_DSN_ENV_KEY_RE = re.compile(
    r"(^DATABASE_URL$|DATABASE.*(?:DSN|URI|URL)|(?:^|_)DB_(?:DSN|URI|URL)$|"
    r"(?:^|_)(?:PG|POSTGRES|POSTGRESQL)_(?:DSN|URI|URL)$|SQLALCHEMY_DATABASE_URI)",
    re.IGNORECASE,
)
SECRET_URL_QUERY_KEY_RE = re.compile(
    r"(^|[-_])(token|password|passwd|pwd|secret|signature|credential|api[-_]?key|"
    r"access[-_]?key|session[-_]?key)$|^x-amz-signature$|^x-amz-credential$",
    re.IGNORECASE,
)
RESERVED_SLURM_ENV_KEYS = frozenset(
    {
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_MANIFEST_INDEX",
        "NHMS_RUN_ID",
        "NHMS_MODEL_ID",
        "NHMS_SOURCE_ID",
        "NHMS_CYCLE_ID",
        "NHMS_CYCLE_TIME",
        "NHMS_START_TIME",
        "NHMS_END_TIME",
        "NHMS_JOB_TYPE",
        "NHMS_RUN_MANIFEST_URI",
        "NHMS_MAX_CONCURRENT",
        "NHMS_BASIN_VERSION_ID",
        "NHMS_RIVER_NETWORK_VERSION_ID",
        "NHMS_FORCING_VERSION_ID",
        "NHMS_FORCING_PACKAGE_URI",
        "NHMS_YEAR",
        "SHUD_THREADS",
        "OMP_NUM_THREADS",
        "SLURM_ARRAY_TASK_ID",
        "MODEL_ID",
        "SOURCE_ID",
        "YEAR",
        "RUN_ID",
        "FORCING_VERSION_ID",
        "FORCING_PACKAGE_URI",
        "TASK_JSON",
    }
)
RESERVED_SLURM_ENV_PREFIXES = ("SLURM_", "SBATCH_")


def is_sensitive_slurm_env_key(key: str) -> bool:
    """Return whether a Slurm env key is unsafe to export or record as evidence."""

    return bool(SENSITIVE_SLURM_ENV_KEY_RE.search(key) or DATABASE_DSN_ENV_KEY_RE.search(key))


def reserved_slurm_env_reason(key: str) -> str | None:
    """Return why a Slurm env key is reserved, or None when user extras may set it."""

    normalized = str(key).upper()
    if normalized in RESERVED_SLURM_ENV_KEYS:
        return "canonical_runtime_env"
    for prefix in RESERVED_SLURM_ENV_PREFIXES:
        if normalized.startswith(prefix):
            return "slurm_runtime_env"
    return None


def is_reserved_slurm_env_key(key: str) -> bool:
    return reserved_slurm_env_reason(key) is not None


def secret_bearing_url_reason(value: str) -> str | None:
    """Return why a URL-shaped env value carries a secret, or None when safe."""

    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return "url_userinfo"
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if SECRET_URL_QUERY_KEY_RE.search(key):
            return "url_secret_query_param"
    return None


def secret_manifest_key_reason(key: str) -> str | None:
    """Return why a manifest key is secret-bearing under the Slurm persistence contract."""

    key_text = str(key)
    url_reason = secret_bearing_url_reason(key_text)
    if url_reason is not None:
        return url_reason
    if is_sensitive_slurm_env_key(key_text):
        return "secret_key"
    return None


def secret_manifest_value_reason(value: str) -> str | None:
    """Return why a manifest value is secret-bearing under the Slurm persistence contract."""

    return secret_bearing_url_reason(value)


def iter_secret_manifest_findings(value: Any, path: str = "manifest") -> list[dict[str, str]]:
    """Find secret-bearing keys or URL values in a manifest-like payload."""

    findings: list[dict[str, str]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            key_reason = secret_manifest_key_reason(key_text)
            if key_reason is not None:
                findings.append({"field": f"{path}.[redacted]", "reason": key_reason})
                continue
            field_path = f"{path}.{key_text}"
            findings.extend(iter_secret_manifest_findings(nested, field_path))
        return findings
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, nested in enumerate(value):
            findings.extend(iter_secret_manifest_findings(nested, f"{path}[{index}]"))
        return findings
    if isinstance(value, str):
        value_reason = secret_manifest_value_reason(value)
        if value_reason is not None:
            findings.append({"field": path, "reason": value_reason})
    return findings
