from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CHANGE_ID = "m22-two-node-docker-readonly-display"
DEFAULT_STATIC_REPORT = Path("artifacts/stage-change") / CHANGE_ID / "static-compose-env-check.json"
DEFAULT_PREFLIGHT_ROOT = Path("artifacts/stage-change") / CHANGE_ID / "docker-preflight"
DEFAULT_DOCKER_SMOKE_ROOT = Path("artifacts/stage-change") / CHANGE_ID / "docker-smoke"
DEFAULT_DOCKER_SECURITY_SUMMARY = Path("artifacts/stage-change") / CHANGE_ID / "docker-security" / "summary.json"
DEFAULT_SOURCE_TRUST_REPORTS = (
    Path("artifacts/stage-change")
    / CHANGE_ID
    / "docker-security"
    / "two-node-docker-source-trust-compute.json",
    Path("artifacts/stage-change")
    / CHANGE_ID
    / "docker-security"
    / "two-node-docker-source-trust-display.json",
)
DEFAULT_APP_DOCKERFILE = Path("infra/docker/Dockerfile.app")
DEFAULT_APP_ENTRYPOINT = Path("infra/docker/entrypoint.sh")
DEFAULT_DOCKERIGNORE = Path(".dockerignore")
DEFAULT_SMOKE_IMAGE = "nhms-app:m22-09-smoke"
DEFAULT_MIN_FREE_GB = 5.0
MAX_COMMAND_OUTPUT_BYTES = 16_384
MAX_SECURITY_CHILD_BYTES = 1024 * 1024
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DOCKER_SECURITY_CHILD_SCHEMAS = {
    "source_trust": "nhms.two_node_docker.source_trust.v1",
    "static": "nhms.two_node_docker.static_check.v1",
    "smoke": "nhms.two_node_docker.app_smoke.v1",
}
DOCKER_REQUIRED_FALSE_PROOFS = (
    "slurm_routes_enabled",
    "slurm_route_available",
    "slurm_cli_present",
    "slurm_config_present",
    "slurm_socket_present",
    "munge_path_present",
    "docker_socket_present",
    "privileged",
    "host_network",
    "host_pid",
    "host_ipc",
    "cap_add_present",
    "forbidden_hostconfig_hazard",
    "forbidden_mount_hazard",
    "forbidden_env_hazard",
    "broad_host_bind_present",
    "private_workspace_bind_present",
    "workspace_mount_present",
    "writable_published_artifact_mount",
    "display_write_capability_present",
)
DOCKER_REQUIRED_TRUE_PROOFS = (
    "published_artifacts_readonly",
    "root_filesystem_readonly",
    "cap_drop_all",
)
DOCKER_STATIC_REQUIRED_PROOFS = (
    "privileged",
    "host_network",
    "host_pid",
    "host_ipc",
    "cap_add_present",
    "forbidden_hostconfig_hazard",
    "forbidden_mount_hazard",
    "forbidden_env_hazard",
    "docker_socket_present",
    "broad_host_bind_present",
    "private_workspace_bind_present",
    "workspace_mount_present",
    "writable_published_artifact_mount",
    "display_write_capability_present",
    "published_artifacts_readonly",
    "root_filesystem_readonly",
    "cap_drop_all",
)
SOURCE_TRUST_COMMON_REQUIRED_LABELS = frozenset(
    {
        "trust path component",
        "checkout root",
        "infra directory",
        "compute compose source",
        "display compose source",
        "env source directory",
        "systemd source directory",
        "compute systemd unit source",
        "display systemd unit source",
    }
)
SOURCE_TRUST_ROLE_LABELS = {
    "compute": "compute role env",
    "display": "display role env",
}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_VAR_PATTERN = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^}]*))?\}")
_COMPOSE_INTERPOLATION_PATTERN = re.compile(
    r"(?<!\$)\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])[^}]*)?\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)
_REQUIRED_MOUNT_ENV_IDENTITY_PATTERN = re.compile(
    r"^(?:\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<operator>:\?|\?)[^}]*)?\}|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*))$"
)

DISPLAY_FORBIDDEN_SCHEDULER_ROOT_ENV_KEYS = frozenset(
    {
        "NHMS_SCHEDULER_LOCK_ROOT",
        "NHMS_SCHEDULER_EVIDENCE_ROOT",
        "NHMS_SCHEDULER_RUNTIME_ROOT",
        "NHMS_SCHEDULER_TEMP_ROOT",
    }
)
DISPLAY_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        "SLURM_GATEWAY_TEMPLATE_DIR",
        "SLURM_GATEWAY_WORKSPACE_DIR",
        "WORKSPACE_ROOT",
        "RUN_WORKSPACE_ROOT",
        "SHARED_LOG_ROOT",
        "NHMS_OBJECT_STORE_COPYBACK_ROOT",
        *DISPLAY_FORBIDDEN_SCHEDULER_ROOT_ENV_KEYS,
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "SHUD_EXECUTABLE",
        "MUNGE_SOCKET",
        "MUNGE_KEY",
        "DOCKER_HOST",
    }
)
DISPLAY_REQUIRED_ENV = {
    "NHMS_SERVICE_ROLE": "display_readonly",
    "NHMS_REQUIRE_SERVICE_ROLE": "true",
    "NHMS_AUTH_MODE": "production",
    "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS": "true",
    "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS": "false",
    "OBJECT_STORE_ROOT": "/home/ghdc/nwm/object-store",
}
COMPUTE_REQUIRED_ENV = {
    "NHMS_SERVICE_ROLE": "compute_control",
    "NHMS_REQUIRE_SERVICE_ROLE": "true",
}
COMPUTE_SCHEDULER_ENV = frozenset(
    {
        "NHMS_SCHEDULER_LOCK_ROOT",
        "NHMS_SCHEDULER_DB_FREE_REQUIRED",
        "NHMS_SCHEDULER_STATE_BACKEND",
        "NHMS_SCHEDULER_LOCK_BACKEND",
        "NHMS_SCHEDULER_REGISTRY_BACKEND",
        "NHMS_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX",
        "NHMS_SCHEDULER_JOURNAL_BACKEND",
        "NHMS_SCHEDULER_JOURNAL_ROOT",
        "NHMS_SCHEDULER_JOURNAL_LOCK_GUARD_MODE",
        "NHMS_SCHEDULER_STATE_INDEX_BACKEND",
        "NHMS_SCHEDULER_STATE_INDEX",
        "NHMS_SCHEDULER_EVIDENCE_ROOT",
        "NHMS_SCHEDULER_RUNTIME_ROOT",
        "NHMS_SCHEDULER_TEMP_ROOT",
        "NHMS_SCHEDULER_ALLOWED_ROOTS",
        "NHMS_SCHEDULER_SOURCES",
        "NHMS_SCHEDULER_MODEL_IDS",
        "NHMS_SCHEDULER_BASIN_IDS",
        "NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC",
        "NHMS_SCHEDULER_INTERVAL_SECONDS",
        "NHMS_SCHEDULER_MAX_PASSES",
        "NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE",
        "NHMS_SCHEDULER_RECONCILE_SLURM_USER",
        "NHMS_SCHEDULER_RECONCILE_SLURM_ACCOUNT",
    }
)
COMPUTE_ADAPTER_ENV = frozenset(
    {
        "GFS_CYCLE_HOURS_UTC",
        "IFS_CYCLE_HOURS_UTC",
    }
)
COMPUTE_SCHEDULER_REQUIRED_ENV = frozenset(
    {
        "NHMS_SCHEDULER_LOCK_ROOT",
        "NHMS_SCHEDULER_DB_FREE_REQUIRED",
        "NHMS_SCHEDULER_STATE_BACKEND",
        "NHMS_SCHEDULER_LOCK_BACKEND",
        "NHMS_SCHEDULER_REGISTRY_BACKEND",
        "NHMS_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX",
        "NHMS_SCHEDULER_JOURNAL_BACKEND",
        "NHMS_SCHEDULER_JOURNAL_ROOT",
        "NHMS_SCHEDULER_JOURNAL_LOCK_GUARD_MODE",
        "NHMS_SCHEDULER_STATE_INDEX_BACKEND",
        "NHMS_SCHEDULER_STATE_INDEX",
        "NHMS_SCHEDULER_EVIDENCE_ROOT",
        "NHMS_SCHEDULER_RUNTIME_ROOT",
        "NHMS_SCHEDULER_TEMP_ROOT",
        "NHMS_SCHEDULER_ALLOWED_ROOTS",
        "NHMS_SCHEDULER_SOURCES",
        "NHMS_SCHEDULER_INTERVAL_SECONDS",
        "NHMS_SCHEDULER_MAX_PASSES",
        "NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE",
        "NHMS_SCHEDULER_RECONCILE_SLURM_USER",
        "NHMS_SCHEDULER_RECONCILE_SLURM_ACCOUNT",
    }
)
CANONICAL_PUBLISHED_ENV = frozenset(
    {
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_S3_BUCKET",
        "NHMS_PUBLISHED_ARTIFACT_S3_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_HOST_ROOT",
    }
)
LEGACY_PUBLISHED_ENV = "PUBLISHED_ARTIFACT_ROOT"
COMPUTE_REQUIRED_RUNTIME_ENV = frozenset(
    {
        "NHMS_SERVICE_ROLE",
        "NHMS_REQUIRE_SERVICE_ROLE",
        "NHMS_AUTH_MODE",
        "NHMS_REQUIRE_FORECAST_WARM_START",
        "UV_CACHE_DIR",
        "DATABASE_URL",
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "NHMS_OBJECT_STORE_COPYBACK_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_S3_BUCKET",
        "NHMS_PUBLISHED_ARTIFACT_S3_PREFIX",
        *COMPUTE_SCHEDULER_REQUIRED_ENV,
        *COMPUTE_ADAPTER_ENV,
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "SLURM_GATEWAY_TEMPLATE_DIR",
        "SLURM_GATEWAY_WORKSPACE_DIR",
    }
)
COMPUTE_SCHEDULER_ONCE_REQUIRED_RUNTIME_ENV = COMPUTE_REQUIRED_RUNTIME_ENV - frozenset({"DATABASE_URL"})
COMPUTE_DB_FREE_SCHEDULER_SELECTOR_ENV = frozenset(
    {
        "NHMS_SCHEDULER_STATE_BACKEND",
        "NHMS_SCHEDULER_LOCK_BACKEND",
        "NHMS_SCHEDULER_REGISTRY_BACKEND",
        "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND",
        "NHMS_SCHEDULER_JOURNAL_BACKEND",
        "NHMS_SCHEDULER_STATE_INDEX_BACKEND",
    }
)
COMPUTE_DB_FREE_SCHEDULER_PATH_ENV = frozenset(
    {
        "NHMS_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX",
        "NHMS_SCHEDULER_JOURNAL_ROOT",
        "NHMS_SCHEDULER_STATE_INDEX",
    }
)
DISPLAY_REQUIRED_RUNTIME_ENV = frozenset(
    {
        "NHMS_SERVICE_ROLE",
        "NHMS_REQUIRE_SERVICE_ROLE",
        "NHMS_AUTH_MODE",
        "UV_CACHE_DIR",
        "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS",
        "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT",
        "DATABASE_URL",
        "OBJECT_STORE_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_S3_BUCKET",
        "NHMS_PUBLISHED_ARTIFACT_S3_PREFIX",
        "NHMS_LOG_TAIL_MAX_BYTES",
        "NHMS_ARTIFACT_BACKEND",
    }
)
NONEMPTY_RUNTIME_ENV = frozenset(
    {
        "NHMS_SERVICE_ROLE",
        "NHMS_REQUIRE_SERVICE_ROLE",
        "NHMS_AUTH_MODE",
        "UV_CACHE_DIR",
        "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS",
        "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS",
        "DATABASE_URL",
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "NHMS_OBJECT_STORE_COPYBACK_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "SLURM_GATEWAY_TEMPLATE_DIR",
        "SLURM_GATEWAY_WORKSPACE_DIR",
        *COMPUTE_SCHEDULER_REQUIRED_ENV,
        "NHMS_LOG_TAIL_MAX_BYTES",
        "NHMS_ARTIFACT_BACKEND",
    }
)
COMPUTE_ONLY_PATH_ENV_KEYS = frozenset(
    {
        "WORKSPACE_ROOT",
        "RUN_WORKSPACE_ROOT",
        "SHARED_LOG_ROOT",
        "NHMS_OBJECT_STORE_COPYBACK_ROOT",
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "NHMS_SCHEDULER_LOCK_ROOT",
        "NHMS_SCHEDULER_EVIDENCE_ROOT",
        "NHMS_SCHEDULER_RUNTIME_ROOT",
        "NHMS_SCHEDULER_TEMP_ROOT",
        "SLURM_GATEWAY_TEMPLATE_DIR",
        "SLURM_GATEWAY_WORKSPACE_DIR",
        "MUNGE_SOCKET",
        "MUNGE_KEY",
        "SHUD_EXECUTABLE",
    }
)
BROAD_HOST_ROOTS = frozenset({"/", "/root", "/home", "/etc", "/run", "/var", "/scratch"})
FORBIDDEN_MOUNT_TOKENS = frozenset(
    {
        "/etc/slurm",
        "/etc/munge",
        "/run/munge",
        "/var/run/munge",
        "munge.key",
        ".nhms-runs",
        "WORKSPACE_ROOT",
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "MUNGE_SOCKET",
        "MUNGE_KEY",
    }
)
PREFLIGHT_COMMANDS = (
    ("docker_version", ("docker", "version")),
    ("docker_compose_version", ("docker", "compose", "version")),
    ("docker_info_docker_root", ("docker", "info", "--format", "{{json .DockerRootDir}}")),
    ("docker_system_df", ("docker", "system", "df")),
    ("df_h", ("df", "-h")),
)
DISPLAY_DYNAMIC_HOSTCONFIG_FIELDS = (
    "privileged",
    "network_mode",
    "pid",
    "ipc",
    "cap_add",
    "cap_drop",
    "security_opt",
    "read_only",
    "devices",
    "device_cgroup_rules",
    "device_requests",
)
PUBLISHED_ARTIFACT_MOUNT_SOURCE_VAR = "NHMS_PUBLISHED_ARTIFACT_HOST_ROOT"
PUBLISHED_ARTIFACT_MOUNT_TARGET_VAR = "NHMS_PUBLISHED_ARTIFACT_ROOT"
DISPLAY_OBJECT_STORE_MOUNT_SOURCE_VAR = "OBJECT_STORE_ROOT"
DISPLAY_OBJECT_STORE_MOUNT_TARGET_VAR = "OBJECT_STORE_ROOT"
DISPLAY_ALLOWED_TMPFS_TARGETS = frozenset({"/tmp", "/run"})
DISPLAY_AUDITED_INTERPOLATION_ENV = frozenset(
    {
        "NHMS_APP_IMAGE",
        "NHMS_IMAGE_TAG",
        "NHMS_CONTAINER_UID",
        "NHMS_CONTAINER_GID",
        "NHMS_DISPLAY_API_PORT",
        "NHMS_PUBLISHED_ARTIFACT_HOST_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_S3_BUCKET",
        "NHMS_PUBLISHED_ARTIFACT_S3_PREFIX",
        "OBJECT_STORE_ROOT",
        "DATABASE_URL",
        "NHMS_SERVICE_ROLE",
        "NHMS_REQUIRE_SERVICE_ROLE",
        "NHMS_AUTH_MODE",
        "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS",
        "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT",
        "NHMS_LOG_TAIL_MAX_BYTES",
        "NHMS_ARTIFACT_BACKEND",
        "OBJECT_STORE_PREFIX",
        "S3_ENDPOINT_URL",
        "S3_BUCKET_NAME",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "CORS_ALLOWED_ORIGINS",
    }
)
DISPLAY_OPTIONAL_RUNTIME_ENV = frozenset(
    {
        "OBJECT_STORE_PREFIX",
        "S3_ENDPOINT_URL",
        "S3_BUCKET_NAME",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "CORS_ALLOWED_ORIGINS",
    }
)
COMPUTE_AUDITED_INTERPOLATION_ENV = frozenset(
    {
        "NHMS_APP_IMAGE",
        "NHMS_IMAGE_TAG",
        "NHMS_CONTAINER_UID",
        "NHMS_CONTAINER_GID",
        "NHMS_SUPPLEMENTAL_GID",
        "NHMS_AUTH_MODE",
        "NHMS_REQUIRE_FORECAST_WARM_START",
        "DATABASE_URL",
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "NHMS_OBJECT_STORE_COPYBACK_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_HOST_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_S3_BUCKET",
        "NHMS_PUBLISHED_ARTIFACT_S3_PREFIX",
        "NHMS_SCHEDULER_LOCK_ROOT",
        "NHMS_SCHEDULER_EVIDENCE_ROOT",
        "NHMS_SCHEDULER_RUNTIME_ROOT",
        "NHMS_SCHEDULER_TEMP_ROOT",
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "SHUD_EXECUTABLE",
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        "SLURM_GATEWAY_TEMPLATE_DIR",
        "SLURM_GATEWAY_WORKSPACE_DIR",
        *COMPUTE_SCHEDULER_ENV,
        *COMPUTE_ADAPTER_ENV,
        "GFS_NOMADS_BASE_URL",
        "IFS_OPEN_DATA_SOURCE",
    }
)
COMPUTE_AUDITED_RUNTIME_ENV = frozenset(
    {
        "NHMS_SERVICE_ROLE",
        "NHMS_REQUIRE_SERVICE_ROLE",
        "NHMS_AUTH_MODE",
        "NHMS_REQUIRE_FORECAST_WARM_START",
        "UV_CACHE_DIR",
        "DATABASE_URL",
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "NHMS_OBJECT_STORE_COPYBACK_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_S3_BUCKET",
        "NHMS_PUBLISHED_ARTIFACT_S3_PREFIX",
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "SHUD_EXECUTABLE",
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        "SLURM_GATEWAY_TEMPLATE_DIR",
        "SLURM_GATEWAY_WORKSPACE_DIR",
        *COMPUTE_SCHEDULER_ENV,
        *COMPUTE_ADAPTER_ENV,
        "GFS_NOMADS_BASE_URL",
        "IFS_OPEN_DATA_SOURCE",
    }
)
COMPUTE_CANONICAL_RUNTIME_ENV = frozenset(
    {
        "NHMS_SERVICE_ROLE",
        "NHMS_REQUIRE_SERVICE_ROLE",
        "NHMS_AUTH_MODE",
        "DATABASE_URL",
        "WORKSPACE_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_S3_BUCKET",
        "NHMS_PUBLISHED_ARTIFACT_S3_PREFIX",
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
    }
)
DISPLAY_AUDITED_RUNTIME_ENV = (
    DISPLAY_REQUIRED_RUNTIME_ENV | DISPLAY_OPTIONAL_RUNTIME_ENV | frozenset(DISPLAY_REQUIRED_ENV)
)
SECRET_ENV_KEYS = frozenset({"DATABASE_URL", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID"})
MAX_COMPOSE_INTERPOLATION_TEXT_CHARS = 65_536
MAX_COMPOSE_INTERPOLATION_DEPTH = 64
MAX_COMPOSE_INTERPOLATION_OBJECT_NODES = 20_000
MAX_COMPOSE_INTERPOLATION_OCCURRENCES = 4_096
API_SERVICE_COMMAND = (
    "uv",
    "run",
    "python",
    "-m",
    "uvicorn",
    "apps.api.main:app",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
)
COMPUTE_SCHEDULER_COMMAND = ("uv", "run", "nhms-pipeline", "plan-production", "--plan")
COMPUTE_SCHEDULER_HELP_COMMAND = ("uv", "run", "nhms-pipeline", "plan-production", "--help")
COMPUTE_REQUIRED_EXTRA_HOST = "host.docker.internal:host-gateway"
DISPLAY_LOCALHOST_PROBE_SCRIPT = r"""
import json
import time
import urllib.error
import urllib.request

def wait_health() -> None:
    last_error = None
    for _ in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=1) as response:
                if response.status == 200:
                    return
                last_error = f"unexpected health status {response.status}"
        except Exception as error:
            last_error = str(error)
        time.sleep(0.25)
    raise SystemExit(f"display API health check did not become ready: {last_error}")

wait_health()
with urllib.request.urlopen("http://127.0.0.1:8000/", timeout=2) as response:
    if response.status != 200:
        raise SystemExit(f"frontend fallback returned unexpected status {response.status}")
with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/runtime/config", timeout=2) as response:
    payload = json.load(response)
data = payload.get("data", {})
if data.get("service_role") != "display_readonly":
    raise SystemExit("runtime config did not report display_readonly")
if data.get("display_readonly") is not True:
    raise SystemExit("runtime config display_readonly flag was not true")
if data.get("slurm_routes_enabled") is not False:
    raise SystemExit("runtime config slurm_routes_enabled was not false")
try:
    urllib.request.urlopen("http://127.0.0.1:8000/api/v1/slurm/health", timeout=2)
except urllib.error.HTTPError as error:
    if error.code == 404:
        raise SystemExit(0)
    raise
raise SystemExit("display_readonly unexpectedly served /api/v1/slurm/health")
"""
APP_IMAGE_FORBIDDEN_BINARIES = (
    "sbatch",
    "scancel",
    "squeue",
    "srun",
    "sacct",
    "sinfo",
    "scontrol",
    "munge",
    "unmunge",
)
APP_IMAGE_FORBIDDEN_PATHS = ("/etc/slurm", "/run/munge", "/etc/munge", "/var/run/munge")
REQUIRED_DOCKERIGNORE_PATTERNS = frozenset(
    {
        ".venv",
        "apps/frontend/node_modules",
        "artifacts",
        "SHUD",
        "rSHUD",
        "AutoSHUD",
        ".env",
        ".env.*",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "pip.conf",
        "id_rsa",
        "id_rsa*",
        "id_ed25519",
        "id_ed25519*",
        "*.pem",
        "*.key",
        ".aws",
        ".ssh",
        "secrets",
        "**/.env",
        "**/.env.*",
        "**/.npmrc",
        "**/.pypirc",
        "**/.netrc",
        "**/pip.conf",
        "**/id_rsa",
        "**/id_rsa*",
        "**/id_ed25519",
        "**/id_ed25519*",
        "**/*.pem",
        "**/*.key",
        "**/.aws",
        "**/.ssh",
        "**/secrets",
    }
)
_NO_FIELD_CONTRACT = object()
_FIELD_MUST_BE_ABSENT = object()


class ComposeInterpolationLimitError(ValueError):
    def __init__(self, message: str, *, metric: str, limit: int) -> None:
        super().__init__(message)
        self.metric = metric
        self.limit = limit


_SECRET_ENV_KEY_ALTERNATION = "|".join(re.escape(key) for key in sorted(SECRET_ENV_KEYS, key=len, reverse=True))
_SECRET_MAPPING_LINE_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])[\"']?(?:{_SECRET_ENV_KEY_ALTERNATION})[\"']?\s*(?:=|:)\s*"
)
_SECRET_INTERPOLATION_PATTERN = re.compile(rf"\$(?:\{{)?(?P<key>{_SECRET_ENV_KEY_ALTERNATION})(?=\b|[:+\-?}}])")
_SECRET_URL_PATTERN = re.compile(r"\b(?:postgres|postgresql|mysql|mariadb|mongodb|redis)://[^\s,;}\]]+")
_SECRET_DETAIL_VALUE_FIELDS = frozenset(
    {
        "actual",
        "actual_rendered",
        "candidate_value",
        "device",
        "entry",
        "env_file_value",
        "error",
        "expression",
        "literal_value",
        "path",
        "process_value",
        "rendered_value",
        "root",
        "source",
        "target",
        "tmpfs",
        "volume",
        "expected",
        "expected_rendered",
        "value",
        "raw_value",
    }
)
_SECRET_PRESERVED_VALUE_FIELDS = frozenset(
    {
        "candidate_key",
        "field",
        "key",
        "operator",
        "root_key",
        "source_key",
        "target_key",
    }
)


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    severity: str = "error"
    path: str | None = None
    service: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.service is not None:
            payload["service"] = self.service
        if self.details:
            payload["details"] = _redact_finding_details(self.details)
        return payload


@dataclass(frozen=True)
class StaticCheckResult:
    status: str
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "finding_count": len(self.findings),
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class ComposeInterpolationOccurrence:
    key: str
    operator: str | None
    payload: str
    expression: str
    literal_prefix: str = ""


@dataclass(frozen=True)
class ComposeDollarRunToken:
    start: int
    end: int
    literal_dollars: str
    occurrence: ComposeInterpolationOccurrence | None = None


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, Any]:
        stdout = _bounded_command_output(self.stdout)
        stderr = _bounded_command_output(self.stderr)
        return {
            "args": list(self.args),
            "returncode": self.returncode,
            "stdout": stdout["text"],
            "stderr": stderr["text"],
            "output_truncation": {
                "max_bytes_per_stream": MAX_COMMAND_OUTPUT_BYTES,
                "stdout": {
                    "original_bytes": stdout["original_bytes"],
                    "stored_bytes": stdout["stored_bytes"],
                    "truncated": stdout["truncated"],
                },
                "stderr": {
                    "original_bytes": stderr["original_bytes"],
                    "stored_bytes": stderr["stored_bytes"],
                    "truncated": stderr["truncated"],
                },
            },
        }


def _bounded_command_output(value: str) -> dict[str, Any]:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= MAX_COMMAND_OUTPUT_BYTES:
        return {
            "text": value,
            "original_bytes": len(raw),
            "stored_bytes": len(raw),
            "truncated": False,
        }
    bounded = raw[:MAX_COMMAND_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return {
        "text": bounded,
        "original_bytes": len(raw),
        "stored_bytes": len(bounded.encode("utf-8", errors="replace")),
        "truncated": True,
    }


@dataclass(frozen=True)
class DiskSpace:
    total: int
    used: int
    free: int

    def to_dict(self) -> dict[str, Any]:
        return {"total_bytes": self.total, "used_bytes": self.used, "free_bytes": self.free}


@dataclass(frozen=True)
class PreflightResult:
    status: str
    evidence_path: Path
    blockers: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class DockerSmokeResult:
    status: str
    evidence_path: Path
    blockers: tuple[dict[str, Any], ...]


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"{path}:{line_number}: empty environment key")
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        env[key] = value
    return env


def load_compose(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: compose file must be a YAML mapping")
    return payload


def run_static_check(
    *,
    compute_compose: Path,
    display_compose: Path,
    compute_env: Path,
    display_env: Path,
    repo_root: Path,
    app_dockerfile: Path = DEFAULT_APP_DOCKERFILE,
    app_entrypoint: Path = DEFAULT_APP_ENTRYPOINT,
    dockerignore: Path = DEFAULT_DOCKERIGNORE,
) -> StaticCheckResult:
    repo_root = repo_root.resolve()
    compute_compose = _resolve_path(compute_compose, repo_root)
    display_compose = _resolve_path(display_compose, repo_root)
    compute_env = _resolve_path(compute_env, repo_root)
    display_env = _resolve_path(display_env, repo_root)

    findings: list[Finding] = []
    findings.extend(_dev_compose_findings(compute_compose, repo_root, role="compute"))
    findings.extend(_dev_compose_findings(display_compose, repo_root, role="display"))
    findings.extend(
        _validate_app_docker_assets(
            repo_root=repo_root,
            dockerfile=app_dockerfile,
            entrypoint=app_entrypoint,
            dockerignore=dockerignore,
        )
    )

    try:
        compute_env_map = parse_env_file(compute_env)
        display_env_map = parse_env_file(display_env)
        compute_yaml = load_compose(compute_compose)
        display_yaml = load_compose(display_compose)

        findings.extend(
            _compose_interpolation_contract_findings(compute_compose, compute_yaml, compute_env_map, role="compute")
        )
        findings.extend(
            _compose_interpolation_contract_findings(display_compose, display_yaml, display_env_map, role="display")
        )
        findings.extend(_validate_env_file(compute_env, compute_env_map, role="compute"))
        findings.extend(_validate_env_file(display_env, display_env_map, role="display"))
        findings.extend(_validate_compute_compose(compute_compose, compute_yaml, compute_env_map))
        findings.extend(_validate_display_compose(display_compose, display_yaml, display_env_map, compute_env_map))
    except ComposeInterpolationLimitError as error:
        findings.append(_compose_interpolation_limit_finding(error))

    status = "PASS" if not findings else "FAIL"
    return StaticCheckResult(status=status, findings=tuple(findings))


def _static_proof_payload(result: StaticCheckResult) -> dict[str, Any]:
    codes = {finding.code for finding in result.findings}
    proof_values: dict[str, bool] = {
        "slurm_routes_enabled": False,
        "slurm_route_available": False,
        "slurm_cli_present": "APP_DOCKERFILE_FORBIDDEN_SLURM_MUNGE_INSTALL" in codes,
        "slurm_config_present": False,
        "slurm_socket_present": _has_finding_code(codes, "DISPLAY_FORBIDDEN_MOUNT"),
        "munge_path_present": _has_finding_code(codes, "DISPLAY_FORBIDDEN_MOUNT")
        or "APP_DOCKERFILE_FORBIDDEN_SLURM_MUNGE_INSTALL" in codes,
        "docker_socket_present": _has_finding_code(codes, "DISPLAY_FORBIDDEN_MOUNT")
        or "DISPLAY_NAMED_VOLUME_FORBIDDEN_SOURCE" in codes
        or "DISPLAY_HOST_DEVICE_UNSUPPORTED" in codes,
        "privileged": "DISPLAY_HOSTCONFIG_PRIVILEGED" in codes,
        "host_network": "DISPLAY_HOSTCONFIG_HOST_NETWORK" in codes,
        "host_pid": "DISPLAY_HOSTCONFIG_HOST_PID" in codes,
        "host_ipc": "DISPLAY_HOSTCONFIG_HOST_IPC" in codes,
        "cap_add_present": "DISPLAY_HOSTCONFIG_CAP_ADD" in codes,
        "forbidden_hostconfig_hazard": _has_finding_code(
            codes,
            "DISPLAY_HOSTCONFIG_",
            "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED",
            "DISPLAY_DEPLOY_UNSUPPORTED",
            "DISPLAY_HOST_DEVICE_UNSUPPORTED",
            "DISPLAY_DEVICE_CGROUP_RULE_UNSUPPORTED",
            "DISPLAY_DEVICE_REQUEST_UNSUPPORTED",
        ),
        "forbidden_mount_hazard": _has_finding_code(
            codes,
            "DISPLAY_FORBIDDEN_MOUNT",
            "DISPLAY_UNAPPROVED_MOUNT",
            "DISPLAY_UNAPPROVED_WRITABLE_MOUNT",
            "DISPLAY_ARTIFACT_OVERLAY_MOUNT",
            "DISPLAY_RELATIVE_MOUNT_SOURCE",
            "DISPLAY_NAMED_VOLUME_FORBIDDEN_SOURCE",
            "DISPLAY_CONFIG_UNSUPPORTED",
            "DISPLAY_SECRET_UNSUPPORTED",
            "DISPLAY_VOLUMES_FROM_UNSUPPORTED",
        ),
        "forbidden_env_hazard": _has_finding_code(
            codes,
            "DISPLAY_FORBIDDEN_ENV",
            "DISPLAY_ENV_FILE_UNSUPPORTED",
            "DISPLAY_RUNTIME_ENV_VALUE_INVALID",
            "DISPLAY_RUNTIME_ENV_ALIAS_INTERPOLATION",
            "DISPLAY_RUNTIME_ENV_NULL_IMPORT",
            "DISPLAY_RUNTIME_ENV_LITERAL_DRIFT",
            "LEGACY_PUBLISHED_ARTIFACT_ENV",
            "COMPOSE_ONLY_ENV_LEAK",
        ),
        "broad_host_bind_present": "DISPLAY_BROAD_HOST_ROOT_BIND" in codes,
        "private_workspace_bind_present": _has_finding_code(codes, "DISPLAY_FORBIDDEN_MOUNT"),
        "workspace_mount_present": _has_finding_code(codes, "DISPLAY_FORBIDDEN_MOUNT"),
        "writable_published_artifact_mount": "DISPLAY_PUBLISHED_MOUNT_NOT_READONLY" in codes,
        "display_write_capability_present": _has_finding_code(
            codes,
            "DISPLAY_ROOT_FILESYSTEM_WRITABLE",
            "DISPLAY_HOSTCONFIG_CAP_DROP_INVALID",
            "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID",
            "DISPLAY_UNAPPROVED_WRITABLE_MOUNT",
        ),
        "published_artifacts_readonly": "DISPLAY_PUBLISHED_MOUNT_NOT_READONLY" not in codes
        and "DISPLAY_PUBLISHED_MOUNT_MISSING" not in codes,
        "root_filesystem_readonly": "DISPLAY_ROOT_FILESYSTEM_WRITABLE" not in codes,
        "cap_drop_all": "DISPLAY_HOSTCONFIG_CAP_DROP_INVALID" not in codes,
    }
    proof_sources = {
        "finding_codes": sorted(codes),
        "required_false": list(DOCKER_REQUIRED_FALSE_PROOFS),
        "required_true": list(DOCKER_REQUIRED_TRUE_PROOFS),
        "static_required": list(DOCKER_STATIC_REQUIRED_PROOFS),
    }
    return {**proof_values, "proofs": {"static_compose_env_checked": result.status == "PASS", **proof_sources}}


def _has_finding_code(codes: set[str], *needles: str) -> bool:
    for code in codes:
        if any(code == needle or code.startswith(needle) for needle in needles):
            return True
    return False


def run_preflight(
    *,
    evidence_root: Path,
    repo_root: Path,
    evidence_run_id: str | None = None,
    min_free_bytes: int = int(DEFAULT_MIN_FREE_GB * 1024**3),
    command_runner: Callable[[Sequence[str]], CommandResult] | None = None,
    disk_usage_provider: Callable[[Path], DiskSpace] | None = None,
) -> PreflightResult:
    repo_root = repo_root.resolve()
    evidence_root = ensure_approved_evidence_root(evidence_root, repo_root)
    resolved_run_id = evidence_run_id or _evidence_run_id_from_output(evidence_root)
    if resolved_run_id is not None:
        resolved_run_id = _safe_evidence_run_id(resolved_run_id)
    evidence_root.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_root / "docker-preflight.json"
    runner = command_runner or _run_command
    disk_provider = disk_usage_provider or _disk_usage
    tmpdir, tmpdir_blocker = _approved_preflight_tmpdir(repo_root)
    blockers: list[dict[str, Any]] = []
    if tmpdir_blocker:
        blockers.append(tmpdir_blocker)

    commands: dict[str, CommandResult]
    if tmpdir_blocker:
        commands = _skipped_preflight_commands("TMPDIR is outside approved evidence roots.")
    else:
        tmpdir.mkdir(parents=True, exist_ok=True)
        with _temporary_tmpdir_env(tmpdir):
            commands = {name: runner(command) for name, command in PREFLIGHT_COMMANDS}

    docker_root = _parse_docker_root(commands["docker_info_docker_root"].stdout)
    disk_paths = {
        "evidence_root": evidence_root,
    }
    if not tmpdir_blocker:
        disk_paths["tmpdir"] = tmpdir
    if docker_root:
        disk_paths["docker_root"] = Path(docker_root)

    disk: dict[str, dict[str, Any]] = {}
    for label, path in disk_paths.items():
        try:
            snapshot = disk_provider(path)
        except OSError as error:
            blockers.append(
                {
                    "code": "DISK_USAGE_UNAVAILABLE",
                    "label": label,
                    "path": str(path),
                    "message": str(error),
                }
            )
            continue
        disk[label] = {"path": str(path), **snapshot.to_dict()}
        if snapshot.free < min_free_bytes:
            blockers.append(
                {
                    "code": "LOW_DISK_SPACE",
                    "label": label,
                    "path": str(path),
                    "free_bytes": snapshot.free,
                    "min_free_bytes": min_free_bytes,
                }
            )

    if not tmpdir_blocker:
        blockers.extend(_docker_command_blockers(commands, docker_root=docker_root))
    status = "BLOCKED" if blockers else "PASS"

    payload = {
        "schema_version": "nhms.two_node_docker.preflight.v1",
        "change_id": CHANGE_ID,
        "status": status,
        "evidence_run_id": resolved_run_id,
        "checked_at": _now_iso(),
        "evidence_root": str(evidence_root),
        "tmpdir": str(tmpdir),
        "docker_root_dir": docker_root,
        "min_free_bytes": min_free_bytes,
        "commands": {name: result.to_dict() for name, result in commands.items()},
        "disk": disk,
        "blockers": blockers,
    }
    _write_json_atomic_replace(evidence_path, payload)
    return PreflightResult(status=status, evidence_path=evidence_path, blockers=tuple(blockers))


def ensure_approved_evidence_root(path: Path, repo_root: Path) -> Path:
    resolved = _resolve_path(path, repo_root)
    artifacts_root = (repo_root / "artifacts").resolve()
    scratch_root = Path("/scratch/frd_muziyao").resolve()
    if _is_relative_to(resolved, repo_root):
        if _is_relative_to(resolved, artifacts_root):
            return resolved
        raise ValueError(
            "repository-local evidence/temp paths must be under artifacts/: "
            f"{resolved}"
        )
    if _is_relative_to(resolved, scratch_root):
        return resolved
    raise ValueError(
        "evidence/temp root must be under repository artifacts/ or external /scratch/frd_muziyao: "
        f"{resolved}"
    )


def write_static_report(
    result: StaticCheckResult,
    report_path: Path,
    repo_root: Path,
    *,
    evidence_run_id: str | None = None,
) -> Path:
    report_path = _resolve_output_path(report_path, repo_root)
    ensure_approved_evidence_root(report_path.parent, repo_root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_run_id = evidence_run_id or _evidence_run_id_from_output(report_path)
    if resolved_run_id is not None:
        resolved_run_id = _safe_evidence_run_id(resolved_run_id)
    payload = {
        "schema_version": "nhms.two_node_docker.static_check.v1",
        "change_id": CHANGE_ID,
        "evidence_run_id": resolved_run_id,
        "checked_at": _now_iso(),
        **result.to_dict(),
        **_static_proof_payload(result),
    }
    _write_json_atomic_replace(report_path, payload)
    return report_path


def write_docker_security_summary(
    *,
    output: Path,
    repo_root: Path,
    evidence_run_id: str,
    source_trust_report: Path | Sequence[Path],
    static_report: Path,
    smoke_report: Path,
) -> Path:
    output = _resolve_output_path(output, repo_root)
    ensure_approved_evidence_root(output.parent, repo_root)
    evidence_run_id = _safe_evidence_run_id(evidence_run_id)
    summary_root = output.parent
    approved_child_roots = _docker_security_child_roots(repo_root, summary_root)
    source_trust_reports = _source_trust_report_paths(source_trust_report)
    source_trust_artifacts = [
        _artifact_summary(
            "source_trust",
            report,
            repo_root=repo_root,
            approved_roots=approved_child_roots,
        )
        for report in source_trust_reports
    ]
    source_artifacts = {
        "source_trust": source_trust_artifacts[0] if len(source_trust_artifacts) == 1 else source_trust_artifacts,
        "static": _artifact_summary(
            "static",
            static_report,
            repo_root=repo_root,
            approved_roots=approved_child_roots,
        ),
        "smoke": _artifact_summary(
            "smoke",
            smoke_report,
            repo_root=repo_root,
            approved_roots=approved_child_roots,
        ),
    }
    source_trust_payloads = [
        _read_security_child_payload(
            "source_trust",
            artifact,
            approved_roots=approved_child_roots,
            evidence_run_id=evidence_run_id,
        )
        for artifact in source_trust_artifacts
    ]
    source_trust_payload = _combine_source_trust_payloads(source_trust_payloads)
    static_payload = _read_security_child_payload(
        "static",
        source_artifacts["static"],
        approved_roots=approved_child_roots,
        evidence_run_id=evidence_run_id,
    )
    smoke_payload = _read_security_child_payload(
        "smoke",
        source_artifacts["smoke"],
        approved_roots=approved_child_roots,
        evidence_run_id=evidence_run_id,
    )
    status = _docker_security_summary_status(source_trust_payload, static_payload, smoke_payload)
    runtime_config = {
        "service_role": "display_readonly",
        "display_readonly": True,
        "slurm_routes_enabled": False,
    }
    payload = {
        "schema_version": "nhms.two_node_docker.security_summary.v1",
        "change_id": CHANGE_ID,
        "status": status,
        "checked_at": _now_iso(),
        "evidence_run_id": evidence_run_id,
        "live_docker_evidence": smoke_payload.get("status") == "PASS",
        "runtime_config": runtime_config,
        "source_artifacts": source_artifacts,
        "source_statuses": {
            "source_trust": source_trust_payload.get("status"),
            "static": static_payload.get("status"),
            "smoke": smoke_payload.get("status"),
        },
        "runtime": {
            "image_tag": smoke_payload.get("image_tag"),
            "dockerfile": smoke_payload.get("dockerfile"),
        },
        "slurm_routes_unavailable": smoke_payload.get("status") == "PASS",
        "slurm_route_available": False,
        "published_artifacts_readonly": static_payload.get("status") == "PASS",
        "root_filesystem_readonly": static_payload.get("status") == "PASS",
        "cap_drop_all": static_payload.get("status") == "PASS",
        "docker_socket_present": False,
        "slurm_routes_enabled": False,
        "slurm_cli_present": False,
        "slurm_config_present": False,
        "slurm_socket_present": False,
        "munge_path_present": False,
        "privileged": False,
        "host_network": False,
        "host_pid": False,
        "host_ipc": False,
        "cap_add_present": False,
        "forbidden_hostconfig_hazard": False,
        "forbidden_mount_hazard": False,
        "forbidden_env_hazard": False,
        "broad_host_bind_present": False,
        "private_workspace_bind_present": False,
        "workspace_mount_present": False,
        "writable_published_artifact_mount": False,
        "display_write_capability_present": False,
        "proofs": {
            "source_trust_passed": source_trust_payload.get("status") == "PASS",
            "static_passed": static_payload.get("status") == "PASS",
            "smoke_passed": smoke_payload.get("status") == "PASS",
            "live_container_checked": smoke_payload.get("status") == "PASS",
            "source_trust_roles": sorted(_source_trust_proven_roles(source_trust_payload)),
        },
        "blockers": _docker_security_summary_blockers(source_trust_payload, static_payload, smoke_payload),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic_replace(output, payload)
    return output


def run_docker_smoke(
    *,
    evidence_root: Path,
    repo_root: Path,
    evidence_run_id: str | None = None,
    image_tag: str = DEFAULT_SMOKE_IMAGE,
    dockerfile: Path = DEFAULT_APP_DOCKERFILE,
    min_free_bytes: int = int(DEFAULT_MIN_FREE_GB * 1024**3),
    command_runner: Callable[[Sequence[str]], CommandResult] | None = None,
    disk_usage_provider: Callable[[Path], DiskSpace] | None = None,
) -> DockerSmokeResult:
    repo_root = repo_root.resolve()
    evidence_root = ensure_approved_evidence_root(evidence_root, repo_root)
    resolved_run_id = evidence_run_id or _evidence_run_id_from_output(evidence_root)
    if resolved_run_id is not None:
        resolved_run_id = _safe_evidence_run_id(resolved_run_id)
    evidence_root.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_root / "docker-smoke.json"
    preflight_runner = command_runner or _run_command
    runner = command_runner or _run_docker_smoke_command
    blockers: list[dict[str, Any]] = []
    commands: dict[str, CommandResult] = {}

    preflight_root = evidence_root / "preflight"
    preflight = run_preflight(
        evidence_root=preflight_root,
        repo_root=repo_root,
        evidence_run_id=resolved_run_id,
        min_free_bytes=min_free_bytes,
        command_runner=preflight_runner,
        disk_usage_provider=disk_usage_provider,
    )
    if preflight.status != "PASS":
        blockers.append(
            {
                "code": "DOCKER_PREFLIGHT_BLOCKED",
                "preflight_evidence_path": str(preflight.evidence_path),
                "blockers": list(preflight.blockers),
            }
        )
        payload = _docker_smoke_payload(
            status="BLOCKED",
            evidence_root=evidence_root,
            repo_root=repo_root,
            evidence_run_id=resolved_run_id,
            image_tag=image_tag,
            dockerfile=dockerfile,
            commands=commands,
            blockers=blockers,
            preflight=preflight,
        )
        _write_json_atomic_replace(evidence_path, payload)
        return DockerSmokeResult(status="BLOCKED", evidence_path=evidence_path, blockers=tuple(blockers))

    dockerfile_path = _resolve_path(dockerfile, repo_root)
    build_command = ("docker", "build", "-f", str(dockerfile_path), "-t", image_tag, str(repo_root))
    run_checks_command = (
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "/bin/sh",
        image_tag,
        "-c",
        _image_absence_probe_script(),
    )
    display_reject_command = (
        "docker",
        "run",
        "--rm",
        "-e",
        "NHMS_REQUIRE_SERVICE_ROLE=true",
        "-e",
        "NHMS_SERVICE_ROLE=display_readonly",
        "-e",
        "WORKSPACE_ROOT=/workspace",
        image_tag,
        "true",
    )
    slurm_gateway_reject_command = (
        "docker",
        "run",
        "--rm",
        "-e",
        "NHMS_REQUIRE_SERVICE_ROLE=true",
        "-e",
        "NHMS_SERVICE_ROLE=slurm_gateway",
        image_tag,
        "true",
    )
    compute_scheduler_command = (
        "docker",
        "run",
        "--rm",
        "-e",
        "NHMS_REQUIRE_SERVICE_ROLE=true",
        "-e",
        "NHMS_SERVICE_ROLE=compute_control",
        image_tag,
        *COMPUTE_SCHEDULER_HELP_COMMAND,
    )
    display_scheduler_reject_command = (
        "docker",
        "run",
        "--rm",
        "-e",
        "NHMS_REQUIRE_SERVICE_ROLE=true",
        "-e",
        "NHMS_SERVICE_ROLE=display_readonly",
        image_tag,
        *COMPUTE_SCHEDULER_COMMAND,
    )

    preflight_payload = json.loads(preflight.evidence_path.read_text(encoding="utf-8"))
    with _temporary_tmpdir_env(Path(preflight_payload["tmpdir"])):
        commands["docker_build"] = runner(build_command)
        if commands["docker_build"].returncode != 0:
            blockers.append(
                {
                    "code": _docker_build_failure_code(commands["docker_build"]),
                    "command": list(build_command),
                    "returncode": commands["docker_build"].returncode,
                }
            )
        else:
            commands["image_inspect"] = runner(("docker", "image", "inspect", image_tag))
            if commands["image_inspect"].returncode == 0:
                commands["image_absence_probe"] = runner(run_checks_command)
                commands["display_compute_env_reject"] = runner(display_reject_command)
                commands["slurm_gateway_reject"] = runner(slurm_gateway_reject_command)
                commands["compute_scheduler_command"] = runner(compute_scheduler_command)
                commands["display_scheduler_reject"] = runner(display_scheduler_reject_command)
                commands.update(_run_display_startup_probe(runner, image_tag=image_tag))
            blockers.extend(_docker_smoke_command_blockers(commands))

    status = _docker_smoke_status(blockers)
    payload = _docker_smoke_payload(
        status=status,
        evidence_root=evidence_root,
        repo_root=repo_root,
        evidence_run_id=resolved_run_id,
        image_tag=image_tag,
        dockerfile=dockerfile,
        commands=commands,
        blockers=blockers,
        preflight=preflight,
    )
    _write_json_atomic_replace(evidence_path, payload)
    return DockerSmokeResult(status=status, evidence_path=evidence_path, blockers=tuple(blockers))


def _run_display_startup_probe(
    runner: Callable[[Sequence[str]], CommandResult],
    *,
    image_tag: str,
) -> dict[str, CommandResult]:
    container_name = f"nhms-display-smoke-{uuid.uuid4().hex[:12]}"
    object_store_root = Path(tempfile.mkdtemp(prefix="nhms-display-object-store-smoke-")).resolve()
    commands: dict[str, CommandResult] = {}
    try:
        start_command = (
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container_name,
            "-e",
            "NHMS_REQUIRE_SERVICE_ROLE=true",
            "-e",
            "NHMS_SERVICE_ROLE=display_readonly",
            "-e",
            "NHMS_AUTH_MODE=production",
            "-e",
            "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true",
            "-e",
            "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false",
            "-e",
            f"OBJECT_STORE_ROOT={object_store_root}",
            "-v",
            f"{object_store_root}:{object_store_root}:ro",
            image_tag,
        )
        probe_command = (
            "docker",
            "exec",
            container_name,
            "uv",
            "run",
            "python",
            "-c",
            DISPLAY_LOCALHOST_PROBE_SCRIPT,
        )
        logs_command = ("docker", "logs", container_name)
        cleanup_command = ("docker", "rm", "-f", container_name)

        commands["display_startup_start"] = runner(start_command)
        if commands["display_startup_start"].returncode == 0:
            commands["display_startup_probe"] = runner(probe_command)
            commands["display_startup_logs"] = runner(logs_command)
        commands["display_startup_cleanup"] = runner(cleanup_command)
    finally:
        shutil.rmtree(object_store_root, ignore_errors=True)
    return commands


def _write_json_atomic_replace(path: Path, payload: Mapping[str, Any]) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            encoding="utf-8",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "BLOCKED", "blockers": [{"code": "ARTIFACT_MISSING", "path": str(path)}]}
    if isinstance(payload, dict):
        return payload
    return {"status": "BLOCKED", "blockers": [{"code": "ARTIFACT_JSON_NOT_OBJECT", "path": str(path)}]}


def _read_security_child_payload(
    label: str,
    artifact: Mapping[str, Any],
    *,
    approved_roots: Sequence[Path],
    evidence_run_id: str,
) -> dict[str, Any]:
    raw_path = artifact.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return _blocked_security_child(
            label,
            "DOCKER_SECURITY_SOURCE_PATH_MISSING",
            "Docker security source artifact path is missing.",
            path=None,
        )
    try:
        path = _approved_security_child_path(Path(raw_path), approved_roots=approved_roots)
        content = _read_security_child_bytes(path, approved_roots=approved_roots)
    except FileNotFoundError:
        return _blocked_security_child(
            label,
            "DOCKER_SECURITY_SOURCE_MISSING",
            "Docker security source artifact is missing.",
            path=raw_path,
        )
    except ValueError as error:
        return _blocked_security_child(
            label,
            str(error),
            "Docker security source artifact path is outside the approved evidence roots.",
            path=raw_path,
        )
    except OSError:
        return _blocked_security_child(
            label,
            "DOCKER_SECURITY_SOURCE_READ_FAILED",
            "Docker security source artifact could not be read safely.",
            path=raw_path,
        )
    except RuntimeError as error:
        return _blocked_security_child(
            label,
            str(error),
            "Docker security source artifact could not be read safely.",
            path=raw_path,
        )

    digest = hashlib.sha256(content).hexdigest()
    raw_sha256 = artifact.get("sha256")
    if isinstance(raw_sha256, str) and re.fullmatch(r"[a-fA-F0-9]{64}", raw_sha256.strip()):
        if digest != raw_sha256.lower():
            return _blocked_security_child(
                label,
                "DOCKER_SECURITY_SOURCE_HASH_MISMATCH",
                "Docker security source artifact sha256 does not match file content.",
                path=raw_path,
            )
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _blocked_security_child(
            label,
            "DOCKER_SECURITY_SOURCE_JSON_INVALID",
            "Docker security source artifact must be bounded valid JSON.",
            path=raw_path,
        )
    if not isinstance(payload, dict):
        return _blocked_security_child(
            label,
            "DOCKER_SECURITY_SOURCE_JSON_NOT_OBJECT",
            "Docker security source artifact JSON must be an object.",
            path=raw_path,
        )
    _bind_security_child_to_current_run(payload, label=label, path=raw_path, evidence_run_id=evidence_run_id)
    expected_schema = DOCKER_SECURITY_CHILD_SCHEMAS[label]
    observed_schema = payload.get("schema_version") or payload.get("schema")
    if observed_schema != expected_schema:
        blockers = list(payload.get("blockers", [])) if isinstance(payload.get("blockers"), list) else []
        blockers.append(
            {
                "code": "DOCKER_SECURITY_SOURCE_SCHEMA_INVALID",
                "source": label,
                "expected_schema": expected_schema,
                "schema": observed_schema,
                "path": str(path),
            }
        )
        payload = dict(payload)
        payload["status"] = "BLOCKED" if payload.get("status") != "FAIL" else "FAIL"
        payload["blockers"] = blockers
    _enforce_security_child_contract(payload, label=label)
    return payload


def _enforce_security_child_contract(payload: dict[str, Any], *, label: str) -> None:
    if payload.get("status") != "PASS":
        return
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    if label == "source_trust":
        blockers.extend(_source_trust_contract_blockers(payload))
    elif label == "static":
        child_blockers, child_findings = _static_contract_issues(payload)
        blockers.extend(child_blockers)
        findings.extend(child_findings)
    elif label == "smoke" and not _smoke_contract_passes(payload):
        blockers.append(
            {
                "code": "DOCKER_SECURITY_SMOKE_LIVE_COMMAND_EVIDENCE_MISSING",
                "source": label,
                "message": "Docker smoke PASS must include live command evidence.",
            }
        )
    if blockers:
        payload["status"] = "BLOCKED"
        payload["blockers"] = [*_child_blockers(payload), *blockers]
    if findings:
        payload["status"] = "FAIL"
        payload["findings"] = [*_child_findings(payload), *findings]


def _source_trust_contract_blockers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    checked_paths = payload.get("checked_paths")
    if not isinstance(checked_paths, list) or not checked_paths:
        return [
            {
                "code": "DOCKER_SECURITY_SOURCE_TRUST_CHECKED_PATHS_MISSING",
                "source": "source_trust",
                "message": "source_trust PASS must include non-empty checked_paths proof records.",
            }
        ]
    records = [record for record in checked_paths if isinstance(record, Mapping)]
    blockers: list[dict[str, Any]] = []
    labels = {str(record.get("label") or "") for record in records}
    required_labels = _required_source_trust_labels(payload)
    for label in sorted(required_labels - labels):
        blockers.append(
            {
                "code": "DOCKER_SECURITY_SOURCE_TRUST_REQUIRED_LABEL_MISSING",
                "source": "source_trust",
                "label": label,
                "message": "source_trust PASS is missing a required checked path label.",
            }
        )
    required_label_set = set(required_labels)
    for record in records:
        label = str(record.get("label") or "")
        if label not in required_label_set:
            continue
        blockers.extend(_source_trust_record_blockers(record))
    return blockers


def _required_source_trust_labels(payload: Mapping[str, Any]) -> set[str]:
    required = set(SOURCE_TRUST_COMMON_REQUIRED_LABELS)
    role_values = _source_trust_required_roles(payload)
    for role in role_values:
        label = SOURCE_TRUST_ROLE_LABELS.get(role)
        if label:
            required.add(label)
    return required


def _source_trust_required_roles(payload: Mapping[str, Any]) -> set[str]:
    default_roles = set(SOURCE_TRUST_ROLE_LABELS)
    roles = payload.get("roles")
    if not isinstance(roles, list):
        return default_roles
    role_values = {str(role).strip() for role in roles if str(role).strip()}
    if role_values and role_values <= set(SOURCE_TRUST_ROLE_LABELS):
        return role_values
    return default_roles


def _source_trust_record_blockers(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    label = str(record.get("label") or "")
    blockers: list[dict[str, Any]] = []
    expected_kind = str(record.get("expected_kind") or "")
    if label in {
        "checkout root",
        "infra directory",
        "env source directory",
        "systemd source directory",
        "trust path component",
    }:
        required_kind = "directory"
        kind_key = "is_directory"
    else:
        required_kind = "file"
        kind_key = "is_regular"
    checks = (
        ("exists", True, "DOCKER_SECURITY_SOURCE_TRUST_PATH_MISSING"),
        ("is_symlink", False, "DOCKER_SECURITY_SOURCE_TRUST_SYMLINK"),
        ("trusted_owner", True, "DOCKER_SECURITY_SOURCE_TRUST_OWNER_UNTRUSTED"),
        ("group_writable", False, "DOCKER_SECURITY_SOURCE_TRUST_GROUP_WRITABLE"),
        ("world_writable", False, "DOCKER_SECURITY_SOURCE_TRUST_WORLD_WRITABLE"),
        (kind_key, True, "DOCKER_SECURITY_SOURCE_TRUST_KIND_MISMATCH"),
    )
    if expected_kind != required_kind:
        blockers.append(_source_trust_record_blocker(record, "DOCKER_SECURITY_SOURCE_TRUST_KIND_MISMATCH"))
    for key, expected, code in checks:
        if record.get(key) is not expected:
            blockers.append(_source_trust_record_blocker(record, code, evidence_key=key, observed=record.get(key)))
    if label in set(SOURCE_TRUST_ROLE_LABELS.values()) and str(record.get("mode") or "") != "0600":
        blockers.append(
            _source_trust_record_blocker(
                record,
                "DOCKER_SECURITY_SOURCE_TRUST_ROLE_ENV_MODE_INVALID",
                evidence_key="mode",
                observed=record.get("mode"),
            )
        )
    return blockers


def _source_trust_record_blocker(
    record: Mapping[str, Any],
    code: str,
    *,
    evidence_key: str | None = None,
    observed: Any = None,
) -> dict[str, Any]:
    blocker: dict[str, Any] = {
        "code": code,
        "source": "source_trust",
        "label": record.get("label"),
        "path": record.get("path"),
        "message": "source_trust PASS contains an unsafe or incomplete checked path record.",
    }
    if evidence_key is not None:
        blocker["evidence_key"] = evidence_key
        blocker["observed"] = observed
    return blocker


def _source_trust_report_paths(value: Path | Sequence[Path]) -> list[Path]:
    if isinstance(value, Path):
        return [value]
    paths = list(value)
    if not paths:
        raise ValueError("at least one --source-trust-report is required")
    return paths


def _combine_source_trust_payloads(payloads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not payloads:
        return _blocked_security_child(
            "source_trust",
            "DOCKER_SECURITY_SOURCE_TRUST_MISSING",
            "Docker security summary requires source-trust evidence.",
            path=None,
        )
    combined: dict[str, Any] = {
        "schema": DOCKER_SECURITY_CHILD_SCHEMAS["source_trust"],
        "status": "PASS",
        "roles": [],
        "checked_paths": [],
        "blockers": [],
        "findings": [],
        "source_reports": [],
    }
    roles: list[str] = []
    checked_paths: list[Any] = []
    blockers: list[Any] = []
    findings: list[Any] = []
    statuses: set[Any] = set()
    evidence_run_id: Any = None
    for payload in payloads:
        statuses.add(payload.get("status"))
        if evidence_run_id is None:
            evidence_run_id = payload.get("evidence_run_id") or payload.get("bundle_run_id")
        raw_roles = payload.get("roles")
        if isinstance(raw_roles, list):
            for role in raw_roles:
                role_text = str(role).strip()
                if role_text and role_text not in roles:
                    roles.append(role_text)
        raw_checked = payload.get("checked_paths")
        if isinstance(raw_checked, list):
            checked_paths.extend(raw_checked)
        blockers.extend(_child_blockers(payload))
        findings.extend(_child_findings(payload))
        combined["source_reports"].append(
            {
                "status": payload.get("status"),
                "roles": list(raw_roles) if isinstance(raw_roles, list) else [],
            }
        )
    combined["evidence_run_id"] = evidence_run_id
    combined["roles"] = roles
    combined["checked_paths"] = checked_paths
    combined["blockers"] = blockers
    combined["findings"] = findings
    missing_roles = sorted(set(SOURCE_TRUST_ROLE_LABELS) - _source_trust_proven_roles(combined))
    for role in missing_roles:
        blockers.append(
            {
                "code": "DOCKER_SECURITY_SOURCE_TRUST_ROLE_ENV_PROOF_MISSING",
                "source": "source_trust",
                "role": role,
                "label": SOURCE_TRUST_ROLE_LABELS[role],
                "message": "source_trust PASS requires both compute and display role env proof.",
            }
        )
    if "FAIL" in statuses or findings:
        combined["status"] = "FAIL"
    elif statuses != {"PASS"} or blockers:
        combined["status"] = "BLOCKED"
    else:
        combined["status"] = "PASS"
    return combined


def _source_trust_proven_roles(payload: Mapping[str, Any]) -> set[str]:
    checked_paths = payload.get("checked_paths")
    if not isinstance(checked_paths, list):
        return set()
    roles: set[str] = set()
    for role, label in SOURCE_TRUST_ROLE_LABELS.items():
        for record in checked_paths:
            if not isinstance(record, Mapping) or str(record.get("label") or "") != label:
                continue
            if not _source_trust_record_blockers(record):
                roles.add(role)
                break
    return roles


def _static_contract_issues(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for proof_name in DOCKER_STATIC_REQUIRED_PROOFS:
        expected = proof_name in DOCKER_REQUIRED_TRUE_PROOFS
        observed = payload.get(proof_name)
        if observed is None:
            blockers.append(
                {
                    "code": "DOCKER_SECURITY_STATIC_PROOF_MISSING",
                    "source": "static",
                    "proof": proof_name,
                    "message": "static PASS must include final-compatible Docker proof fields.",
                }
            )
        elif observed is not expected:
            findings.append(
                {
                    "code": "DOCKER_SECURITY_STATIC_PROOF_CONTRADICTS_PASS",
                    "source": "static",
                    "proof": proof_name,
                    "observed": observed,
                    "expected": expected,
                    "message": "static PASS contradicts a required Docker proof field.",
                }
            )
    return blockers, findings


def _smoke_contract_passes(payload: Mapping[str, Any]) -> bool:
    commands = payload.get("commands")
    if not isinstance(commands, Mapping):
        return False
    required_success = ("image_absence_probe", "display_startup_start", "display_startup_probe")
    return all(
        isinstance(commands.get(name), Mapping) and commands[name].get("returncode") == 0
        for name in required_success
    )


def _bind_security_child_to_current_run(
    payload: dict[str, Any],
    *,
    label: str,
    path: str,
    evidence_run_id: str,
) -> None:
    raw_id = payload.get("evidence_run_id") or payload.get("bundle_run_id") or payload.get("evidence_bundle_id")
    if raw_id is not None and str(raw_id).strip() == evidence_run_id:
        return
    blockers = list(payload.get("blockers", [])) if isinstance(payload.get("blockers"), list) else []
    if raw_id is None or not str(raw_id).strip():
        blockers.append(
            {
                "code": "DOCKER_SECURITY_SOURCE_RUN_ID_MISSING",
                "source": label,
                "path": path,
                "expected_evidence_run_id": evidence_run_id,
            }
        )
    else:
        blockers.append(
            {
                "code": "DOCKER_SECURITY_SOURCE_RUN_ID_MISMATCH",
                "source": label,
                "path": path,
                "evidence_run_id": raw_id,
                "expected_evidence_run_id": evidence_run_id,
            }
        )
    payload["status"] = "BLOCKED" if payload.get("status") != "FAIL" else "FAIL"
    payload["blockers"] = blockers


def _blocked_security_child(label: str, code: str, message: str, *, path: str | None) -> dict[str, Any]:
    blocker: dict[str, Any] = {"code": code, "source": label, "message": message}
    if path is not None:
        blocker["path"] = path
    return {"status": "BLOCKED", "blockers": [blocker]}


def _artifact_summary(
    label: str,
    path: Path,
    *,
    repo_root: Path,
    approved_roots: Sequence[Path],
) -> dict[str, Any]:
    resolved = _resolve_child_path_no_follow(path, repo_root)
    summary: dict[str, Any] = {"path": str(resolved)}
    try:
        approved_path = _approved_security_child_path(resolved, approved_roots=approved_roots)
        content = _read_security_child_bytes(approved_path, approved_roots=approved_roots)
    except FileNotFoundError:
        summary["sha256"] = None
        summary["blocked"] = True
        summary["blocker_code"] = "DOCKER_SECURITY_SOURCE_MISSING"
    except ValueError as error:
        summary["sha256"] = None
        summary["blocked"] = True
        summary["blocker_code"] = str(error)
    except (OSError, RuntimeError):
        summary["sha256"] = None
        summary["blocked"] = True
        summary["blocker_code"] = "DOCKER_SECURITY_SOURCE_UNSAFE"
    else:
        summary["sha256"] = hashlib.sha256(content).hexdigest()
    summary["source"] = label
    return summary


def _docker_security_child_roots(repo_root: Path, summary_root: Path) -> tuple[Path, ...]:
    stage_change_root = (repo_root / "artifacts" / "stage-change" / CHANGE_ID).resolve(strict=False)
    roots = [summary_root.resolve(strict=False), stage_change_root]
    return tuple(dict.fromkeys(roots))


def _approved_security_child_path(path: Path, *, approved_roots: Sequence[Path]) -> Path:
    candidate = path.expanduser()
    _reject_symlink_components(candidate.parent)
    resolved = candidate.parent.resolve(strict=False) / candidate.name
    if not any(_is_relative_to(resolved, root.resolve(strict=False)) for root in approved_roots):
        raise ValueError("DOCKER_SECURITY_SOURCE_OUTSIDE_APPROVED_ROOT")
    _reject_symlink_components(resolved.parent)
    return resolved


def _read_security_child_bytes(path: Path, *, approved_roots: Sequence[Path]) -> bytes:
    root = _security_child_containment_root(path, approved_roots=approved_roots)
    relative = path.relative_to(root)
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = parent_fd
        for part in relative.parts[:-1]:
            if part in {"", ".", ".."}:
                raise RuntimeError("DOCKER_SECURITY_SOURCE_UNSAFE_PATH")
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            if fd != parent_fd:
                os.close(fd)
            fd = next_fd
        target = relative.name
        if target in {"", ".", ".."}:
            raise RuntimeError("DOCKER_SECURITY_SOURCE_UNSAFE_PATH")
        st = os.stat(target, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError("DOCKER_SECURITY_SOURCE_SYMLINK")
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError("DOCKER_SECURITY_SOURCE_NOT_FILE")
        if st.st_size > MAX_SECURITY_CHILD_BYTES:
            raise RuntimeError("DOCKER_SECURITY_SOURCE_TOO_LARGE")
        read_fd = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=fd,
        )
        try:
            opened = os.fstat(read_fd)
            if opened.st_dev != st.st_dev or opened.st_ino != st.st_ino:
                raise RuntimeError("DOCKER_SECURITY_SOURCE_CHANGED")
            content = os.read(read_fd, MAX_SECURITY_CHILD_BYTES + 1)
        finally:
            os.close(read_fd)
        if len(content) > MAX_SECURITY_CHILD_BYTES:
            raise RuntimeError("DOCKER_SECURITY_SOURCE_TOO_LARGE")
        return content
    finally:
        if "fd" in locals() and fd != parent_fd:
            os.close(fd)
        os.close(parent_fd)


def _security_child_containment_root(path: Path, *, approved_roots: Sequence[Path]) -> Path:
    resolved = path.parent.resolve(strict=False) / path.name
    for root in approved_roots:
        resolved_root = root.resolve(strict=False)
        if _is_relative_to(resolved, resolved_root):
            return resolved_root
    raise ValueError("DOCKER_SECURITY_SOURCE_OUTSIDE_APPROVED_ROOT")


def _reject_symlink_components(path: Path) -> None:
    current = path.expanduser()
    for component in [current, *current.parents]:
        if component.exists() and component.is_symlink():
            raise RuntimeError("DOCKER_SECURITY_SOURCE_SYMLINK")


def _resolve_child_path_no_follow(path: Path, repo_root: Path) -> Path:
    candidate = path if path.is_absolute() else repo_root / path
    candidate = candidate.expanduser()
    _reject_symlink_components(candidate.parent)
    return candidate.parent.resolve(strict=False) / candidate.name


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _docker_security_summary_status(
    source_trust_payload: Mapping[str, Any],
    static_payload: Mapping[str, Any],
    smoke_payload: Mapping[str, Any],
) -> str:
    payloads = (source_trust_payload, static_payload, smoke_payload)
    if any(_child_findings(payload) for payload in payloads):
        return "FAIL"
    if any(_child_blockers(payload) for payload in payloads):
        return "BLOCKED"
    statuses = {source_trust_payload.get("status"), static_payload.get("status"), smoke_payload.get("status")}
    if "FAIL" in statuses:
        return "FAIL"
    if statuses == {"PASS"}:
        return "PASS"
    return "BLOCKED"


def _docker_security_summary_blockers(
    source_trust_payload: Mapping[str, Any],
    static_payload: Mapping[str, Any],
    smoke_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for label, payload in (
        ("source_trust", source_trust_payload),
        ("static", static_payload),
        ("smoke", smoke_payload),
    ):
        child_blockers = _child_blockers(payload)
        child_findings = _child_findings(payload)
        if payload.get("status") == "PASS" and not child_blockers and not child_findings:
            continue
        blockers.append(
            {
                "code": "DOCKER_SECURITY_SOURCE_NOT_PASS",
                "source": label,
                "status": payload.get("status"),
                "source_blockers": child_blockers,
                "source_findings": child_findings,
            }
        )
    return blockers


def _child_blockers(payload: Mapping[str, Any]) -> list[Any]:
    blockers = payload.get("blockers")
    return list(blockers) if isinstance(blockers, list) else []


def _child_findings(payload: Mapping[str, Any]) -> list[Any]:
    findings = payload.get("findings")
    return list(findings) if isinstance(findings, list) else []


def _validate_env_file(path: Path, env: Mapping[str, str], *, role: str) -> list[Finding]:
    findings: list[Finding] = []
    required = COMPUTE_REQUIRED_ENV if role == "compute" else DISPLAY_REQUIRED_ENV
    for key, expected in required.items():
        actual = env.get(key)
        if actual is None:
            findings.append(
                Finding("ENV_REQUIRED_MISSING", f"{role} env must define {key}.", path=str(path), details={"key": key})
            )
            continue
        if actual.strip().lower() != expected:
            findings.append(
                Finding(
                    "ENV_REQUIRED_VALUE_INVALID",
                    f"{role} env {key} must be {expected}.",
                    path=str(path),
                    details={"key": key, "expected": expected, "actual": actual},
                )
            )
    for key in ("NHMS_PUBLISHED_ARTIFACT_ROOT", "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX"):
        if not env.get(key, "").strip():
            findings.append(
                Finding(
                    "ENV_REQUIRED_MISSING",
                    f"{role} env must define {key}.",
                    path=str(path),
                    details={"key": key},
                )
            )
    missing_canonical = sorted(key for key in CANONICAL_PUBLISHED_ENV if key not in env)
    if missing_canonical:
        findings.append(
            Finding(
                "CANONICAL_PUBLISHED_ENV_MISSING",
                f"{role} env is missing canonical published artifact variables.",
                path=str(path),
                details={"missing_keys": missing_canonical},
            )
        )
    if LEGACY_PUBLISHED_ENV in env:
        findings.append(
            Finding(
                "LEGACY_PUBLISHED_ARTIFACT_ENV",
                "Use NHMS_PUBLISHED_ARTIFACT_ROOT instead of PUBLISHED_ARTIFACT_ROOT.",
                path=str(path),
                details={"key": LEGACY_PUBLISHED_ENV},
            )
        )
    if role == "compute":
        for key in (
            "WORKSPACE_ROOT",
            "OBJECT_STORE_ROOT",
            "DATABASE_URL",
            "NHMS_PUBLISHED_ARTIFACT_HOST_ROOT",
            *sorted(COMPUTE_SCHEDULER_REQUIRED_ENV),
        ):
            if not env.get(key, "").strip():
                findings.append(
                    Finding(
                        "ENV_REQUIRED_MISSING",
                        f"compute env must define {key}.",
                        path=str(path),
                        details={"key": key},
                    )
                )
        findings.extend(_compute_object_store_copyback_overlap_findings(path, env))
    if role == "display":
        for key in ("DATABASE_URL", "NHMS_PUBLISHED_ARTIFACT_HOST_ROOT"):
            if not env.get(key, "").strip():
                findings.append(
                    Finding(
                        "ENV_REQUIRED_MISSING",
                        f"display env must define {key}.",
                        path=str(path),
                        details={"key": key},
                    )
                )
        for key in sorted(DISPLAY_FORBIDDEN_ENV_KEYS):
            if key in env:
                findings.append(
                    Finding(
                        "DISPLAY_FORBIDDEN_ENV",
                        f"display env must not configure {key}.",
                        path=str(path),
                        details={"key": key},
                    )
                )
    return findings


def _compute_object_store_copyback_overlap_findings(path: Path, env: Mapping[str, str]) -> list[Finding]:
    object_store_root = env.get("OBJECT_STORE_ROOT", "").strip()
    copyback_root = env.get("NHMS_OBJECT_STORE_COPYBACK_ROOT", "").strip()
    if not object_store_root or not copyback_root:
        return []
    normalized_object_store_root = _normalize_posix_path(object_store_root)
    normalized_copyback_root = _normalize_posix_path(copyback_root)
    if not normalized_object_store_root.startswith("/") or not normalized_copyback_root.startswith("/"):
        return []
    if normalized_object_store_root == normalized_copyback_root:
        return []
    if _posix_path_is_child(normalized_copyback_root, normalized_object_store_root):
        relationship = "copyback_root_under_object_store_root"
    elif _posix_path_is_child(normalized_object_store_root, normalized_copyback_root):
        relationship = "object_store_root_under_copyback_root"
    else:
        return []
    return [
        Finding(
            "COMPUTE_OBJECT_STORE_COPYBACK_ROOT_OVERLAP",
            "NHMS_OBJECT_STORE_COPYBACK_ROOT must not overlap OBJECT_STORE_ROOT except for exact equality.",
            path=str(path),
            details={
                "object_store_root": normalized_object_store_root,
                "copyback_root": normalized_copyback_root,
                "relationship": relationship,
            },
        )
    ]


def _validate_app_docker_assets(
    *,
    repo_root: Path,
    dockerfile: Path,
    entrypoint: Path,
    dockerignore: Path,
) -> list[Finding]:
    dockerfile_path = _resolve_path(dockerfile, repo_root)
    entrypoint_path = _resolve_path(entrypoint, repo_root)
    dockerignore_path = _resolve_path(dockerignore, repo_root)
    findings: list[Finding] = []

    if not dockerfile_path.is_file():
        findings.append(
            Finding(
                "APP_DOCKERFILE_MISSING",
                "default app image Dockerfile must exist.",
                path=str(dockerfile_path),
            )
        )
        return findings

    if not entrypoint_path.is_file():
        findings.append(
            Finding(
                "APP_ENTRYPOINT_MISSING",
                "role-aware app entrypoint must exist.",
                path=str(entrypoint_path),
            )
        )
    elif not os.access(entrypoint_path, os.X_OK):
        findings.append(
            Finding(
                "APP_ENTRYPOINT_NOT_EXECUTABLE",
                "role-aware app entrypoint must be executable.",
                path=str(entrypoint_path),
            )
        )

    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    if "infra/docker/entrypoint.sh" not in dockerfile_text or "ENTRYPOINT" not in dockerfile_text:
        findings.append(
            Finding(
                "APP_DOCKERFILE_ENTRYPOINT_MISSING",
                "Dockerfile.app must install infra/docker/entrypoint.sh as the image entrypoint.",
                path=str(dockerfile_path),
            )
        )
    if "apps/frontend/dist" not in dockerfile_text:
        findings.append(
            Finding(
                "APP_DOCKERFILE_FRONTEND_DIST_MISSING",
                "Dockerfile.app must build and copy frontend static assets to apps/frontend/dist.",
                path=str(dockerfile_path),
            )
        )
    for required_lock in ("uv.lock", "pyproject.toml", "pnpm-lock.yaml", "package.json"):
        if required_lock not in dockerfile_text:
            findings.append(
                Finding(
                    "APP_DOCKERFILE_LOCK_METADATA_MISSING",
                    "Dockerfile.app must use repository lock/project metadata.",
                    path=str(dockerfile_path),
                    details={"required": required_lock},
                )
            )
    if re.search(r"\b(?:slurm(?:-[A-Za-z0-9_.+-]+)?|munge)\b", dockerfile_text, flags=re.IGNORECASE):
        findings.append(
            Finding(
                "APP_DOCKERFILE_FORBIDDEN_SLURM_MUNGE_INSTALL",
                "default app image must not install Slurm client or Munge.",
                path=str(dockerfile_path),
            )
        )

    if entrypoint_path.is_file():
        findings.extend(_validate_app_entrypoint(entrypoint_path))

    if not dockerignore_path.is_file():
        findings.append(
            Finding(
                "APP_DOCKERIGNORE_MISSING",
                ".dockerignore must bound Docker build context and local artifacts for the default app image.",
                path=str(dockerignore_path),
            )
        )
    else:
        dockerignore_lines = _dockerignore_patterns(dockerignore_path)
        for required_pattern in sorted(REQUIRED_DOCKERIGNORE_PATTERNS):
            if required_pattern not in dockerignore_lines:
                findings.append(
                    Finding(
                        "APP_DOCKERIGNORE_PATTERN_MISSING",
                        ".dockerignore is missing a required context-bounding pattern.",
                        path=str(dockerignore_path),
                        details={"required": required_pattern},
                    )
                )
    return findings


def _validate_app_entrypoint(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    required_tokens = (
        "NHMS_REQUIRE_SERVICE_ROLE",
        "NHMS_SERVICE_ROLE",
        "display_readonly",
        "compute_control",
        "dev_monolith",
        "slurm_gateway",
        "SERVICE_ROLE_RESERVED",
        "DISPLAY_BOUNDARY_CONFIG_UNSAFE",
        "DISPLAY_COMMAND_FORBIDDEN",
        "uv run nhms-pipeline plan-production --plan",
        "apps.api.main:app",
    )
    for token in required_tokens:
        if token not in text:
            findings.append(
                Finding(
                    "APP_ENTRYPOINT_ROLE_CONTRACT_MISSING",
                    "entrypoint.sh is missing required role-aware startup contract text.",
                    path=str(path),
                    details={"required": token},
                )
            )
    for key in sorted(DISPLAY_FORBIDDEN_ENV_KEYS):
        if key == "SLURM_GATEWAY_BACKEND":
            expected = "SLURM_GATEWAY_BACKEND"
        else:
            expected = key
        if expected not in text:
            findings.append(
                Finding(
                    "APP_ENTRYPOINT_DISPLAY_FORBIDDEN_ENV_MISSING",
                    "entrypoint.sh must reject display startup with every display-forbidden env key.",
                    path=str(path),
                    details={"key": key},
                )
            )
    for binary in APP_IMAGE_FORBIDDEN_BINARIES:
        if binary not in text:
            findings.append(
                Finding(
                    "APP_ENTRYPOINT_DISPLAY_FORBIDDEN_COMMAND_MISSING",
                    "entrypoint.sh must reject display compute-control command overrides.",
                    path=str(path),
                    details={"command": binary},
                )
            )
    return findings


def _dockerignore_patterns(path: Path) -> set[str]:
    patterns: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            patterns.add(line.rstrip("/"))
    return patterns


def _validate_compute_compose(path: Path, compose: Mapping[str, Any], env: Mapping[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    services = _services(compose)
    if not services:
        return [Finding("COMPUTE_SERVICE_MISSING", "compute compose must define services.", path=str(path))]
    for service_name, service in services.items():
        service_env = _service_environment(service, env)
        required_runtime_env = (
            COMPUTE_SCHEDULER_ONCE_REQUIRED_RUNTIME_ENV
            if service_name == "scheduler-once"
            else COMPUTE_REQUIRED_RUNTIME_ENV
        )
        findings.extend(
            _runtime_env_findings(
                path=path,
                service=service_name,
                service_env=service_env,
                required_keys=required_runtime_env,
                expected_values=COMPUTE_REQUIRED_ENV,
                role="compute",
            )
        )
        if service_name == "scheduler-once":
            findings.extend(_compute_scheduler_once_db_free_findings(path, service_env))
        findings.extend(_compute_runtime_env_contract_findings(path, service_name, service, env))
        findings.extend(_compute_extra_hosts_findings(path, service_name, service))
        if service_env.get("NHMS_PUBLISHED_ARTIFACT_HOST_ROOT"):
            findings.append(
                Finding(
                    "COMPOSE_ONLY_ENV_LEAK",
                    "NHMS_PUBLISHED_ARTIFACT_HOST_ROOT must stay compose-only, not app runtime env.",
                    path=str(path),
                    service=service_name,
                )
            )
        if LEGACY_PUBLISHED_ENV in service_env:
            findings.append(
                Finding(
                    "LEGACY_PUBLISHED_ARTIFACT_ENV",
                    "compose service must not expose legacy PUBLISHED_ARTIFACT_ROOT.",
                    path=str(path),
                    service=service_name,
                )
            )
        if service_env.get("NHMS_SERVICE_ROLE") != "compute_control":
            findings.append(
                Finding(
                    "COMPUTE_SERVICE_ROLE_INVALID",
                    "compute services must run with NHMS_SERVICE_ROLE=compute_control.",
                    path=str(path),
                    service=service_name,
                    details={"actual": service_env.get("NHMS_SERVICE_ROLE")},
                )
            )
        findings.extend(_compute_port_findings(path, service_name, service))
        findings.extend(_compute_mount_findings(path, service_name, service, env))

    scheduler = services.get("scheduler-once")
    if scheduler is None:
        findings.append(
            Finding("COMPUTE_SCHEDULER_SERVICE_MISSING", "compute compose must define scheduler-once.", path=str(path))
        )
    elif _command_list(scheduler.get("command")) != ["uv", "run", "nhms-pipeline", "plan-production", "--plan"]:
        findings.append(
            Finding(
                "COMPUTE_SCHEDULER_COMMAND_INVALID",
                "scheduler-once must use uv run nhms-pipeline plan-production --plan.",
                path=str(path),
                service="scheduler-once",
                details={"actual": _command_list(scheduler.get("command"))},
            )
        )
    return findings


def _compute_scheduler_once_db_free_findings(path: Path, service_env: Mapping[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    db_free_required = service_env.get("NHMS_SCHEDULER_DB_FREE_REQUIRED", "").strip().lower()
    if db_free_required != "true":
        findings.append(
            Finding(
                "COMPUTE_SCHEDULER_DB_FREE_REQUIRED_INVALID",
                "scheduler-once must run with NHMS_SCHEDULER_DB_FREE_REQUIRED=true.",
                path=str(path),
                service="scheduler-once",
                details={
                    "key": "NHMS_SCHEDULER_DB_FREE_REQUIRED",
                    "expected": "true",
                    "actual": service_env.get("NHMS_SCHEDULER_DB_FREE_REQUIRED"),
                },
            )
        )
    if "DATABASE_URL" in service_env:
        findings.append(
            Finding(
                "COMPUTE_SCHEDULER_DATABASE_URL_FORBIDDEN",
                "DB-free scheduler-once runtime must not receive DATABASE_URL.",
                path=str(path),
                service="scheduler-once",
                details={"key": "DATABASE_URL"},
            )
        )
    journal_lock_guard_mode = service_env.get("NHMS_SCHEDULER_JOURNAL_LOCK_GUARD_MODE")
    if journal_lock_guard_mode is not None and journal_lock_guard_mode != "flock":
        findings.append(
            Finding(
                "COMPUTE_SCHEDULER_JOURNAL_LOCK_GUARD_MODE_INVALID",
                "DB-free scheduler-once journal lock guard mode must be exactly flock.",
                path=str(path),
                service="scheduler-once",
                details={
                    "key": "NHMS_SCHEDULER_JOURNAL_LOCK_GUARD_MODE",
                    "expected": "flock",
                    "actual": journal_lock_guard_mode,
                },
            )
        )
    for key in sorted(COMPUTE_DB_FREE_SCHEDULER_SELECTOR_ENV):
        actual = service_env.get(key)
        if actual is None:
            continue
        if actual.strip().lower() != "file":
            findings.append(
                Finding(
                    "COMPUTE_SCHEDULER_DB_FREE_SELECTOR_INVALID",
                    "DB-free scheduler-once selectors must be file.",
                    path=str(path),
                    service="scheduler-once",
                    details={"key": key, "expected": "file", "actual": actual},
                )
            )
    for key in sorted(COMPUTE_DB_FREE_SCHEDULER_PATH_ENV):
        if not service_env.get(key, "").strip():
            findings.append(
                Finding(
                    "COMPUTE_SCHEDULER_DB_FREE_PATH_MISSING",
                    "DB-free scheduler-once paths must be configured.",
                    path=str(path),
                    service="scheduler-once",
                    details={"key": key},
                )
            )
    return findings


def _compute_extra_hosts_findings(path: Path, service_name: str, service: Mapping[str, Any]) -> list[Finding]:
    extra_hosts = _compose_entry_list(service.get("extra_hosts"))
    if COMPUTE_REQUIRED_EXTRA_HOST in {str(item) for item in extra_hosts}:
        return []
    return [
        Finding(
            "COMPUTE_HOST_GATEWAY_MISSING",
            "compute services must map host.docker.internal to host-gateway for Linux Docker host service access.",
            path=str(path),
            service=service_name,
            details={"required": COMPUTE_REQUIRED_EXTRA_HOST},
        )
    ]


def _compute_mount_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
    env: Mapping[str, str],
) -> list[Finding]:
    volumes = [_volume_info(volume, env) for volume in service.get("volumes", []) or []]
    findings: list[Finding] = []
    findings.extend(
        _require_mount(
            volumes,
            path=path,
            service=service_name,
            env=env,
            source_key="WORKSPACE_ROOT",
            target_key="WORKSPACE_ROOT",
            read_only=False,
            missing_code="COMPUTE_WORKSPACE_MOUNT_MISSING",
            readonly_code="COMPUTE_WORKSPACE_MOUNT_READONLY",
            type_code="COMPUTE_WORKSPACE_MOUNT_TYPE_INVALID",
            identity_code="COMPUTE_WORKSPACE_MOUNT_IDENTITY_INVALID",
        )
    )
    findings.extend(
        _require_mount(
            volumes,
            path=path,
            service=service_name,
            env=env,
            source_key="OBJECT_STORE_ROOT",
            target_key="OBJECT_STORE_ROOT",
            read_only=False,
            missing_code="COMPUTE_OBJECT_STORE_MOUNT_MISSING",
            readonly_code="COMPUTE_OBJECT_STORE_MOUNT_READONLY",
            type_code="COMPUTE_OBJECT_STORE_MOUNT_TYPE_INVALID",
            identity_code="COMPUTE_OBJECT_STORE_MOUNT_IDENTITY_INVALID",
        )
    )
    findings.extend(
        _require_mount(
            volumes,
            path=path,
            service=service_name,
            env=env,
            source_key="NHMS_OBJECT_STORE_COPYBACK_ROOT",
            target_key="NHMS_OBJECT_STORE_COPYBACK_ROOT",
            read_only=False,
            missing_code="COMPUTE_OBJECT_STORE_COPYBACK_MOUNT_MISSING",
            readonly_code="COMPUTE_OBJECT_STORE_COPYBACK_MOUNT_READONLY",
            type_code="COMPUTE_OBJECT_STORE_COPYBACK_MOUNT_TYPE_INVALID",
            identity_code="COMPUTE_OBJECT_STORE_COPYBACK_MOUNT_IDENTITY_INVALID",
        )
    )
    findings.extend(
        _require_mount(
            volumes,
            path=path,
            service=service_name,
            env=env,
            source_key="NHMS_SCHEDULER_RUNTIME_ROOT",
            target_key="NHMS_SCHEDULER_RUNTIME_ROOT",
            read_only=False,
            missing_code="COMPUTE_SCHEDULER_RUNTIME_MOUNT_MISSING",
            readonly_code="COMPUTE_SCHEDULER_RUNTIME_MOUNT_READONLY",
            type_code="COMPUTE_SCHEDULER_RUNTIME_MOUNT_TYPE_INVALID",
            identity_code="COMPUTE_SCHEDULER_RUNTIME_MOUNT_IDENTITY_INVALID",
        )
    )
    findings.extend(
        _require_mount(
            volumes,
            path=path,
            service=service_name,
            env=env,
            source_key="NHMS_SCHEDULER_TEMP_ROOT",
            target_key="NHMS_SCHEDULER_TEMP_ROOT",
            read_only=False,
            missing_code="COMPUTE_SCHEDULER_TEMP_MOUNT_MISSING",
            readonly_code="COMPUTE_SCHEDULER_TEMP_MOUNT_READONLY",
            type_code="COMPUTE_SCHEDULER_TEMP_MOUNT_TYPE_INVALID",
            identity_code="COMPUTE_SCHEDULER_TEMP_MOUNT_IDENTITY_INVALID",
        )
    )
    findings.extend(
        _require_mount(
            volumes,
            path=path,
            service=service_name,
            env=env,
            source_key="NHMS_PUBLISHED_ARTIFACT_HOST_ROOT",
            target_key="NHMS_PUBLISHED_ARTIFACT_ROOT",
            read_only=False,
            missing_code="COMPUTE_PUBLISHED_MOUNT_MISSING",
            readonly_code="COMPUTE_PUBLISHED_MOUNT_READONLY",
            type_code="COMPUTE_PUBLISHED_MOUNT_TYPE_INVALID",
            identity_code="COMPUTE_PUBLISHED_MOUNT_IDENTITY_INVALID",
        )
    )
    findings.extend(
        _require_mount(
            volumes,
            path=path,
            service=service_name,
            env=env,
            source_key="NHMS_BASINS_ROOT",
            target_key="NHMS_BASINS_ROOT",
            read_only=True,
            missing_code="COMPUTE_BASINS_MOUNT_MISSING",
            readonly_code="COMPUTE_BASINS_MOUNT_WRITABLE",
            type_code="COMPUTE_BASINS_MOUNT_TYPE_INVALID",
            identity_code="COMPUTE_BASINS_MOUNT_IDENTITY_INVALID",
        )
    )
    findings.extend(
        _require_mount(
            volumes,
            path=path,
            service=service_name,
            env=env,
            source_key="NHMS_MODEL_ASSET_ROOT",
            target_key="NHMS_MODEL_ASSET_ROOT",
            read_only=True,
            missing_code="COMPUTE_MODEL_ASSET_MOUNT_MISSING",
            readonly_code="COMPUTE_MODEL_ASSET_MOUNT_WRITABLE",
            type_code="COMPUTE_MODEL_ASSET_MOUNT_TYPE_INVALID",
            identity_code="COMPUTE_MODEL_ASSET_MOUNT_IDENTITY_INVALID",
        )
    )
    return findings


def _validate_display_compose(
    path: Path,
    compose: Mapping[str, Any],
    env: Mapping[str, str],
    compute_env: Mapping[str, str],
) -> list[Finding]:
    findings: list[Finding] = []
    if _has_compose_value(compose, "include"):
        findings.append(
            Finding(
                "DISPLAY_INCLUDE_UNSUPPORTED",
                "display compose must not use top-level include; included fragments are not statically audited.",
                path=str(path),
                details={"include": compose.get("include")},
            )
        )
    services = _services(compose)
    if not services:
        findings.append(Finding("DISPLAY_SERVICE_MISSING", "display compose must define services.", path=str(path)))
        return findings
    named_volumes = _named_volumes(compose)
    compute_roots = _compute_only_roots(compute_env)
    for service_name, service in services.items():
        service_env = _service_environment(service, env)
        findings.extend(
            _runtime_env_findings(
                path=path,
                service=service_name,
                service_env=service_env,
                required_keys=DISPLAY_REQUIRED_RUNTIME_ENV,
                expected_values=DISPLAY_REQUIRED_ENV,
                role="display",
            )
        )
        if _has_compose_value(service, "env_file"):
            findings.append(
                Finding(
                    "DISPLAY_ENV_FILE_UNSUPPORTED",
                    "display service env must stay inline and statically auditable; env_file is not allowed.",
                    path=str(path),
                    service=service_name,
                    details={"env_file": service.get("env_file")},
                )
            )
        if _has_compose_value(service, "volumes_from"):
            findings.append(
                Finding(
                    "DISPLAY_VOLUMES_FROM_UNSUPPORTED",
                    "display service must not inherit mounts through volumes_from.",
                    path=str(path),
                    service=service_name,
                    details={"volumes_from": service.get("volumes_from")},
                )
            )
        if _has_compose_value(service, "extends"):
            findings.append(
                Finding(
                    "DISPLAY_EXTENDS_UNSUPPORTED",
                    "display service must not inherit configuration through extends.",
                    path=str(path),
                    service=service_name,
                    details={"extends": service.get("extends")},
                )
            )
        findings.extend(_display_deploy_findings(path, service_name, service))
        findings.extend(_display_file_ingress_findings(path, service_name, service, compose))
        findings.extend(_display_device_ingress_findings(path, service_name, service))
        findings.extend(_display_runtime_env_contract_findings(path, service_name, service, env))
        findings.extend(_display_environment_list_findings(path, service_name, service))
        if service_env.get("NHMS_PUBLISHED_ARTIFACT_HOST_ROOT"):
            findings.append(
                Finding(
                    "COMPOSE_ONLY_ENV_LEAK",
                    "NHMS_PUBLISHED_ARTIFACT_HOST_ROOT must stay compose-only, not app runtime env.",
                    path=str(path),
                    service=service_name,
                )
            )
        if LEGACY_PUBLISHED_ENV in service_env:
            findings.append(
                Finding(
                    "LEGACY_PUBLISHED_ARTIFACT_ENV",
                    "display service must not expose legacy PUBLISHED_ARTIFACT_ROOT.",
                    path=str(path),
                    service=service_name,
                )
            )
        for key in sorted(DISPLAY_FORBIDDEN_ENV_KEYS):
            if key in service_env:
                findings.append(
                    Finding(
                        "DISPLAY_FORBIDDEN_ENV",
                        f"display service must not configure {key}.",
                        path=str(path),
                        service=service_name,
                        details={"key": key},
                    )
                )
        findings.extend(_display_hostconfig_findings(path, service_name, service, env))
        findings.extend(_display_tmpfs_findings(path, service_name, service, env))
        findings.extend(_display_mount_findings(path, service_name, service, env, compute_roots, named_volumes))
    return findings


def _display_hostconfig_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
    env: Mapping[str, str],
) -> list[Finding]:
    findings: list[Finding] = []
    for field_name in DISPLAY_DYNAMIC_HOSTCONFIG_FIELDS:
        value = service.get(field_name)
        if _has_compose_value(service, field_name) and _compose_contains_interpolation(value):
            findings.append(
                Finding(
                    "DISPLAY_HOSTCONFIG_DYNAMIC_INTERPOLATION",
                    "display HostConfig capability fields must use literal audited values, not Compose interpolation.",
                    path=str(path),
                    service=service_name,
                    details={
                        "field": field_name,
                        "variables": sorted(_compose_interpolation_keys(value)),
                    },
                )
            )
    privileged = _compose_bool(_resolve_compose_object(service.get("privileged"), env))
    if privileged is True:
        findings.append(
            Finding(
                "DISPLAY_HOSTCONFIG_PRIVILEGED",
                "display service must not be privileged.",
                path=str(path),
                service=service_name,
            )
        )
    network_mode = _resolved_compose_text(service.get("network_mode"), env).lower()
    if network_mode == "host":
        findings.append(
            Finding(
                "DISPLAY_HOSTCONFIG_HOST_NETWORK",
                "display service must not use host network.",
                path=str(path),
                service=service_name,
            )
        )
    if _is_namespace_sharing_mode(network_mode):
        findings.append(
            Finding(
                "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED",
                "display service must not share another service or container network namespace.",
                path=str(path),
                service=service_name,
                details={"field": "network_mode", "mode": network_mode},
            )
        )
    pid_mode = _resolved_compose_text(service.get("pid"), env).lower()
    if pid_mode == "host":
        findings.append(
            Finding(
                "DISPLAY_HOSTCONFIG_HOST_PID",
                "display service must not use host PID.",
                path=str(path),
                service=service_name,
            )
        )
    if _is_namespace_sharing_mode(pid_mode):
        findings.append(
            Finding(
                "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED",
                "display service must not share another service or container PID namespace.",
                path=str(path),
                service=service_name,
                details={"field": "pid", "mode": pid_mode},
            )
        )
    ipc_mode = _resolved_compose_text(service.get("ipc"), env).lower()
    if ipc_mode == "host":
        findings.append(
            Finding(
                "DISPLAY_HOSTCONFIG_HOST_IPC",
                "display service must not use host IPC.",
                path=str(path),
                service=service_name,
            )
        )
    if _is_namespace_sharing_mode(ipc_mode):
        findings.append(
            Finding(
                "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED",
                "display service must not share another service or container IPC namespace.",
                path=str(path),
                service=service_name,
                details={"field": "ipc", "mode": ipc_mode},
            )
        )
    if _nonempty_compose_sequence(service.get("cap_add"), env):
        findings.append(
            Finding(
                "DISPLAY_HOSTCONFIG_CAP_ADD",
                "display service must not add Linux capabilities.",
                path=str(path),
                service=service_name,
            )
        )
    if not _cap_drop_all_literal(service.get("cap_drop")):
        findings.append(
            Finding(
                "DISPLAY_HOSTCONFIG_CAP_DROP_INVALID",
                "display service must literally drop all Linux capabilities with cap_drop: [ALL].",
                path=str(path),
                service=service_name,
                details={"actual": service.get("cap_drop")},
            )
        )
    if not _security_opt_no_new_privileges_literal(service.get("security_opt")):
        findings.append(
            Finding(
                "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID",
                "display service must literally set security_opt: [no-new-privileges:true].",
                path=str(path),
                service=service_name,
                details={"actual": service.get("security_opt")},
            )
        )
    read_only = _compose_bool(_resolve_compose_object(service.get("read_only"), env))
    if read_only is not True:
        findings.append(
            Finding(
                "DISPLAY_ROOT_FILESYSTEM_WRITABLE",
                "display service must use a readonly root filesystem where feasible.",
                path=str(path),
                service=service_name,
            )
        )
    return findings


def _display_file_ingress_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
    compose: Mapping[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    for field_name, code, singular in (
        ("configs", "DISPLAY_CONFIG_UNSUPPORTED", "config"),
        ("secrets", "DISPLAY_SECRET_UNSUPPORTED", "secret"),
    ):
        if not _has_compose_value(service, field_name):
            continue
        top_level = compose.get(field_name, {})
        top_level_map = top_level if isinstance(top_level, Mapping) else {}
        for entry in _compose_entry_list(service.get(field_name)):
            source, target = _file_ingress_entry_source_target(entry, singular)
            definition = top_level_map.get(source, {}) if source else {}
            findings.append(
                Finding(
                    code,
                    f"display service must not mount Compose {field_name}.",
                    path=str(path),
                    service=service_name,
                    details={
                        "field": field_name,
                        "entry": entry,
                        "source": source,
                        "target": target,
                        "top_level": _file_ingress_definition_details(definition),
                    },
                )
            )
    return findings


def _display_deploy_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
) -> list[Finding]:
    if not _has_compose_value(service, "deploy"):
        return []
    return [
        Finding(
            "DISPLAY_DEPLOY_UNSUPPORTED",
            "display service must not configure deploy; #236 has no approved display deploy/device surface.",
            path=str(path),
            service=service_name,
            details={
                "field": "deploy",
                "value": service.get("deploy"),
                "variables": sorted(_compose_interpolation_keys(service.get("deploy"))),
            },
        )
    ]


def _display_device_ingress_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    for field_name, code in (
        ("devices", "DISPLAY_HOST_DEVICE_UNSUPPORTED"),
        ("device_cgroup_rules", "DISPLAY_DEVICE_CGROUP_RULE_UNSUPPORTED"),
        ("device_requests", "DISPLAY_DEVICE_REQUEST_UNSUPPORTED"),
    ):
        if not _has_compose_value(service, field_name):
            continue
        value = service.get(field_name)
        findings.append(
            Finding(
                code,
                f"display service must not configure {field_name}.",
                path=str(path),
                service=service_name,
                details={
                    "field": field_name,
                    "value": value,
                    "variables": sorted(_compose_interpolation_keys(value)),
                },
            )
        )
    return findings


def _compute_runtime_env_contract_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
    env: Mapping[str, str],
) -> list[Finding]:
    environment = service.get("environment", {})
    findings: list[Finding] = []
    if isinstance(environment, Mapping):
        for raw_key, raw_value in environment.items():
            key = str(raw_key)
            findings.extend(
                _compute_audited_env_value_findings(
                    path=path,
                    service_name=service_name,
                    key=key,
                    value=raw_value,
                    entry=key,
                    env=env,
                )
            )
        return findings
    if isinstance(environment, list):
        for item in environment:
            text = str(item)
            key, value = _split_environment_list_entry(text)
            findings.extend(
                _compute_audited_env_value_findings(
                    path=path,
                    service_name=service_name,
                    key=key,
                    value=value,
                    entry=text,
                    env=env,
                )
            )
            if _compose_contains_interpolation(text):
                for candidate_key, candidate_value in _environment_entry_candidates(text):
                    findings.extend(
                        _compute_audited_env_value_findings(
                            path=path,
                            service_name=service_name,
                            key=_resolve_compose_value(candidate_key, env),
                            value=candidate_value,
                            entry=text,
                            env=env,
                        )
                    )
    return findings


def _compute_audited_env_value_findings(
    *,
    path: Path,
    service_name: str,
    key: str,
    value: Any,
    entry: str,
    env: Mapping[str, str],
) -> list[Finding]:
    if key not in COMPUTE_AUDITED_RUNTIME_ENV:
        return []
    if value is None:
        return [
            Finding(
                "COMPUTE_RUNTIME_ENV_NULL_IMPORT",
                "compute audited runtime env keys must not import values from the ambient process environment.",
                path=str(path),
                service=service_name,
                details={"key": key, "entry": entry},
            )
        ]
    variables = _compose_interpolation_keys(value)
    if variables and _compose_interpolation_matches_env_file_value(value, key, env):
        return []
    if variables and variables != {key}:
        return [
            Finding(
                "COMPUTE_RUNTIME_ENV_ALIAS_INTERPOLATION",
                "compute audited runtime env interpolation must use the same audited env key.",
                path=str(path),
                service=service_name,
                details={"key": key, "entry": entry, "variables": sorted(variables)},
            )
        ]
    if key in env:
        actual_value = _resolved_compose_text(value, env if variables else {})
        if actual_value != env[key]:
            details = {
                "key": key,
                "entry": entry,
                "env_file_value": _redact_env_value(key, env[key]),
                "literal_value": _redact_env_value(key, actual_value),
            }
            if variables:
                details["rendered_value"] = _redact_env_value(key, actual_value)
            return [
                Finding(
                    "COMPUTE_RUNTIME_ENV_LITERAL_DRIFT",
                    "compute audited runtime env value must match the audited compute env file value.",
                    path=str(path),
                    service=service_name,
                    details=details,
                )
            ]
    return []


def _display_runtime_env_contract_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
    env: Mapping[str, str],
) -> list[Finding]:
    environment = service.get("environment", {})
    findings: list[Finding] = []
    if isinstance(environment, Mapping):
        for raw_key, raw_value in environment.items():
            key = str(raw_key)
            if _compose_contains_interpolation(key):
                findings.append(
                    Finding(
                        "DISPLAY_ENV_DYNAMIC_KEY",
                        "display environment mapping keys must not use Compose interpolation.",
                        path=str(path),
                        service=service_name,
                        details={"key": key, "variables": sorted(_compose_interpolation_keys(key))},
                    )
                )
            findings.extend(
                _display_critical_env_value_findings(
                    path=path,
                    service_name=service_name,
                    key=key,
                    value=raw_value,
                    entry=key,
                    env=env,
                )
            )
        return findings
    if isinstance(environment, list):
        for item in environment:
            text = str(item)
            key, value = _split_environment_list_entry(text)
            findings.extend(
                _display_critical_env_value_findings(
                    path=path,
                    service_name=service_name,
                    key=key,
                    value=value,
                    entry=text,
                    env=env,
                )
            )
            if _compose_contains_interpolation(text):
                for candidate_key, candidate_value in _environment_entry_candidates(text):
                    findings.extend(
                        _display_critical_env_value_findings(
                            path=path,
                            service_name=service_name,
                            key=_resolve_compose_value(candidate_key, env),
                            value=candidate_value,
                            entry=text,
                            env=env,
                        )
                    )
    return findings


def _display_critical_env_value_findings(
    *,
    path: Path,
    service_name: str,
    key: str,
    value: Any,
    entry: str,
    env: Mapping[str, str],
) -> list[Finding]:
    if key not in DISPLAY_AUDITED_RUNTIME_ENV:
        return []
    if value is None:
        return [
            Finding(
                "DISPLAY_RUNTIME_ENV_NULL_IMPORT",
                "display audited runtime env keys must not import values from the ambient process environment.",
                path=str(path),
                service=service_name,
                details={"key": key, "entry": entry},
            )
        ]
    variables = _compose_interpolation_keys(value)
    if variables and _compose_interpolation_matches_env_file_value(value, key, env):
        return []
    if variables and variables != {key}:
        return [
            Finding(
                "DISPLAY_RUNTIME_ENV_ALIAS_INTERPOLATION",
                "display audited runtime env interpolation must use the same canonical env key.",
                path=str(path),
                service=service_name,
                details={"key": key, "entry": entry, "variables": sorted(variables)},
            )
        ]
    if key in env:
        actual_value = _resolved_compose_text(value, env if variables else {})
        if actual_value != env[key]:
            details = {
                "key": key,
                "entry": entry,
                "env_file_value": _redact_env_value(key, env[key]),
                "literal_value": _redact_env_value(key, actual_value),
            }
            if variables:
                details["rendered_value"] = _redact_env_value(key, actual_value)
            return [
                Finding(
                    "DISPLAY_RUNTIME_ENV_LITERAL_DRIFT",
                    "display audited runtime env value must match the audited display env file value.",
                    path=str(path),
                    service=service_name,
                    details=details,
                )
            ]
    return []


def _display_tmpfs_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
    env: Mapping[str, str],
) -> list[Finding]:
    findings: list[Finding] = []
    for tmpfs in _tmpfs_infos(service.get("tmpfs", []) or [], env):
        raw_target = str(tmpfs["raw_target"])
        target = str(tmpfs["target"])
        if _compose_contains_interpolation(raw_target):
            findings.append(
                Finding(
                    "DISPLAY_TMPFS_DYNAMIC_INTERPOLATION",
                    "display tmpfs targets must use literal audited paths.",
                    path=str(path),
                    service=service_name,
                    details={"tmpfs": tmpfs["text"], "variables": sorted(_compose_interpolation_keys(raw_target))},
                )
            )
        if _targets_artifact_root_or_child(target, env):
            findings.append(
                Finding(
                    "DISPLAY_TMPFS_ARTIFACT_OVERLAY",
                    "display tmpfs entries must not overlay the published artifact root.",
                    path=str(path),
                    service=service_name,
                    details={"tmpfs": tmpfs["text"], "target": target},
                )
            )
        if _normalize_posix_path(target) not in DISPLAY_ALLOWED_TMPFS_TARGETS:
            findings.append(
                Finding(
                    "DISPLAY_UNAPPROVED_TMPFS",
                    "display tmpfs entries are limited to /tmp and /run.",
                    path=str(path),
                    service=service_name,
                    details={"tmpfs": tmpfs["text"], "target": target},
                )
            )
    return findings


def _display_mount_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
    env: Mapping[str, str],
    compute_roots: Mapping[str, str],
    named_volumes: Mapping[str, Mapping[str, Any]],
) -> list[Finding]:
    findings: list[Finding] = []
    volumes = [_volume_info(volume, env) for volume in service.get("volumes", []) or []]
    approved_artifact_volumes: list[Mapping[str, Any]] = []
    findings.extend(
        _require_mount(
            volumes,
            path=path,
            service=service_name,
            env=env,
            source_key=DISPLAY_OBJECT_STORE_MOUNT_SOURCE_VAR,
            target_key=DISPLAY_OBJECT_STORE_MOUNT_TARGET_VAR,
            read_only=True,
            missing_code="DISPLAY_OBJECT_STORE_MOUNT_MISSING",
            readonly_code="DISPLAY_OBJECT_STORE_MOUNT_NOT_READONLY",
            type_code="DISPLAY_OBJECT_STORE_MOUNT_TYPE_INVALID",
            identity_code="DISPLAY_OBJECT_STORE_MOUNT_IDENTITY_INVALID",
        )
    )
    for volume in volumes:
        findings.extend(_display_mount_dynamic_interpolation_findings(path, service_name, volume, env))
        findings.extend(_display_mount_relative_path_findings(path, service_name, volume))
        is_approved_published = _is_approved_display_published_mount(volume, env)
        is_approved_object_store = _is_approved_display_object_store_mount(volume, env)
        if is_approved_published:
            approved_artifact_volumes.append(volume)
        if not is_approved_published and not is_approved_object_store:
            findings.append(
                Finding(
                    "DISPLAY_UNAPPROVED_MOUNT",
                    "display service may only mount exact readonly published artifact and object-store binds.",
                    path=str(path),
                    service=service_name,
                    details={"volume": volume["text"]},
                )
            )
        if _targets_artifact_root_or_child(volume["target"], env) and not _is_approved_display_published_mount(
            volume, env
        ):
            findings.append(
                Finding(
                    "DISPLAY_ARTIFACT_OVERLAY_MOUNT",
                    "display service must not overlay or shadow the published artifact root.",
                    path=str(path),
                    service=service_name,
                    details={"volume": volume["text"], "target": volume["target"]},
                )
            )
        if not volume["read_only"] and not is_approved_published and not is_approved_object_store:
            findings.append(
                Finding(
                    "DISPLAY_UNAPPROVED_WRITABLE_MOUNT",
                    "display service must not define writable non-published mounts.",
                    path=str(path),
                    service=service_name,
                    details={"volume": volume["text"], "target": volume["target"]},
                )
            )
    if not approved_artifact_volumes:
        findings.append(
            Finding(
                "DISPLAY_PUBLISHED_MOUNT_MISSING",
                "display service must mount published artifacts read-only.",
                path=str(path),
                service=service_name,
            )
        )
    if len(approved_artifact_volumes) > 1:
        findings.append(
            Finding(
                "DISPLAY_PUBLISHED_MOUNT_DUPLICATE",
                "display service must have exactly one readonly published artifact bind.",
                path=str(path),
                service=service_name,
                details={"count": len(approved_artifact_volumes)},
            )
        )
    for volume in volumes:
        if not _is_published_mount_candidate(volume, env):
            continue
        expected_source = env.get("NHMS_PUBLISHED_ARTIFACT_HOST_ROOT", "")
        expected_target = env.get("NHMS_PUBLISHED_ARTIFACT_ROOT", "")
        if not _raw_mount_type_is_literal_bind(volume):
            findings.append(
                Finding(
                    "DISPLAY_PUBLISHED_MOUNT_TYPE_INVALID",
                    "display published artifact mount must be a canonical bind mount.",
                    path=str(path),
                    service=service_name,
                    details={"actual_type": volume["type"] or "short-form-or-implicit"},
                )
            )
        identity_fields = _required_mount_identity_fields(
            volume,
            source_key=PUBLISHED_ARTIFACT_MOUNT_SOURCE_VAR,
            target_key=PUBLISHED_ARTIFACT_MOUNT_TARGET_VAR,
        )
        if identity_fields:
            findings.append(
                Finding(
                    "DISPLAY_PUBLISHED_MOUNT_IDENTITY_INVALID",
                    "display published artifact mount must use canonical Compose env-variable identity.",
                    path=str(path),
                    service=service_name,
                    details={
                        "fields": identity_fields,
                        "source_key": PUBLISHED_ARTIFACT_MOUNT_SOURCE_VAR,
                        "target_key": PUBLISHED_ARTIFACT_MOUNT_TARGET_VAR,
                        "raw_source": volume.get("raw_source", ""),
                        "raw_target": volume.get("raw_target", ""),
                    },
                )
            )
        if volume["source"] != expected_source:
            findings.append(
                Finding(
                    "DISPLAY_PUBLISHED_SOURCE_DRIFT",
                    "display published artifact source must use NHMS_PUBLISHED_ARTIFACT_HOST_ROOT.",
                    path=str(path),
                    service=service_name,
                    details={"actual": volume["source"], "expected": expected_source},
                )
            )
        if volume["target"] != expected_target:
            findings.append(
                Finding(
                    "DISPLAY_PUBLISHED_TARGET_DRIFT",
                    "display published artifact target must equal NHMS_PUBLISHED_ARTIFACT_ROOT.",
                    path=str(path),
                    service=service_name,
                    details={"actual": volume["target"], "expected": expected_target},
                )
            )
        if not volume["read_only"]:
            findings.append(
                Finding(
                    "DISPLAY_PUBLISHED_MOUNT_NOT_READONLY",
                    "display published artifact mount must be read-only.",
                    path=str(path),
                    service=service_name,
                )
            )

    for volume in volumes:
        text = volume["text"]
        mount_text = "\n".join((text, volume["source"], volume["target"]))
        if any(token in mount_text for token in FORBIDDEN_MOUNT_TOKENS) or any(
            _is_docker_socket_path(part) for part in (volume["source"], volume["target"])
        ) or any(
            _is_munge_path(part) for part in (volume["source"], volume["target"])
        ):
            findings.append(
                Finding(
                    "DISPLAY_FORBIDDEN_MOUNT",
                    "display service contains a forbidden compute-control mount.",
                    path=str(path),
                    service=service_name,
                    details={"volume": text},
                )
            )
        for side in ("source", "target"):
            mount_path = volume[side]
            if _is_broad_host_path(mount_path):
                findings.append(
                    Finding(
                        "DISPLAY_BROAD_HOST_ROOT_BIND",
                        "display service must not bind broad host roots or private scratch paths.",
                        path=str(path),
                        service=service_name,
                        details={"side": side, "path": mount_path},
                    )
                )
            matched_root = _matching_compute_root(mount_path, compute_roots)
            if matched_root:
                root_key, root_value = matched_root
                findings.append(
                    Finding(
                        "DISPLAY_FORBIDDEN_MOUNT",
                        "display service must not mount compute-only roots.",
                        path=str(path),
                        service=service_name,
                        details={
                            "volume": text,
                            "side": side,
                            "root_key": root_key,
                            "root": root_value,
                            "path": mount_path,
                        },
                    )
                )
        source = volume["source"]
        if source in named_volumes:
            findings.extend(
                _display_named_volume_findings(
                    path=path,
                    service_name=service_name,
                    volume_name=source,
                    volume=volume,
                    volume_definition=named_volumes[source],
                    env=env,
                    compute_roots=compute_roots,
                )
            )
    return findings


def _display_mount_dynamic_interpolation_findings(
    path: Path,
    service_name: str,
    volume: Mapping[str, Any],
    env: Mapping[str, str],
) -> list[Finding]:
    dynamic_fields = {
        field_name
        for field_name, raw_key in (
            ("type", "raw_type"),
            ("source", "raw_source"),
            ("target", "raw_target"),
            ("mode", "raw_mode"),
            ("read_only", "raw_read_only"),
        )
        if _compose_contains_interpolation(volume.get(raw_key, ""))
    }
    if not dynamic_fields:
        return []
    if _is_allowed_published_artifact_mount_interpolation(volume, env, dynamic_fields):
        return []
    if _is_allowed_object_store_mount_interpolation(volume, env, dynamic_fields):
        return []
    raw_values = {
        field_name: volume.get(raw_key, "")
        for field_name, raw_key in (
            ("type", "raw_type"),
            ("source", "raw_source"),
            ("target", "raw_target"),
            ("mode", "raw_mode"),
            ("read_only", "raw_read_only"),
        )
        if field_name in dynamic_fields
    }
    return [
        Finding(
            "DISPLAY_MOUNT_DYNAMIC_INTERPOLATION",
            "display mounts must use literal audited values except approved readonly bind variables.",
            path=str(path),
            service=service_name,
            details={
                "volume": volume["text"],
                "fields": sorted(dynamic_fields),
                "raw_values": raw_values,
            },
        )
    ]


def _display_mount_relative_path_findings(
    path: Path,
    service_name: str,
    volume: Mapping[str, Any],
) -> list[Finding]:
    raw_source = str(volume.get("raw_source", "")).strip()
    if not raw_source or _compose_contains_interpolation(raw_source) or not _is_relative_host_path(raw_source):
        return []
    return [
        Finding(
            "DISPLAY_RELATIVE_MOUNT_SOURCE",
            "display bind sources must be absolute approved roots; relative paths are not allowed.",
            path=str(path),
            service=service_name,
            details={
                "volume": volume["text"],
                "source": raw_source,
                "resolved_from_compose": str((path.parent / raw_source).resolve()),
            },
        )
    ]


def _is_approved_display_published_mount(volume: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    return (
        _raw_mount_type_is_literal_bind(volume)
        and volume["source"] == env.get("NHMS_PUBLISHED_ARTIFACT_HOST_ROOT", "")
        and volume["target"] == env.get("NHMS_PUBLISHED_ARTIFACT_ROOT", "")
        and _raw_mount_field_has_canonical_identity(
            volume.get("raw_source", ""),
            PUBLISHED_ARTIFACT_MOUNT_SOURCE_VAR,
        )
        and _raw_mount_field_has_canonical_identity(
            volume.get("raw_target", ""),
            PUBLISHED_ARTIFACT_MOUNT_TARGET_VAR,
        )
        and volume["read_only"]
    )


def _is_approved_display_object_store_mount(volume: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    return (
        _raw_mount_type_is_literal_bind(volume)
        and volume["source"] == env.get(DISPLAY_OBJECT_STORE_MOUNT_SOURCE_VAR, "")
        and volume["target"] == env.get(DISPLAY_OBJECT_STORE_MOUNT_TARGET_VAR, "")
        and _raw_mount_field_has_canonical_identity(
            volume.get("raw_source", ""),
            DISPLAY_OBJECT_STORE_MOUNT_SOURCE_VAR,
        )
        and _raw_mount_field_has_canonical_identity(
            volume.get("raw_target", ""),
            DISPLAY_OBJECT_STORE_MOUNT_TARGET_VAR,
        )
        and volume["read_only"]
    )


def _is_published_mount_candidate(volume: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    expected_source = env.get("NHMS_PUBLISHED_ARTIFACT_HOST_ROOT", "")
    expected_target = env.get("NHMS_PUBLISHED_ARTIFACT_ROOT", "")
    return (
        "NHMS_PUBLISHED_ARTIFACT" in volume["text"]
        or volume["source"] in {expected_source, expected_target}
        or _targets_artifact_root_or_child(volume["target"], env)
    )


def _targets_artifact_root_or_child(target: str, env: Mapping[str, str]) -> bool:
    artifact_root = env.get("NHMS_PUBLISHED_ARTIFACT_ROOT", "").strip()
    return bool(artifact_root) and _is_path_equal_or_child(target, artifact_root)


def _is_allowed_published_artifact_mount_interpolation(
    volume: Mapping[str, Any],
    env: Mapping[str, str],
    dynamic_fields: set[str],
) -> bool:
    if not dynamic_fields <= {"source", "target"}:
        return False
    expected_source = env.get(PUBLISHED_ARTIFACT_MOUNT_SOURCE_VAR, "")
    expected_target = env.get(PUBLISHED_ARTIFACT_MOUNT_TARGET_VAR, "")
    if volume["source"] != expected_source or volume["target"] != expected_target:
        return False
    if not volume["read_only"]:
        return False
    if not _raw_mount_type_is_literal_bind(volume):
        return False
    return _raw_mount_field_has_canonical_identity(
        volume.get("raw_source", ""),
        PUBLISHED_ARTIFACT_MOUNT_SOURCE_VAR,
    ) and _raw_mount_field_has_canonical_identity(
        volume.get("raw_target", ""),
        PUBLISHED_ARTIFACT_MOUNT_TARGET_VAR,
    )


def _is_allowed_object_store_mount_interpolation(
    volume: Mapping[str, Any],
    env: Mapping[str, str],
    dynamic_fields: set[str],
) -> bool:
    if not dynamic_fields <= {"source", "target"}:
        return False
    expected_source = env.get(DISPLAY_OBJECT_STORE_MOUNT_SOURCE_VAR, "")
    expected_target = env.get(DISPLAY_OBJECT_STORE_MOUNT_TARGET_VAR, "")
    if volume["source"] != expected_source or volume["target"] != expected_target:
        return False
    if not volume["read_only"]:
        return False
    if not _raw_mount_type_is_literal_bind(volume):
        return False
    return _raw_mount_field_has_canonical_identity(
        volume.get("raw_source", ""),
        DISPLAY_OBJECT_STORE_MOUNT_SOURCE_VAR,
    ) and _raw_mount_field_has_canonical_identity(
        volume.get("raw_target", ""),
        DISPLAY_OBJECT_STORE_MOUNT_TARGET_VAR,
    )


def _display_named_volume_findings(
    *,
    path: Path,
    service_name: str,
    volume_name: str,
    volume: Mapping[str, Any],
    volume_definition: Mapping[str, Any],
    env: Mapping[str, str],
    compute_roots: Mapping[str, str],
) -> list[Finding]:
    driver = str(volume_definition.get("driver", "local") or "local")
    driver_opts = volume_definition.get("driver_opts", {})
    if driver != "local" or not isinstance(driver_opts, dict):
        return []
    device = driver_opts.get("device")
    if device is None:
        return []
    raw_device = str(device)
    device_path = _resolve_compose_value(raw_device, env)
    findings: list[Finding] = []
    if _compose_contains_interpolation(raw_device):
        findings.append(
            Finding(
                "DISPLAY_NAMED_VOLUME_DYNAMIC_INTERPOLATION",
                "display named volume bind devices must not use dynamic Compose interpolation.",
                path=str(path),
                service=service_name,
                details={
                    "volume": volume["text"],
                    "volume_name": volume_name,
                    "device": raw_device,
                    "variables": sorted(_compose_interpolation_keys(raw_device)),
                },
            )
        )
    if _is_relative_host_path(raw_device):
        findings.append(
            Finding(
                "DISPLAY_NAMED_VOLUME_RELATIVE_DEVICE",
                "display named volume bind devices must not use relative host paths.",
                path=str(path),
                service=service_name,
                details={
                    "volume": volume["text"],
                    "volume_name": volume_name,
                    "device": raw_device,
                    "resolved_from_compose": str((path.parent / raw_device).resolve()),
                },
            )
        )
    reasons: list[dict[str, str]] = []
    if _is_docker_socket_path(device_path):
        reasons.append({"reason": "docker_socket", "path": device_path})
    if _is_munge_path(device_path):
        reasons.append({"reason": "munge_path", "path": device_path})
    if _is_broad_host_path(device_path):
        reasons.append({"reason": "broad_host_root", "path": device_path})
    if any(token in device_path for token in FORBIDDEN_MOUNT_TOKENS):
        reasons.append({"reason": "forbidden_token", "path": device_path})
    matched_root = _matching_compute_root(device_path, compute_roots)
    if matched_root:
        root_key, root_value = matched_root
        reasons.append({"reason": "compute_root", "path": device_path, "root_key": root_key, "root": root_value})
    if _is_local_bind_volume(volume_definition):
        reasons.append({"reason": "unapproved_named_bind", "path": device_path})
    if reasons:
        findings.append(
            Finding(
                "DISPLAY_NAMED_VOLUME_FORBIDDEN_SOURCE",
                "display service named volume resolves to a forbidden host source.",
                path=str(path),
                service=service_name,
                details={
                    "volume": volume["text"],
                    "volume_name": volume_name,
                    "device": device_path,
                    "reasons": reasons,
                },
            )
        )
    return findings


def _raw_mount_type_is_literal_bind(volume: Mapping[str, Any]) -> bool:
    raw_type = str(volume.get("raw_type", "")).strip()
    return raw_type == "bind" and not _compose_contains_interpolation(raw_type)


def _raw_mount_field_has_canonical_identity(raw_value: Any, expected_key: str) -> bool:
    raw_text = str(raw_value).strip()
    match = _REQUIRED_MOUNT_ENV_IDENTITY_PATTERN.fullmatch(raw_text)
    if match is None:
        return False
    key = match.group("braced") or match.group("plain")
    return key == expected_key


def _required_mount_identity_fields(
    volume: Mapping[str, Any],
    *,
    source_key: str,
    target_key: str,
) -> list[str]:
    fields: list[str] = []
    if not _raw_mount_field_has_canonical_identity(volume.get("raw_source", ""), source_key):
        fields.append("source")
    if not _raw_mount_field_has_canonical_identity(volume.get("raw_target", ""), target_key):
        fields.append("target")
    return fields


def _require_mount(
    volumes: Sequence[dict[str, Any]],
    *,
    path: Path,
    service: str,
    env: Mapping[str, str],
    source_key: str,
    target_key: str,
    read_only: bool,
    missing_code: str,
    readonly_code: str,
    type_code: str,
    identity_code: str,
) -> list[Finding]:
    expected_source = env.get(source_key, "")
    expected_target = env.get(target_key, "")
    matches = [
        volume
        for volume in volumes
        if volume["source"] == expected_source and volume["target"] == expected_target
    ]
    if not matches:
        return [
            Finding(
                missing_code,
                f"missing bind mount from {source_key} to {target_key}.",
                path=str(path),
                service=service,
                details={"source_key": source_key, "target_key": target_key},
            )
        ]
    findings: list[Finding] = []
    for volume in matches:
        if not _raw_mount_type_is_literal_bind(volume):
            findings.append(
                Finding(
                    type_code,
                    f"{target_key} mount must be a canonical bind mount.",
                    path=str(path),
                    service=service,
                    details={
                        "source_key": source_key,
                        "target_key": target_key,
                        "actual_type": volume["type"] or "short-form-or-implicit",
                    },
                )
            )
        identity_fields = _required_mount_identity_fields(volume, source_key=source_key, target_key=target_key)
        if identity_fields:
            findings.append(
                Finding(
                    identity_code,
                    f"{target_key} mount must use canonical Compose env-variable identity.",
                    path=str(path),
                    service=service,
                    details={
                        "source_key": source_key,
                        "target_key": target_key,
                        "fields": identity_fields,
                        "raw_source": volume.get("raw_source", ""),
                        "raw_target": volume.get("raw_target", ""),
                    },
                )
            )
        if read_only and not volume["read_only"]:
            findings.append(
                Finding(
                    readonly_code,
                    f"{target_key} mount must be read-only.",
                    path=str(path),
                    service=service,
                )
            )
        if not read_only and volume["read_only"]:
            findings.append(
                Finding(
                    readonly_code,
                    f"{target_key} mount must be writable.",
                    path=str(path),
                    service=service,
                )
            )
    return findings


def _compute_port_findings(path: Path, service_name: str, service: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for port in service.get("ports", []) or []:
        if not _port_is_loopback(port):
            findings.append(
                Finding(
                    "COMPUTE_PUBLIC_PORT_EXPOSURE",
                    "compute control ports must bind localhost or stay unexposed by default.",
                    path=str(path),
                    service=service_name,
                    details={"port": port},
                )
            )
    return findings


def _docker_command_blockers(commands: Mapping[str, CommandResult], *, docker_root: str | None) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    checks = {
        "docker_version": "DOCKER_UNAVAILABLE",
        "docker_compose_version": "DOCKER_COMPOSE_UNAVAILABLE",
        "docker_info_docker_root": "DOCKER_ROOT_UNAVAILABLE",
        "docker_system_df": "DOCKER_SYSTEM_DF_UNAVAILABLE",
    }
    for name, code in checks.items():
        result = commands[name]
        if result.returncode != 0:
            blockers.append(
                {
                    "code": code,
                    "command": list(result.args),
                    "returncode": result.returncode,
                }
            )
    if commands["docker_info_docker_root"].returncode == 0 and not docker_root:
        blockers.append({"code": "DOCKER_ROOT_PARSE_FAILED", "command": list(commands["docker_info_docker_root"].args)})
    return blockers


def _docker_smoke_payload(
    *,
    status: str,
    evidence_root: Path,
    repo_root: Path,
    evidence_run_id: str | None,
    image_tag: str,
    dockerfile: Path,
    commands: Mapping[str, CommandResult],
    blockers: Sequence[Mapping[str, Any]],
    preflight: PreflightResult,
) -> dict[str, Any]:
    return {
        "schema_version": "nhms.two_node_docker.app_smoke.v1",
        "change_id": CHANGE_ID,
        "evidence_run_id": evidence_run_id or _evidence_run_id_from_output(evidence_root),
        "status": status,
        "checked_at": _now_iso(),
        "evidence_root": str(evidence_root),
        "repo_root": str(repo_root),
        "image_tag": image_tag,
        "dockerfile": str(_resolve_path(dockerfile, repo_root)),
        "preflight_evidence_path": str(preflight.evidence_path),
        "commands": {name: result.to_dict() for name, result in commands.items()},
        "expected_absent_binaries": list(APP_IMAGE_FORBIDDEN_BINARIES),
        "expected_absent_paths": list(APP_IMAGE_FORBIDDEN_PATHS),
        "blockers": list(blockers),
    }


def _evidence_run_id_from_output(path: Path) -> str | None:
    resolved = path.resolve(strict=False)
    parts = resolved.parts
    for marker in ("two-node-e2e", "test-two-node-e2e-evidence"):
        if marker not in parts:
            continue
        index = parts.index(marker)
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _safe_evidence_run_id(value: str) -> str:
    text = str(value).strip()
    if not SAFE_RUN_ID_RE.fullmatch(text) or ".." in text:
        raise ValueError("evidence_run_id must use only alphanumerics, '.', '_' or '-' and be at most 128 chars")
    return text


def _docker_smoke_command_blockers(commands: Mapping[str, CommandResult]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    image_inspect = commands.get("image_inspect")
    if image_inspect is None:
        blockers.append(
            {
                "code": "IMAGE_INSPECT_MISSING",
                "probe": "image_inspect",
            }
        )
        return blockers
    if image_inspect.returncode != 0:
        blockers.append(
            {
                "code": "IMAGE_INSPECT_FAILED",
                "command": list(image_inspect.args),
                "returncode": image_inspect.returncode,
            }
        )
        return blockers

    zero_exit_checks = {
        "image_absence_probe": "APP_IMAGE_FORBIDDEN_CAPABILITY_PRESENT",
        "compute_scheduler_command": "COMPUTE_SCHEDULER_HELP_FAILED",
        "display_startup_start": "DISPLAY_STARTUP_FAILED",
        "display_startup_probe": "DISPLAY_STARTUP_PROBE_FAILED",
    }
    for name, code in zero_exit_checks.items():
        result = commands.get(name)
        if result is None:
            blockers.append(
                {
                    "code": f"{code}_MISSING",
                    "probe": name,
                }
            )
            continue
        if result.returncode != 0:
            blockers.append(
                {
                    "code": code,
                    "command": list(result.args),
                    "returncode": result.returncode,
                }
            )
    display_startup_start = commands.get("display_startup_start")
    if display_startup_start is not None and display_startup_start.returncode == 0:
        display_startup_cleanup = commands.get("display_startup_cleanup")
        if display_startup_cleanup is None:
            blockers.append(
                {
                    "code": "DISPLAY_STARTUP_CLEANUP_MISSING",
                    "probe": "display_startup_cleanup",
                }
            )
        elif display_startup_cleanup.returncode != 0:
            blockers.append(
                {
                    "code": "DISPLAY_STARTUP_CLEANUP_FAILED",
                    "command": list(display_startup_cleanup.args),
                    "returncode": display_startup_cleanup.returncode,
                }
            )
    expected_rejections = {
        "display_compute_env_reject": "DISPLAY_BOUNDARY_CONFIG_UNSAFE",
        "slurm_gateway_reject": "SERVICE_ROLE_RESERVED",
        "display_scheduler_reject": "DISPLAY_COMMAND_FORBIDDEN",
    }
    for name, expected_code in expected_rejections.items():
        result = commands.get(name)
        if result is None:
            blockers.append(
                {
                    "code": f"{expected_code}_PROBE_MISSING",
                    "probe": name,
                    "expected_stderr_code": expected_code,
                }
            )
            continue
        combined = result.stdout + result.stderr
        if result.returncode == 0 or expected_code not in combined:
            blockers.append(
                {
                    "code": f"{expected_code}_NOT_ENFORCED",
                    "command": list(result.args),
                    "returncode": result.returncode,
                    "expected_stderr_code": expected_code,
                }
            )
    return blockers


def _docker_build_failure_code(result: CommandResult) -> str:
    combined = f"{result.stdout}\n{result.stderr}".lower()
    blocked_markers = (
        "client.timeout",
        "request canceled",
        "connection timed out",
        "temporary failure in name resolution",
        "no route to host",
        "network is unreachable",
        "tls handshake timeout",
        "proxyconnect",
        "registry-1.docker.io",
        "failed to resolve source metadata",
        "error getting credentials",
    )
    if any(marker in combined for marker in blocked_markers):
        return "DOCKER_BUILD_BLOCKED"
    return "DOCKER_BUILD_FAILED"


def _docker_smoke_status(blockers: Sequence[Mapping[str, Any]]) -> str:
    if not blockers:
        return "PASS"
    blocked_codes = {"DOCKER_PREFLIGHT_BLOCKED", "DOCKER_BUILD_BLOCKED"}
    codes = {str(blocker.get("code", "")) for blocker in blockers}
    if codes and codes <= blocked_codes:
        return "BLOCKED"
    return "FAIL"


def _image_absence_probe_script() -> str:
    binaries = " ".join(APP_IMAGE_FORBIDDEN_BINARIES)
    paths = " ".join(APP_IMAGE_FORBIDDEN_PATHS)
    return "\n".join(
        [
            "set -eu",
            f"for binary in {binaries}; do",
            "  if command -v \"$binary\" >/dev/null 2>&1; then",
            "    echo \"forbidden binary present: $binary\" >&2",
            "    exit 1",
            "  fi",
            "done",
            f"for path in {paths}; do",
            "  if [ -e \"$path\" ]; then",
            "    echo \"forbidden path present: $path\" >&2",
            "    exit 1",
            "  fi",
            "done",
        ]
    )


def _dev_compose_findings(path: Path, repo_root: Path, *, role: str) -> list[Finding]:
    dev_compose = (repo_root / "infra" / "docker-compose.dev.yml").resolve()
    if path.resolve() != dev_compose:
        return []
    return [
        Finding(
            "DEV_COMPOSE_PRODUCTION_MISUSE",
            "infra/docker-compose.dev.yml is development-only and cannot be a production two-node compose input.",
            path=str(path),
            details={"role": role},
        )
    ]


def _services(compose: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    services = compose.get("services", {})
    if not isinstance(services, dict):
        return {}
    return {str(name): service for name, service in services.items() if isinstance(service, dict)}


def _named_volumes(compose: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    volumes = compose.get("volumes", {})
    if not isinstance(volumes, dict):
        return {}
    return {str(name): volume for name, volume in volumes.items() if isinstance(volume, dict)}


def _compose_entry_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        return [value]
    return [value]


def _file_ingress_entry_source_target(entry: Any, singular: str) -> tuple[str, str]:
    if isinstance(entry, Mapping):
        source = str(entry.get("source", entry.get(singular, entry.get("name", ""))))
        target = str(entry.get("target", entry.get("uid", "")))
        return source, target
    return str(entry), ""


def _file_ingress_definition_details(definition: Any) -> dict[str, Any]:
    if not isinstance(definition, Mapping):
        return {}
    details: dict[str, Any] = {}
    for key in ("file", "external", "name"):
        if key in definition:
            details[key] = definition[key]
    return details


def _compose_interpolation_contract_findings(
    path: Path,
    compose: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    role: str,
) -> list[Finding]:
    if role not in {"compute", "display"}:
        raise AssertionError(f"unsupported role: {role}")
    used_keys = _compose_interpolation_keys(compose)
    approved_keys = COMPUTE_AUDITED_INTERPOLATION_ENV if role == "compute" else DISPLAY_AUDITED_INTERPOLATION_ENV
    findings: list[Finding] = []
    missing_keys = sorted(used_keys - set(env))
    if missing_keys:
        findings.append(
            Finding(
                f"{role.upper()}_INTERPOLATION_ENV_MISSING",
                f"{role} env file must declare every Compose interpolation variable used by the role compose file.",
                path=str(path),
                details={"missing_keys": missing_keys},
            )
        )
    unapproved_keys = sorted(used_keys - approved_keys)
    if unapproved_keys:
        findings.append(
            Finding(
                f"{role.upper()}_INTERPOLATION_ENV_UNAPPROVED",
                f"{role} compose file references interpolation variables outside the approved role contract.",
                path=str(path),
                details={"unapproved_keys": unapproved_keys},
            )
        )
    findings.extend(
        _compose_interpolation_value_drift_findings(
            path=path,
            compose=compose,
            env=env,
            role=role,
            approved_keys=approved_keys,
        )
    )
    findings.extend(_compose_field_render_drift_findings(path=path, compose=compose, env=env, role=role))
    for key in sorted(used_keys & approved_keys):
        if key not in env:
            continue
        if key not in os.environ:
            continue
        process_value = os.environ[key]
        env_file_value = env.get(key, "")
        if process_value == env_file_value:
            continue
        findings.append(
            Finding(
                f"{role.upper()}_AMBIENT_ENV_OVERRIDE",
                f"ambient process environment overrides an audited {role} Compose interpolation variable.",
                path=str(path),
                details={
                    "key": key,
                    "env_file_value": _redact_env_value(key, env_file_value),
                    "process_value": _redact_env_value(key, process_value),
                },
            )
        )
    return findings


def _compose_interpolation_value_drift_findings(
    *,
    path: Path,
    compose: Mapping[str, Any],
    env: Mapping[str, str],
    role: str,
    approved_keys: frozenset[str],
) -> list[Finding]:
    findings: list[Finding] = []
    for compose_path, text in _compose_interpolation_text_nodes(compose):
        for occurrence in _compose_interpolation_occurrences_from_text(text):
            key = occurrence.key
            if key not in approved_keys or key not in env:
                continue
            rendered = _compose_interpolation_contract_value(occurrence, env)
            expected = env[key]
            if rendered == expected:
                continue
            findings.append(
                Finding(
                    f"{role.upper()}_INTERPOLATION_VALUE_DRIFT",
                    f"{role} Compose interpolation must resolve to the matching env-file value.",
                    path=str(path),
                    details={
                        "compose_path": compose_path,
                        "key": key,
                        "operator": occurrence.operator,
                        "expression": _redact_interpolation_expression(key, occurrence.expression),
                        "env_file_value": _redact_env_value(key, expected),
                        "rendered_value": _redact_env_value(key, rendered),
                    },
                )
            )
    return findings


def _compose_field_render_drift_findings(
    *,
    path: Path,
    compose: Mapping[str, Any],
    env: Mapping[str, str],
    role: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for service_name, service in _services(compose).items():
        contract = _canonical_service_field_render_contract(role=role, service_name=service_name, env=env)
        for field_name in ("image", "user", "ports", "command", "entrypoint"):
            expected = contract.get(field_name, _NO_FIELD_CONTRACT)
            if expected is _NO_FIELD_CONTRACT:
                continue
            raw_value = service.get(field_name)
            if expected is _FIELD_MUST_BE_ABSENT:
                if _absent_field_contract_is_satisfied(service, field_name, role=role):
                    continue
                expected_detail: Any = _absent_field_expected_detail(field_name, role=role)
            else:
                expected_detail = expected
            actual = _render_deployment_field(field_name, raw_value, env)
            if expected is not _FIELD_MUST_BE_ABSENT and actual == expected:
                continue
            variables = sorted(_compose_interpolation_keys(raw_value))
            findings.append(
                Finding(
                    f"{role.upper()}_INTERPOLATION_FIELD_RENDER_DRIFT",
                    f"{role} service {field_name} must render to the approved deployment contract.",
                    path=str(path),
                    service=service_name,
                    details={
                        "field": field_name,
                        "compose_path": f"$.services.{_compose_path_segment(service_name)}.{field_name}",
                        "variables": variables,
                        "expected_rendered": expected_detail,
                        "actual_rendered": actual,
                    },
                )
            )
    return findings


def _absent_field_contract_is_satisfied(service: Mapping[str, Any], field_name: str, *, role: str) -> bool:
    if field_name not in service:
        return True
    value = service.get(field_name)
    if value is None:
        return True
    if role == "compute" and field_name == "ports":
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 0
    return False


def _absent_field_expected_detail(field_name: str, *, role: str) -> str:
    if role == "compute" and field_name == "ports":
        return "<absent-or-empty-list>"
    return "<absent>"


def _canonical_service_field_render_contract(
    *,
    role: str,
    service_name: str,
    env: Mapping[str, str],
) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "image": f"{env.get('NHMS_APP_IMAGE', '')}:{env.get('NHMS_IMAGE_TAG', '')}",
        "user": f"{env.get('NHMS_CONTAINER_UID', '')}:{env.get('NHMS_CONTAINER_GID', '')}",
        "entrypoint": _FIELD_MUST_BE_ABSENT,
    }
    if role == "display":
        contract["ports"] = [f"127.0.0.1:{env.get('NHMS_DISPLAY_API_PORT', '')}:8000"]
        contract["command"] = list(API_SERVICE_COMMAND)
        return contract
    contract["ports"] = _FIELD_MUST_BE_ABSENT
    contract["command"] = (
        list(COMPUTE_SCHEDULER_COMMAND)
        if service_name == "scheduler-once"
        else list(API_SERVICE_COMMAND)
    )
    return contract


def _render_deployment_field(field_name: str, value: Any, env: Mapping[str, str]) -> Any:
    if field_name in {"command", "entrypoint"}:
        if value is None:
            return []
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [_resolved_compose_text(item, env) for item in value]
        return _resolved_compose_text(value, env)
    if field_name == "ports":
        return [_render_compose_value(port, env) for port in _compose_entry_list(value)]
    return _resolved_compose_text(value, env)


def _render_compose_value(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _render_compose_value(item, env) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_render_compose_value(item, env) for item in value]
    return _resolved_compose_text(value, env)


def _display_environment_list_findings(
    path: Path,
    service_name: str,
    service: Mapping[str, Any],
) -> list[Finding]:
    environment = service.get("environment", {})
    if not isinstance(environment, list):
        return []
    findings: list[Finding] = []
    for item in environment:
        text = str(item)
        key, _value = _split_environment_list_entry(text)
        has_interpolation = _compose_contains_interpolation(text)
        if _compose_contains_interpolation(key):
            findings.append(
                Finding(
                    "DISPLAY_ENV_DYNAMIC_KEY",
                    "display environment list entries must not use Compose interpolation in the key.",
                    path=str(path),
                    service=service_name,
                    details={"entry": text, "variables": sorted(_compose_interpolation_keys(key))},
                )
            )
        if has_interpolation:
            for candidate_key, candidate_value in _environment_entry_candidates(text):
                if candidate_key in DISPLAY_FORBIDDEN_ENV_KEYS or candidate_key in COMPUTE_ONLY_PATH_ENV_KEYS:
                    findings.append(
                        Finding(
                            "DISPLAY_FORBIDDEN_ENV",
                            f"display service must not configure {candidate_key}.",
                            path=str(path),
                            service=service_name,
                            details={
                                "key": candidate_key,
                                "entry": text,
                                "candidate_value": candidate_value,
                            },
                        )
                    )
    return findings


def _service_environment(service: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[str, str]:
    interpolation_env = env or {}
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return {
            str(key): (
                interpolation_env.get(str(key), "")
                if value is None
                else _resolved_compose_text(value, interpolation_env)
            )
            for key, value in environment.items()
        }
    if isinstance(environment, list):
        parsed: dict[str, str] = {}
        for item in environment:
            text = str(item)
            key, value = _split_environment_list_entry(text)
            if value is not None:
                parsed[_resolve_compose_value(key, interpolation_env)] = _resolve_compose_value(
                    value,
                    interpolation_env,
                )
            else:
                resolved_text = _resolve_compose_value(text, interpolation_env)
                resolved_key, resolved_value = _split_environment_list_entry(resolved_text)
                if resolved_value is not None:
                    parsed[resolved_key] = resolved_value
                elif resolved_text:
                    parsed[resolved_text] = interpolation_env.get(resolved_text, "")
        return parsed
    return {}


def _volume_info(volume: Any, env: Mapping[str, str]) -> dict[str, Any]:
    if isinstance(volume, dict):
        raw_type = str(volume.get("type", ""))
        raw_source = str(volume.get("source", ""))
        raw_target = str(volume.get("target", volume.get("destination", "")))
        raw_mode = str(volume.get("mode", ""))
        raw_read_only = str(volume.get("read_only", "")) if "read_only" in volume else ""
        mount_type = _resolve_compose_value(raw_type, env)
        source = _resolve_compose_value(raw_source, env)
        target = _resolve_compose_value(raw_target, env)
        mode = _resolved_compose_text(raw_mode, env)
        read_only_value = _compose_bool(_resolve_compose_object(volume.get("read_only"), env))
        read_only = read_only_value is True or _mode_is_readonly(mode)
        return {
            "source": source,
            "target": target,
            "read_only": read_only,
            "text": json.dumps(volume, sort_keys=True),
            "type": mount_type,
            "raw_type": raw_type,
            "raw_source": raw_source,
            "raw_target": raw_target,
            "raw_mode": raw_mode,
            "raw_read_only": raw_read_only,
        }
    text = str(volume)
    raw_source, raw_target, source, target, raw_mode, mode = _parse_short_volume(text, env)
    return {
        "source": source,
        "target": target,
        "read_only": _mode_is_readonly(mode),
        "text": text,
        "type": "",
        "raw_type": "",
        "raw_source": raw_source,
        "raw_target": raw_target,
        "raw_mode": raw_mode,
        "raw_read_only": "",
    }


def _tmpfs_infos(tmpfs_entries: Any, env: Mapping[str, str]) -> list[dict[str, str]]:
    if isinstance(tmpfs_entries, (str, bytes)) or not isinstance(tmpfs_entries, Sequence):
        entries = [tmpfs_entries]
    else:
        entries = list(tmpfs_entries)
    infos: list[dict[str, str]] = []
    for entry in entries:
        if isinstance(entry, dict):
            raw_target = str(entry.get("target", entry.get("destination", entry.get("path", ""))))
            text = json.dumps(entry, sort_keys=True)
        else:
            text = str(entry)
            raw_target = _split_compose_short_volume(text)[0] if text else ""
        infos.append(
            {
                "target": _resolve_compose_value(raw_target, env),
                "raw_target": raw_target,
                "text": text,
            }
        )
    return infos


def _parse_short_volume(volume: str, env: Mapping[str, str]) -> tuple[str, str, str, str, str, str]:
    parts = _split_compose_short_volume(volume)
    if len(parts) == 1:
        return "", parts[0], "", _resolve_compose_value(parts[0], env), "", ""
    if len(parts) == 2:
        source, target = parts
        return source, target, _resolve_compose_value(source, env), _resolve_compose_value(target, env), "", ""
    source, target, *mode_parts = parts
    raw_mode = ":".join(mode_parts)
    mode = _resolve_compose_value(raw_mode, env)
    return source, target, _resolve_compose_value(source, env), _resolve_compose_value(target, env), raw_mode, mode


def _split_compose_short_volume(volume: str) -> list[str]:
    parts: list[str] = []
    start = 0
    brace_depth = 0
    for index, char in enumerate(volume):
        if char == "{" and index > 0 and volume[index - 1] == "$":
            brace_depth += 1
        elif char == "}" and brace_depth:
            brace_depth -= 1
        elif char == ":" and not brace_depth:
            parts.append(volume[start:index])
            start = index + 1
    parts.append(volume[start:])
    return parts


def _split_environment_list_entry(entry: str) -> tuple[str, str | None]:
    brace_depth = 0
    for index, char in enumerate(entry):
        if char == "{" and index > 0 and entry[index - 1] == "$":
            brace_depth += 1
        elif char == "}" and brace_depth:
            brace_depth -= 1
        elif char == "=" and not brace_depth:
            return entry[:index], entry[index + 1 :]
    return entry, None


def _environment_entry_candidates(entry: str) -> set[tuple[str, str]]:
    candidates: set[tuple[str, str]] = set()
    key, value = _split_environment_list_entry(entry)
    if value is not None:
        candidates.add((key, value))
    for payload in _compose_interpolation_default_or_alternate_values(entry):
        payload_key, payload_value = _split_environment_list_entry(payload)
        if payload_value is not None:
            candidates.add((payload_key, payload_value))
    return candidates


def _compose_interpolation_default_or_alternate_values(value: str) -> set[str]:
    return {
        occurrence.payload
        for occurrence in _compose_interpolation_occurrences_from_text(value)
        if occurrence.operator in {":-", "-", ":+", "+"}
    } - {""}


def _compose_interpolation_uses_env_file_value(value: Any, key: str, env: Mapping[str, str]) -> bool:
    if not isinstance(value, str):
        return False
    if key not in env:
        return False
    return any(
        occurrence.key == key and _compose_interpolation_uses_current_env_value(occurrence.operator, env[key])
        for occurrence in _compose_interpolation_occurrences_from_text(value)
    )


def _compose_interpolation_matches_env_file_value(value: Any, key: str, env: Mapping[str, str]) -> bool:
    if not isinstance(value, str):
        return False
    if key not in env:
        return False
    for occurrence in _compose_interpolation_occurrences_from_text(value):
        if (
            occurrence.key == key
            and occurrence.operator in {":+", "+"}
            and _compose_interpolation_contract_value(occurrence, env) != env[key]
        ):
            return False
    if _resolved_compose_text(value, env) != env[key]:
        return False
    variables = _compose_interpolation_keys(value)
    return variables == {key} or _compose_interpolation_uses_env_file_value(value, key, env)


def _compose_interpolation_uses_current_env_value(operator: str | None, current: str) -> bool:
    if operator is None:
        return True
    if operator in {"-", "?"}:
        return True
    if operator in {":-", ":?"}:
        return current != ""
    return False


def _compose_interpolation_contract_value(
    occurrence: ComposeInterpolationOccurrence,
    env: Mapping[str, str],
) -> str:
    if occurrence.operator in {":+", "+"}:
        return occurrence.literal_prefix + _resolve_compose_value(occurrence.payload, env)
    return occurrence.literal_prefix + _resolve_compose_value(occurrence.expression, env)


def _mode_is_readonly(mode: str) -> bool:
    return "ro" in {part.strip().lower() for part in mode.split(",")}


def _runtime_env_findings(
    *,
    path: Path,
    service: str,
    service_env: Mapping[str, str],
    required_keys: frozenset[str],
    expected_values: Mapping[str, str],
    role: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for key in sorted(required_keys):
        if key not in service_env:
            findings.append(
                Finding(
                    f"{role.upper()}_RUNTIME_ENV_MISSING",
                    f"{role} service runtime env must define {key}.",
                    path=str(path),
                    service=service,
                    details={"key": key},
                )
            )
            continue
        if key in NONEMPTY_RUNTIME_ENV and not service_env[key].strip():
            findings.append(
                Finding(
                    f"{role.upper()}_RUNTIME_ENV_EMPTY",
                    f"{role} service runtime env {key} must not be empty.",
                    path=str(path),
                    service=service,
                    details={"key": key},
                )
            )
    for key, expected in expected_values.items():
        actual = service_env.get(key)
        if actual is None:
            continue
        if actual.strip().lower() != expected:
            findings.append(
                Finding(
                    f"{role.upper()}_RUNTIME_ENV_VALUE_INVALID",
                    f"{role} service runtime env {key} must be {expected}.",
                    path=str(path),
                    service=service,
                    details={"key": key, "expected": expected, "actual": actual},
                )
            )
    return findings


def _has_compose_value(service: Mapping[str, Any], key: str) -> bool:
    value = service.get(key)
    if value is None:
        return False
    if isinstance(value, (str, bytes)):
        return bool(value)
    if isinstance(value, Sequence):
        return bool(value)
    if isinstance(value, Mapping):
        return bool(value)
    return True


def _compose_contains_interpolation(value: Any) -> bool:
    return bool(_compose_interpolation_keys(value))


def _compose_interpolation_text_nodes(value: Any, compose_path: str = "$") -> Iterator[tuple[str, str]]:
    stack: list[tuple[str, Any, int]] = [(compose_path, value, 0)]
    visited_nodes = 0
    scheduled_nodes = 1
    while stack:
        current_path, current, depth = stack.pop()
        visited_nodes += 1
        _check_compose_interpolation_object_bounds(visited_nodes=visited_nodes, depth=depth)
        if isinstance(current, str):
            yield current_path, current
            continue
        if isinstance(current, Mapping):
            for raw_key, raw_value in current.items():
                key_path = f"{current_path}.{_compose_path_segment(raw_key)}"
                for child in ((key_path, raw_value, depth + 1), (f"{key_path}<key>", raw_key, depth + 1)):
                    scheduled_nodes += 1
                    _check_compose_interpolation_object_bounds(visited_nodes=scheduled_nodes, depth=child[2])
                    stack.append(child)
            continue
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            for index in range(len(current) - 1, -1, -1):
                child = (f"{current_path}[{index}]", current[index], depth + 1)
                scheduled_nodes += 1
                _check_compose_interpolation_object_bounds(visited_nodes=scheduled_nodes, depth=child[2])
                stack.append(child)


def _compose_path_segment(value: Any) -> str:
    text = str(value)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", text):
        return text
    return json.dumps(text)


def _compose_interpolation_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    for _compose_path, text in _compose_interpolation_text_nodes(value):
        keys.update(_compose_interpolation_keys_from_text(text))
    return keys


def _compose_interpolation_keys_from_text(value: str) -> set[str]:
    return {occurrence.key for occurrence in _compose_interpolation_occurrences_from_text(value)}


def _compose_interpolation_occurrences_from_text(
    value: str,
    *,
    _depth: int = 0,
    _count: list[int] | None = None,
) -> list[ComposeInterpolationOccurrence]:
    _check_compose_interpolation_bounds(value, depth=_depth)
    count = _count if _count is not None else [0]
    occurrences: list[ComposeInterpolationOccurrence] = []
    for token in _compose_dollar_run_tokens(value):
        if token.occurrence is None:
            continue
        count[0] += 1
        if count[0] > MAX_COMPOSE_INTERPOLATION_OCCURRENCES:
            raise ComposeInterpolationLimitError(
                "Compose interpolation occurrence count exceeds the static validation limit.",
                metric="occurrences",
                limit=MAX_COMPOSE_INTERPOLATION_OCCURRENCES,
            )
        occurrences.append(token.occurrence)
        if token.occurrence.operator is not None and token.occurrence.payload:
            occurrences.extend(
                _compose_interpolation_occurrences_from_text(
                    token.occurrence.payload,
                    _depth=_depth + 1,
                    _count=count,
                )
            )
    return occurrences


def _compose_dollar_run_tokens(value: str) -> Iterator[ComposeDollarRunToken]:
    index = 0
    while index < len(value):
        if value[index] != "$":
            index += 1
            continue
        run_start = index
        run_end = index
        while run_end < len(value) and value[run_end] == "$":
            run_end += 1
        run_length = run_end - run_start
        literal_dollars = "$" * (run_length // 2)
        if run_length % 2:
            interpolation_start = run_end - 1
            occurrence, token_end = _compose_interpolation_from_dollar(value, interpolation_start, literal_dollars)
            if occurrence is not None:
                yield ComposeDollarRunToken(
                    start=run_start,
                    end=token_end,
                    literal_dollars=literal_dollars,
                    occurrence=occurrence,
                )
                index = token_end
                continue
            literal_dollars += "$"
        yield ComposeDollarRunToken(start=run_start, end=run_end, literal_dollars=literal_dollars)
        index = run_end


def _compose_interpolation_from_dollar(
    value: str,
    dollar_index: int,
    literal_prefix: str,
) -> tuple[ComposeInterpolationOccurrence | None, int]:
    next_index = dollar_index + 1
    if next_index >= len(value):
        return None, next_index
    if value[next_index] == "{":
        close_index = _find_matching_interpolation_brace(value, next_index)
        if close_index is None:
            return None, next_index
        payload = value[next_index + 1 : close_index]
        parsed = _parse_compose_interpolation_payload(payload)
        if parsed is None:
            return None, next_index
        key, operator, operator_payload = parsed
        return (
            ComposeInterpolationOccurrence(
                key=key,
                operator=operator,
                payload=operator_payload,
                expression=value[dollar_index : close_index + 1],
                literal_prefix=literal_prefix,
            ),
            close_index + 1,
        )
    if value[next_index].isalpha() or value[next_index] == "_":
        end = next_index + 1
        while end < len(value) and (value[end].isalnum() or value[end] == "_"):
            end += 1
        return (
            ComposeInterpolationOccurrence(
                key=value[next_index:end],
                operator=None,
                payload="",
                expression=value[dollar_index:end],
                literal_prefix=literal_prefix,
            ),
            end,
        )
    return None, next_index


def _parse_compose_interpolation_payload(payload: str) -> tuple[str, str | None, str] | None:
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(.*)", payload, flags=re.DOTALL)
    if match is None:
        return None
    key = match.group(1)
    suffix = match.group(2)
    for operator in (":-", ":+", ":?", "-", "+", "?"):
        if suffix.startswith(operator):
            return key, operator, suffix[len(operator) :]
    return key, None, suffix


def _find_matching_interpolation_brace(value: str, open_index: int) -> int | None:
    depth = 0
    index = open_index
    while index < len(value):
        if value[index] == "{" and index > 0 and value[index - 1] == "$":
            depth += 1
            if depth > MAX_COMPOSE_INTERPOLATION_DEPTH:
                raise ComposeInterpolationLimitError(
                    "Compose interpolation nesting exceeds the static validation limit.",
                    metric="depth",
                    limit=MAX_COMPOSE_INTERPOLATION_DEPTH,
                )
        elif value[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _resolve_compose_object(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _resolve_compose_value(value, env)
    return value


def _resolved_compose_text(value: Any, env: Mapping[str, str]) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return _resolve_compose_value(str(value), env)


def _compose_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _nonempty_compose_sequence(value: Any, env: Mapping[str, str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        resolved = _resolve_compose_value(value, env).strip()
        return [resolved] if resolved else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            resolved
            for item in value
            if (resolved := _resolved_compose_text(item, env).strip())
        ]
    return [str(value)] if str(value).strip() else []


def _cap_drop_all_literal(value: Any) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return False
    items = [str(item).strip() for item in value]
    return items == ["ALL"] and not _compose_contains_interpolation(value)


def _security_opt_no_new_privileges_literal(value: Any) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return False
    items = [str(item).strip() for item in value]
    return items == ["no-new-privileges:true"] and not _compose_contains_interpolation(value)


def _is_local_bind_volume(volume_definition: Mapping[str, Any]) -> bool:
    driver = str(volume_definition.get("driver", "local") or "local")
    driver_opts = volume_definition.get("driver_opts", {})
    if driver != "local" or not isinstance(driver_opts, Mapping):
        return False
    option_text = " ".join(
        str(driver_opts.get(key, ""))
        for key in ("type", "o", "device")
    ).lower()
    return "bind" in option_text or "type none" in option_text or str(driver_opts.get("type", "")).lower() == "none"


def _compose_interpolation_limit_finding(error: ComposeInterpolationLimitError) -> Finding:
    return Finding(
        "COMPOSE_INTERPOLATION_COMPLEXITY_LIMIT_EXCEEDED",
        "Compose interpolation input exceeds the static validator bounds.",
        details={"metric": error.metric, "limit": error.limit, "error": str(error)},
    )


def _check_compose_interpolation_bounds(value: str, *, depth: int) -> None:
    if len(value) > MAX_COMPOSE_INTERPOLATION_TEXT_CHARS:
        raise ComposeInterpolationLimitError(
            "Compose interpolation text exceeds the static validation limit.",
            metric="text_chars",
            limit=MAX_COMPOSE_INTERPOLATION_TEXT_CHARS,
        )
    if depth > MAX_COMPOSE_INTERPOLATION_DEPTH:
        raise ComposeInterpolationLimitError(
            "Compose interpolation nesting exceeds the static validation limit.",
            metric="depth",
            limit=MAX_COMPOSE_INTERPOLATION_DEPTH,
        )


def _check_compose_interpolation_object_bounds(*, visited_nodes: int, depth: int) -> None:
    if visited_nodes > MAX_COMPOSE_INTERPOLATION_OBJECT_NODES:
        raise ComposeInterpolationLimitError(
            "Compose interpolation YAML object traversal exceeds the static validation node limit.",
            metric="object_nodes",
            limit=MAX_COMPOSE_INTERPOLATION_OBJECT_NODES,
        )
    if depth > MAX_COMPOSE_INTERPOLATION_DEPTH:
        raise ComposeInterpolationLimitError(
            "Compose interpolation YAML object nesting exceeds the static validation depth limit.",
            metric="object_depth",
            limit=MAX_COMPOSE_INTERPOLATION_DEPTH,
        )


def _redact_finding_details(details: Mapping[str, Any]) -> dict[str, Any]:
    redacted = _redact_detail_value(details, secret_context=False, field_name=None)
    return redacted if isinstance(redacted, dict) else {}


def _redact_detail_value(value: Any, *, secret_context: bool, field_name: str | None) -> Any:
    if isinstance(value, Mapping):
        mapping_secret_context = secret_context or _mapping_has_secret_key_context(value)
        return {
            str(key): _redact_detail_value(
                item,
                secret_context=mapping_secret_context or str(key) in SECRET_ENV_KEYS,
                field_name=str(key),
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        sequence_secret_context = secret_context or any(
            isinstance(item, str) and item in SECRET_ENV_KEYS for item in value
        )
        return [
            _redact_detail_value(item, secret_context=sequence_secret_context, field_name=field_name)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_detail_text(value, secret_context=secret_context, field_name=field_name)
    return value


def _mapping_has_secret_key_context(value: Mapping[str, Any]) -> bool:
    for context_field in ("key", "candidate_key", "source_key", "target_key"):
        candidate = value.get(context_field)
        if isinstance(candidate, str) and candidate in SECRET_ENV_KEYS:
            return True
    variables = value.get("variables")
    return isinstance(variables, Sequence) and not isinstance(variables, (str, bytes)) and any(
        isinstance(item, str) and item in SECRET_ENV_KEYS for item in variables
    )


def _redact_detail_text(value: str, *, secret_context: bool, field_name: str | None) -> str:
    if not value:
        return value
    if value in SECRET_ENV_KEYS and field_name in _SECRET_PRESERVED_VALUE_FIELDS:
        return value
    if field_name in _SECRET_DETAIL_VALUE_FIELDS:
        return "<redacted>"
    if secret_context and value in SECRET_ENV_KEYS:
        return value
    text = _redact_secret_mapping_lines(value)
    if _SECRET_INTERPOLATION_PATTERN.search(text) or _SECRET_URL_PATTERN.search(text):
        return "<redacted>"
    if secret_context:
        return text if text != value else "<redacted>"
    return text


def _redact_secret_mapping_lines(value: str) -> str:
    lines = value.splitlines(keepends=True)
    if not lines:
        return value
    redacted_lines = [
        _redact_secret_mapping_line(line)
        if _SECRET_MAPPING_LINE_PATTERN.search(line)
        else line
        for line in lines
    ]
    return "".join(redacted_lines)


def _redact_secret_mapping_line(line: str) -> str:
    line_ending = ""
    body = line
    if body.endswith("\r\n"):
        body = body[:-2]
        line_ending = "\r\n"
    elif body.endswith("\n"):
        body = body[:-1]
        line_ending = "\n"
    elif body.endswith("\r"):
        body = body[:-1]
        line_ending = "\r"
    if "=" in body:
        separator = "="
    elif ":" in body:
        separator = ":"
    else:
        return f"<redacted>{line_ending}"
    prefix = body.split(separator, 1)[0].strip()
    return f"{prefix}{separator} <redacted>{line_ending}"


def _redact_env_value(key: str, value: str) -> str:
    if key in SECRET_ENV_KEYS:
        return "<redacted>" if value else ""
    return value


def _redact_interpolation_expression(key: str, expression: str) -> str:
    if key in SECRET_ENV_KEYS:
        return "<redacted>"
    return _redact_detail_text(expression, secret_context=False, field_name="expression")


def _is_namespace_sharing_mode(value: str) -> bool:
    mode = value.strip().lower()
    return mode.startswith("service:") or mode.startswith("container:")


def _compute_only_roots(env: Mapping[str, str]) -> dict[str, str]:
    roots: dict[str, str] = {}
    prioritized_keys = (
        "WORKSPACE_ROOT",
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "RUN_WORKSPACE_ROOT",
        "SHARED_LOG_ROOT",
        "NHMS_OBJECT_STORE_COPYBACK_ROOT",
        "SLURM_GATEWAY_WORKSPACE_DIR",
        "SLURM_GATEWAY_TEMPLATE_DIR",
        "MUNGE_SOCKET",
        "MUNGE_KEY",
        "SHUD_EXECUTABLE",
    )
    for key in prioritized_keys:
        value = env.get(key, "").strip()
        if value.startswith("/"):
            roots[key] = _normalize_posix_path(value)
    return roots


def _is_broad_host_path(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    return normalized in BROAD_HOST_ROOTS or normalized.startswith("/scratch/")


def _is_docker_socket_path(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    return normalized in {"/var/run/docker.sock", "/run/docker.sock"} or (
        normalized.startswith("/") and normalized.endswith("/docker.sock")
    )


def _is_munge_path(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    if not normalized.startswith("/"):
        return False
    return (
        normalized in {"/run/munge", "/var/run/munge", "/etc/munge"}
        or normalized.startswith("/run/munge/")
        or normalized.startswith("/var/run/munge/")
        or normalized.startswith("/etc/munge/")
        or normalized.endswith("/munge.key")
        or normalized.endswith("/munge.socket")
        or "/munge.socket." in normalized
    )


def _is_relative_host_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped or stripped.startswith("/") or stripped.startswith("$"):
        return False
    return stripped in {".", ".."} or stripped.startswith(("./", "../")) or "/" in stripped


def _is_path_equal_or_child(value: str, root: str) -> bool:
    normalized = _normalize_posix_path(value)
    normalized_root = _normalize_posix_path(root)
    if not normalized.startswith("/") or not normalized_root.startswith("/"):
        return False
    return normalized == normalized_root or normalized.startswith(f"{normalized_root.rstrip('/')}/")


def _posix_path_is_child(value: str, root: str) -> bool:
    normalized = _normalize_posix_path(value)
    normalized_root = _normalize_posix_path(root)
    if not normalized.startswith("/") or not normalized_root.startswith("/"):
        return False
    return normalized != normalized_root and normalized.startswith(f"{normalized_root.rstrip('/')}/")


def _matching_compute_root(value: str, roots: Mapping[str, str]) -> tuple[str, str] | None:
    normalized = _normalize_posix_path(value)
    if not normalized.startswith("/"):
        return None
    for key, root in roots.items():
        if normalized == root or normalized.startswith(f"{root.rstrip('/')}/"):
            return key, root
    return None


def _normalize_posix_path(value: str) -> str:
    stripped = value.strip()
    if not stripped.startswith("/"):
        return stripped
    return posixpath.normpath(stripped)


def _resolve_compose_value(value: str, env: Mapping[str, str]) -> str:
    return _resolve_compose_value_once(value, env, depth=0)


def _resolve_compose_value_once(value: str, env: Mapping[str, str], *, depth: int) -> str:
    _check_compose_interpolation_bounds(value, depth=depth)
    parts: list[str] = []
    cursor = 0
    for token in _compose_dollar_run_tokens(value):
        parts.append(value[cursor : token.start])
        parts.append(token.literal_dollars)
        if token.occurrence is not None:
            parts.append(_resolve_compose_occurrence(token.occurrence, env, depth=depth))
        cursor = token.end
    parts.append(value[cursor:])
    return "".join(parts)


def _resolve_compose_occurrence(
    occurrence: ComposeInterpolationOccurrence,
    env: Mapping[str, str],
    *,
    depth: int,
) -> str:
    if occurrence.expression.startswith("${"):
        return _resolve_braced_compose_expression(occurrence.expression, env, depth=depth)
    return env.get(occurrence.key, occurrence.expression)


def _resolve_braced_compose_expression(expression: str, env: Mapping[str, str], *, depth: int) -> str:
    _check_compose_interpolation_bounds(expression, depth=depth)
    payload = expression[2:-1]
    parsed = _parse_compose_interpolation_payload(payload)
    if parsed is None:
        return expression
    key, operator, fallback = parsed
    current = env.get(key)
    is_set = key in env
    is_nonempty = current not in (None, "")
    if operator is None and not fallback:
        return current if is_set else expression
    if operator is None:
        return expression
    if operator == ":-":
        return current if is_nonempty else _resolve_compose_value_once(fallback, env, depth=depth + 1)
    if operator == "-":
        return current if is_set else _resolve_compose_value_once(fallback, env, depth=depth + 1)
    if operator == ":+":
        return _resolve_compose_value_once(fallback, env, depth=depth + 1) if is_nonempty else ""
    if operator == "+":
        return _resolve_compose_value_once(fallback, env, depth=depth + 1) if is_set else ""
    if operator == ":?":
        return current if is_nonempty else expression
    if operator == "?":
        return current if is_set else expression
    return current if is_nonempty else expression


def _command_list(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(item) for item in command]
    if isinstance(command, str):
        return command.split()
    return []


def _port_is_loopback(port: Any) -> bool:
    if isinstance(port, dict):
        host_ip = str(port.get("host_ip", ""))
        return host_ip in {"127.0.0.1", "::1", "localhost"}
    text = str(port)
    return text.startswith("127.0.0.1:") or text.startswith("[::1]:") or text.startswith("localhost:")


def _run_command(args: Sequence[str]) -> CommandResult:
    return _run_command_with_timeout(args, timeout_seconds=60)


def _run_docker_smoke_command(args: Sequence[str]) -> CommandResult:
    command = tuple(args)
    timeout_seconds = 1800 if command[:2] == ("docker", "build") else 120
    return _run_command_with_timeout(args, timeout_seconds=timeout_seconds)


def _run_command_with_timeout(args: Sequence[str], *, timeout_seconds: int) -> CommandResult:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(tuple(args), 127, "", str(error))
    return CommandResult(tuple(args), completed.returncode, completed.stdout, completed.stderr)


def _approved_preflight_tmpdir(repo_root: Path) -> tuple[Path, dict[str, Any] | None]:
    configured = os.getenv("TMPDIR")
    candidate = Path(configured) if configured else repo_root / "artifacts" / "tmp"
    try:
        return ensure_approved_evidence_root(candidate, repo_root), None
    except ValueError as error:
        return (
            _resolve_path(candidate, repo_root),
            {
                "code": "TMPDIR_OUTSIDE_APPROVED_ROOT",
                "path": str(_resolve_path(candidate, repo_root)),
                "message": str(error),
            },
        )


@contextlib.contextmanager
def _temporary_tmpdir_env(tmpdir: Path) -> Iterator[None]:
    previous = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(tmpdir)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous


def _skipped_preflight_commands(reason: str) -> dict[str, CommandResult]:
    return {
        name: CommandResult(tuple(command), 125, "", reason)
        for name, command in PREFLIGHT_COMMANDS
    }


def _disk_usage(path: Path) -> DiskSpace:
    existing = path
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    usage = shutil.disk_usage(existing)
    return DiskSpace(total=usage.total, used=usage.used, free=usage.free)


def _parse_docker_root(raw_stdout: str) -> str | None:
    raw = raw_stdout.strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw.strip('"')
    if isinstance(value, str) and value:
        return value
    return None


def _resolve_path(path: Path, repo_root: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def _resolve_output_path(path: Path, repo_root: Path) -> Path:
    candidate = path if path.is_absolute() else repo_root / path
    return candidate.parent.resolve() / candidate.name


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate M22 two-node Docker compose/env boundaries.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)

    static_parser = subparsers.add_parser("static", help="Run static compose/env checks.")
    static_parser.add_argument("--compute-compose", type=Path, default=Path("infra/compose.compute.yml"))
    static_parser.add_argument("--display-compose", type=Path, default=Path("infra/compose.display.yml"))
    static_parser.add_argument("--compute-env", type=Path, default=Path("infra/env/compute.example"))
    static_parser.add_argument("--display-env", type=Path, default=Path("infra/env/display.example"))
    static_parser.add_argument("--report", type=Path, default=DEFAULT_STATIC_REPORT)
    static_parser.add_argument(
        "--evidence-run-id",
        help="Current final E2E evidence run id. Defaults to the segment after artifacts/two-node-e2e/ when present.",
    )

    preflight_parser = subparsers.add_parser("preflight", help="Record Docker disk/cache preflight evidence.")
    preflight_parser.add_argument("--evidence-root", type=Path, default=DEFAULT_PREFLIGHT_ROOT)
    preflight_parser.add_argument(
        "--evidence-run-id",
        help="Current final E2E evidence run id. Defaults to the segment after artifacts/two-node-e2e/ when present.",
    )
    preflight_parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB)

    smoke_parser = subparsers.add_parser("smoke", help="Build and smoke-test the default app Docker image.")
    smoke_parser.add_argument("--evidence-root", type=Path, default=DEFAULT_DOCKER_SMOKE_ROOT)
    smoke_parser.add_argument(
        "--evidence-run-id",
        help="Current final E2E evidence run id. Defaults to the segment after artifacts/two-node-e2e/ when present.",
    )
    smoke_parser.add_argument("--image-tag", default=DEFAULT_SMOKE_IMAGE)
    smoke_parser.add_argument("--dockerfile", type=Path, default=DEFAULT_APP_DOCKERFILE)
    smoke_parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB)

    summary_parser = subparsers.add_parser(
        "security-summary",
        help="Aggregate Docker security evidence for final gate.",
    )
    summary_parser.add_argument("--output", type=Path, default=DEFAULT_DOCKER_SECURITY_SUMMARY)
    summary_parser.add_argument("--evidence-run-id", required=True)
    summary_parser.add_argument(
        "--source-trust-report",
        type=Path,
        action="append",
        default=None,
        help="Source-trust report path. Repeat for role-scoped compute/display reports.",
    )
    summary_parser.add_argument("--static-report", type=Path, default=DEFAULT_STATIC_REPORT)
    summary_parser.add_argument(
        "--smoke-report",
        type=Path,
        default=DEFAULT_DOCKER_SMOKE_ROOT / "docker-smoke.json",
    )
    return parser.parse_args(argv)


def _static_setup_failure_result(error: Exception) -> StaticCheckResult:
    redacted_error = _redact_static_output_text(str(error))
    return StaticCheckResult(
        status="FAIL",
        findings=(
            Finding(
                "STATIC_VALIDATION_SETUP_FAILED",
                "static validation failed before normal findings were produced.",
                details={"error_type": type(error).__name__, "error": redacted_error},
            ),
        ),
    )


def _redact_static_output_text(value: str) -> str:
    return _redact_detail_text(value, secret_context=True, field_name="error")


def _run_static_command(args: argparse.Namespace, repo_root: Path) -> int:
    try:
        result = run_static_check(
            compute_compose=args.compute_compose,
            display_compose=args.display_compose,
            compute_env=args.compute_env,
            display_env=args.display_env,
            repo_root=repo_root,
        )
    except Exception as error:
        result = _static_setup_failure_result(error)
        report_path = write_static_report(result, args.report, repo_root, evidence_run_id=args.evidence_run_id)
        redacted_error = _redact_static_output_text(str(error))
        print(
            json.dumps(
                {
                    "status": result.status,
                    "report": str(report_path),
                    "error": redacted_error,
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    report_path = write_static_report(result, args.report, repo_root, evidence_run_id=args.evidence_run_id)
    print(json.dumps({"status": result.status, "report": str(report_path)}, sort_keys=True))
    return 0 if result.status == "PASS" else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        if args.command == "static":
            return _run_static_command(args, repo_root)
        if args.command == "preflight":
            result = run_preflight(
                evidence_root=args.evidence_root,
                repo_root=repo_root,
                evidence_run_id=args.evidence_run_id,
                min_free_bytes=int(args.min_free_gb * 1024**3),
            )
            print(json.dumps({"status": result.status, "evidence_path": str(result.evidence_path)}, sort_keys=True))
            return 0 if result.status == "PASS" else 3
        if args.command == "smoke":
            result = run_docker_smoke(
                evidence_root=args.evidence_root,
                repo_root=repo_root,
                evidence_run_id=args.evidence_run_id,
                image_tag=args.image_tag,
                dockerfile=args.dockerfile,
                min_free_bytes=int(args.min_free_gb * 1024**3),
            )
            print(json.dumps({"status": result.status, "evidence_path": str(result.evidence_path)}, sort_keys=True))
            if result.status == "PASS":
                return 0
            if result.status == "BLOCKED":
                return 3
            return 1
        if args.command == "security-summary":
            output = write_docker_security_summary(
                output=args.output,
                repo_root=repo_root,
                evidence_run_id=args.evidence_run_id,
                source_trust_report=args.source_trust_report or list(DEFAULT_SOURCE_TRUST_REPORTS),
                static_report=args.static_report,
                smoke_report=args.smoke_report,
            )
            payload = _read_json_file(output)
            print(json.dumps({"status": payload.get("status"), "summary": str(output)}, sort_keys=True))
            if payload.get("status") == "PASS":
                return 0
            if payload.get("status") == "BLOCKED":
                return 3
            return 1
    except ValueError as error:
        print(
            json.dumps({"status": "FAIL", "error": _redact_static_output_text(str(error))}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
