from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common.redaction import redact_payload
from packages.common.shud_preflight import check_shud_executable
from packages.common.slurm_env import (
    reserved_slurm_env_reason,
    secret_bearing_url_reason,
    secret_manifest_key_reason,
)
from services.orchestrator.chain import ForecastOrchestrator
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES

MAX_SLURM_ENV_VALUE_LENGTH = 1024
SLURM_ARRAY_STAGE_NAMES = {"forcing", "forecast", "parse", "state_save_qc", "frequency"}
SAFE_SLURM_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SAFE_SLURM_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:=,@+\-]*$")
SHELL_META_RE = re.compile(r"[;|&$`<>\n\r]")
PRODUCTION_SLURM_ENV_PASSTHROUGH_KEYS = (
    "GFS_NOMADS_BASE_URL",
    "GFS_FORECAST_START_HOUR",
    "GFS_FORECAST_END_HOUR",
    "GFS_FORECAST_STEP_HOURS",
    "GFS_FORECAST_RESOLUTION_SEGMENTS",
    "IFS_OPEN_DATA_SOURCE",
    "IFS_OPEN_DATA_FALLBACK_SOURCES",
    "IFS_FORECAST_START_HOUR",
    "IFS_FORECAST_END_HOUR",
    "IFS_FORECAST_STEP_HOURS",
    "IFS_FORECAST_RESOLUTION_SEGMENTS",
    "IFS_SOURCE_COOLDOWN_SECONDS",
    "IFS_DOWNLOAD_CHUNK_SIZE_BYTES",
    "IFS_MAX_FILE_SIZE_BYTES",
    "NHMS_DOWNLOAD_BBOX_SOUTH",
    "NHMS_DOWNLOAD_BBOX_NORTH",
    "NHMS_DOWNLOAD_BBOX_WEST",
    "NHMS_DOWNLOAD_BBOX_EAST",
    "NHMS_GRIB_ENV_ROOT",
    "NHMS_GRIB_SYSTEM_ECCODES",
)
LOCALHOST_NAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "::",
}
DATABASE_HOST_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_GATEWAY_SELF_HOSTS = frozenset(
    {"localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0", "::", "ip6-localhost", "ip6-loopback"}
)
_GATEWAY_REQUIRED_BINARIES = ("sbatch", "squeue", "sacct", "scancel")


def _scheduler_shud_executable(config: Any) -> str:
    env_value = config.slurm_env.get("SHUD_EXECUTABLE") if isinstance(config.slurm_env, Mapping) else None
    return str(env_value or os.getenv("SHUD_EXECUTABLE") or "").strip()


def _slurm_shud_executable_check(
    config: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the SHUD executable before any Slurm submission.

    A stub or missing executable produces a typed, redacted blocker that gates
    the pass at ``slurm_preflight_blocked`` so the scheduler never calls the
    orchestrator / Slurm gateway, never marks an active job, and never records a
    hydro success state.
    """

    executable = _scheduler_shud_executable(config)
    result = check_shud_executable(executable)
    blockers = [
        {
            "code": str(blocker.get("error_code") or "SHUD_EXECUTABLE_PREFLIGHT_FAILED"),
            "field": "SHUD_EXECUTABLE",
            "message": str(blocker.get("message") or "SHUD executable preflight failed."),
            **({"library": blocker["library"]} if blocker.get("library") is not None else {}),
            **({"executable": blocker["executable"]} if blocker.get("executable") is not None else {}),
        }
        for blocker in result.blockers
    ]
    return redact_payload(dict(result.checks)), blockers


def _gateway_endpoint(url: str) -> tuple[str | None, int | None]:
    """Return ``(host, port)`` for a gateway URL without leaking credentials.

    Only the host and port are returned; any ``user:pass@`` userinfo is dropped
    so it never reaches evidence.
    """

    candidate = (url or "").strip()
    if not candidate:
        return None, None
    try:
        parsed = urlparse(candidate)
        # A bare ``host:port`` (e.g. ``localhost:8000``) has no ``//`` authority,
        # so urlparse mis-reads the host as the scheme and yields hostname=None.
        # Re-parse with an explicit ``//`` authority in that case. ``_has_uri_scheme``
        # cannot distinguish ``localhost:`` from a real ``http:`` scheme, so rely on
        # the parse result instead.
        if parsed.hostname is None and "//" not in candidate:
            parsed = urlparse(f"//{candidate}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None, None
    return (host.lower() if host else None), port


def _gateway_self_reference_blocker(
    url: str, service_port: int, *, backend: str
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Pure config comparison (no network) for self-referential gateway URLs.

    Rule: a gateway URL is an *invalid* self-reference when (a) a real Slurm
    backend is configured, and (b) its host is a loopback / unspecified /
    localhost name, and (c) its (defaulted) port equals this service's own
    control-API listen port. That combination means the "gateway" points back at
    the orchestrator's own API instead of a real Slurm gateway -> a guaranteed
    loop that can never submit.

    The mock / co-located dev gateway convention (``http://localhost:8000`` with
    the mock backend) is intentionally NOT flagged, so this never fences a
    submittable dev/test run (never-break-userspace).
    """

    host, port = _gateway_endpoint(url)
    effective_port = port if port is not None else service_port
    endpoint = {"host": host, "port": effective_port}
    if host is None:
        return None, endpoint
    if backend not in {"real", "slurm"}:
        return None, endpoint
    is_loopback_name = host in _GATEWAY_SELF_HOSTS
    if not is_loopback_name:
        address = _database_host_ip_address(host)
        is_loopback_name = address is not None and (address.is_loopback or address.is_unspecified)
    if is_loopback_name and effective_port == service_port:
        return (
            {
                "code": "SLURM_GATEWAY_SELF_REFERENCE",
                "field": "SLURM_GATEWAY_URL",
                "message": (
                    "Slurm gateway URL resolves to this service's own listen address; "
                    "configure a real node-22 Slurm gateway endpoint."
                ),
                "host": host,
                "port": effective_port,
            },
            endpoint,
        )
    return None, endpoint


def _interpret_gateway_health(payload: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    """Map a gateway ``/api/v1/slurm/health`` body to a probe verdict.

    Prefers the Lane 1 structure (top-level ``healthy: bool`` + per-binary
    ``binaries[name].executable``): any required binary that is not executable
    flips the verdict to unhealthy. Falls back to the legacy ``status`` field
    (``healthy``/``ok``) when the new fields are absent, so a gateway still on
    the old shape is never wrongly fenced or wrongly passed.
    """

    binaries = payload.get("binaries")
    reasons: list[str] = []
    if isinstance(binaries, Mapping) and binaries:
        missing = [
            name for name in _GATEWAY_REQUIRED_BINARIES if not bool((binaries.get(name) or {}).get("executable"))
        ]
        binaries_ok = not missing
        if missing:
            reasons.append("missing/non-executable Slurm binaries: " + ", ".join(missing))
    else:
        # Legacy gateway without per-binary probes: cannot prove binaries.
        binaries_ok = None

    if "healthy" in payload:
        top_healthy = bool(payload.get("healthy"))
    else:
        status = str(payload.get("status", "") or "").lower()
        top_healthy = status in {"healthy", "ok"}
    if not top_healthy:
        reason = str(payload.get("error", "") or "").strip()
        reasons.append(reason or "gateway reported unhealthy")

    if binaries_ok is None:
        is_healthy = top_healthy
    else:
        is_healthy = top_healthy and binaries_ok

    return {
        "mode": mode,
        "backend": str(payload.get("backend", mode) or mode),
        "version": str(payload.get("version", "") or ""),
        "healthy": is_healthy,
        "submit_capable": is_healthy,
        "accounting_available": is_healthy,
        "reason": ("; ".join(reasons) or None) if not is_healthy else None,
    }


def _in_process_gateway_probe(mode: str) -> dict[str, Any]:
    """In-process health for the co-located mock/dev gateway convention.

    The mock backend runs in-process (no HTTP server to probe), so the dev
    convention reads ``create_gateway().health()`` directly. This keeps a
    submittable dev/test run from ever being fenced (never-break-userspace);
    the HTTP probe path is reserved for a real node-22 gateway deployment.
    """

    from services.slurm_gateway.config import SlurmGatewaySettings
    from services.slurm_gateway.gateway import create_gateway

    try:
        gateway = create_gateway(SlurmGatewaySettings())
        health = gateway.health()
        return _interpret_gateway_health(
            {
                "backend": getattr(health, "backend", mode),
                "version": getattr(health, "version", ""),
                "status": getattr(health, "status", ""),
                "healthy": getattr(health, "healthy", None),
                "error": getattr(health, "error", "") or "",
                "binaries": {
                    name: {
                        "resolved": getattr(probe, "resolved", False),
                        "executable": getattr(probe, "executable", False),
                    }
                    for name, probe in (getattr(health, "binaries", {}) or {}).items()
                },
            },
            mode=mode,
        )
    except Exception as error:  # noqa: BLE001 - probe must be fail-safe, never raise.
        return {
            "mode": mode,
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": str(redact_payload(str(error))),
        }


def _scheduler_grib_env_root(config: Any) -> str:
    env_value = config.slurm_env.get("NHMS_GRIB_ENV_ROOT") if isinstance(config.slurm_env, Mapping) else None
    return str(env_value or os.getenv("NHMS_GRIB_ENV_ROOT") or "").strip()


def _default_grib_system_eccodes_probe(
    config: Any,
) -> Mapping[str, Any]:
    """Model whether compute nodes ship system cdo+libeccodes.

    The control node cannot probe a compute node's shared libraries without a
    job, so absent an explicit operator assertion to the contrary we do NOT
    fence a submission: node-eccodes-availability DEFAULTS to available. Set
    ``NHMS_GRIB_SYSTEM_ECCODES=false`` on a partition whose nodes lack system
    cdo/eccodes when you intentionally leave ``NHMS_GRIB_ENV_ROOT`` empty to
    restore the fail-loud (an empty root skips PATH injection, which only breaks
    GRIB at runtime where the node genuinely lacks eccodes).
    """

    asserted = config.slurm_env.get("NHMS_GRIB_SYSTEM_ECCODES") if isinstance(config.slurm_env, Mapping) else None
    asserted = str(asserted or os.getenv("NHMS_GRIB_SYSTEM_ECCODES") or "").strip()
    if asserted.lower() in {"0", "false", "no"}:
        return {
            "system_eccodes_available": False,
            "reason": "operator asserted compute nodes lack system cdo/eccodes",
        }
    # Truthy ("1"/"true"/"yes") OR unset/empty/unknown -> available (no fence).
    return {"system_eccodes_available": True}


def _slurm_grib_env_check(
    config: Any,
    *,
    probe: Callable[[Any], Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fail loud when GRIB tooling will be unavailable on the compute node.

    Compute nodes lack system cdo/libeccodes; GRIB clip/read only works when
    ``NHMS_GRIB_ENV_ROOT`` (a shared conda env with bin/ + lib/) is injected into
    the sbatch PATH/LD_LIBRARY_PATH. That injection is truthiness-only and never
    validates the directory, so a typo'd root silently injects a broken PATH and
    an empty root silently skips injection -- both break GRIB at runtime with no
    fail-loud (this bit us in #291). Two gates, both required by spec §4.5:

    1. **Root SET:** ``<root>/bin`` AND ``<root>/lib`` must both exist as dirs,
       else ``GRIB_ENV_ROOT_INVALID``.
    2. **Root UNSET/empty:** injection is skipped. The default (no assertion)
       does NOT block -- only an explicit operator assertion that nodes lack
       eccodes (``NHMS_GRIB_SYSTEM_ECCODES=false``) does. ``probe`` (injectable,
       default ``_default_grib_system_eccodes_probe``) models that; only when it
       reports unavailable -> ``GRIB_ENV_UNAVAILABLE``, else NO blocker. A probe
       that raises fails safe as BLOCKED rather than faking PASS.

    Valid root with real bin+lib, or empty root absent an operator assertion
    that nodes lack eccodes, PASS. When genuinely OK this adds NO blocker so it
    never fences a healthy run (never-break-userspace).
    """

    checks: dict[str, Any] = {}
    blockers: list[dict[str, Any]] = []

    root = _scheduler_grib_env_root(config)
    checks["root"] = root or None

    if root:
        root_path = Path(root)
        bin_ok = (root_path / "bin").is_dir()
        lib_ok = (root_path / "lib").is_dir()
        checks["bin_present"] = bin_ok
        checks["lib_present"] = lib_ok
        if not (bin_ok and lib_ok):
            blockers.append(
                {
                    "code": "GRIB_ENV_ROOT_INVALID",
                    "field": "NHMS_GRIB_ENV_ROOT",
                    "message": (
                        f"NHMS_GRIB_ENV_ROOT set but {root}/bin or {root}/lib "
                        "missing; GRIB tooling would not be injected on the "
                        "compute node."
                    ),
                    "root": root,
                    "bin_present": bin_ok,
                    "lib_present": lib_ok,
                }
            )
        return redact_payload(checks), redact_payload(blockers)

    probe_fn = probe or _default_grib_system_eccodes_probe
    try:
        result = dict(probe_fn(config))
    except Exception as error:  # noqa: BLE001 - injected probe must not break the pass.
        result = {
            "system_eccodes_available": False,
            "reason": str(redact_payload(str(error))),
        }

    available = bool(result.get("system_eccodes_available"))
    checks["system_eccodes_available"] = available
    if not available:
        blockers.append(
            {
                "code": "GRIB_ENV_UNAVAILABLE",
                "field": "NHMS_GRIB_ENV_ROOT",
                "message": (
                    "NHMS_GRIB_ENV_ROOT is empty so GRIB env injection is "
                    "skipped, and compute-node system eccodes is not confirmed; "
                    "GRIB clip/read would fail at runtime on the node."
                ),
                **({"reason": str(result["reason"])} if result.get("reason") else {}),
            }
        )

    return redact_payload(checks), redact_payload(blockers)


def _database_url_blocker(database_url: str | None) -> dict[str, Any] | None:
    if not database_url:
        return {
            "code": "SLURM_PREFLIGHT_DATABASE_URL_MISSING",
            "field": "DATABASE_URL",
            "message": "Slurm execution requires a compute-node reachable DATABASE_URL before submission.",
        }
    host = _database_host(database_url)
    if _database_host_is_unsafe(host):
        return {
            "code": "SLURM_PREFLIGHT_DATABASE_URL_UNSAFE_HOST",
            "field": "DATABASE_URL",
            "message": "Slurm execution rejects malformed or unsafe DATABASE_URL hosts.",
            "host": host,
        }
    if _database_host_is_local(host):
        return {
            "code": "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST",
            "field": "DATABASE_URL",
            "message": "Slurm execution rejects localhost-only DATABASE_URL values.",
            "host": host,
        }
    return None


def _database_host(database_url: str | None) -> str | None:
    if not database_url:
        return None
    try:
        parsed = urlparse(database_url)
    except ValueError:
        return None
    if parsed.scheme == "sqlite":
        return "localhost"
    try:
        host = parsed.hostname
        parsed.port
    except ValueError:
        return None
    return host


def _database_host_is_local(host: str | None) -> bool:
    if host is None:
        return True
    normalized = _normalize_database_host(host)
    if normalized in LOCALHOST_NAMES:
        return True
    if normalized.endswith(".localhost"):
        return True
    address = _database_host_ip_address(normalized)
    if address is None:
        return False
    return address.is_loopback or address.is_unspecified


def _database_host_is_unsafe(host: str | None) -> bool:
    if host is None:
        return True
    normalized = _normalize_database_host(host)
    if not normalized:
        return True
    if DATABASE_HOST_ALLOWED_RE.fullmatch(normalized) is None:
        return True
    address = _database_host_ip_address(normalized)
    if address is not None and address.is_link_local:
        return True
    if ":" in normalized:
        if address is None:
            return True
    return _is_unsafe_numeric_ipv4_like_host(normalized)


def _normalize_database_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized.rstrip(".")


def _database_host_ip_address(host: str) -> Any | None:
    try:
        return ip_address(host)
    except ValueError:
        return _parse_noncanonical_ipv4_address(host)


def _parse_noncanonical_ipv4_address(host: str) -> IPv4Address | None:
    if not _is_noncanonical_numeric_ipv4_host(host):
        return None
    parts = host.split(".")
    values: list[int] = []
    for part in parts:
        if part == "":
            return None
        try:
            values.append(int(part, 0))
        except ValueError:
            return None
    if len(values) == 1:
        value = values[0]
    elif len(values) == 2:
        value = (values[0] << 24) | values[1]
    elif len(values) == 3:
        value = (values[0] << 24) | (values[1] << 16) | values[2]
    elif len(values) == 4:
        value = (values[0] << 24) | (values[1] << 16) | (values[2] << 8) | values[3]
    else:
        return None
    if value < 0 or value > 0xFFFFFFFF:
        return None
    return IPv4Address(value)


def _is_noncanonical_numeric_ipv4_host(host: str) -> bool:
    if not host:
        return False
    if not _is_numeric_ipv4_like_host(host):
        return False
    parts = host.split(".")
    return len(parts) != 4 or any(_is_noncanonical_ipv4_part(part) for part in parts)


def _is_numeric_ipv4_like_host(host: str) -> bool:
    parts = host.split(".")
    return all(_is_ipv4_number_part(part) for part in parts)


def _is_ipv4_number_part(part: str) -> bool:
    if part == "":
        return False
    if part.lower().startswith("0x"):
        return len(part) > 2 and all(character in "0123456789abcdefABCDEF" for character in part[2:])
    return part.isdigit()


def _is_noncanonical_ipv4_part(part: str) -> bool:
    if part == "":
        return True
    if part.lower().startswith("0x"):
        return True
    return len(part) > 1 and part.startswith("0")


def _is_unsafe_numeric_ipv4_like_host(host: str) -> bool:
    if not _is_numeric_ipv4_like_host(host):
        return False
    return _database_host_ip_address(host) is None


def _preflight_allowed_roots(config: Any) -> tuple[Path, ...]:
    roots = list(config.allowed_storage_roots) or [Path(config.workspace_root)]
    resolved: list[Path] = []
    for root in roots:
        candidate = root.expanduser().resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved)


def _storage_root_check(
    field_name: str,
    value: Path | str | None,
    allowed_roots: Sequence[Path],
    *,
    evidence_safe_paths: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    evidence_path = "[local-path]" if evidence_safe_paths else None
    if value in (None, ""):
        return (
            {
                "configured": False,
                "path": None,
                "contained": False,
                "compute_node_visible": False,
            },
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_MISSING",
                "field": field_name,
                "message": f"Slurm execution requires configured {field_name}.",
            },
        )
    path = Path(value).expanduser()
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        check = {
            "configured": True,
            "path": evidence_path or str(path),
            "contained": False,
            "compute_node_visible": False,
        }
        return (
            check,
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_UNSAFE_PATH",
                "field": field_name,
                "path": evidence_path or str(path),
                "message": f"Slurm {field_name} must be a safe compute-node visible directory.",
            },
        )
    visible = path.exists() and path.is_dir()
    contained = _path_is_under_any(resolved, allowed_roots)
    check = {
        "configured": True,
        "path": evidence_path or str(resolved),
        "contained": contained,
        "compute_node_visible": visible,
    }
    if not contained:
        return (
            check,
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_OUT_OF_ROOT",
                "field": field_name,
                "path": evidence_path or str(resolved),
                "message": f"Slurm {field_name} must stay under configured project or production roots.",
            },
        )
    if not visible:
        return (
            check,
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_NOT_VISIBLE",
                "field": field_name,
                "path": evidence_path or str(resolved),
                "message": f"Slurm {field_name} must exist as a compute-node visible directory.",
            },
        )
    return check, None


def _path_is_under_any(path: Path, allowed_roots: Sequence[Path]) -> bool:
    for root in allowed_roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _slurm_template_allowlist_check(config: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    templates = dict(config.slurm_job_type_templates or {})
    blockers: list[dict[str, Any]] = []
    expected_by_stage = {stage.stage: stage.job_type for stage in ForecastOrchestrator.stages}
    allowed_names = set(DEFAULT_JOB_TYPE_TEMPLATES.values())
    checks: dict[str, Any] = {}
    for stage_name, job_type in expected_by_stage.items():
        template_name = templates.get(job_type)
        check = {
            "job_type": job_type,
            "template_name": template_name,
            "allowlisted": template_name in allowed_names,
            "array_capable": stage_name in SLURM_ARRAY_STAGE_NAMES,
        }
        checks[stage_name] = check
        if template_name not in allowed_names:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_TEMPLATE_NOT_ALLOWLISTED",
                    "field": f"slurm_job_type_templates.{job_type}",
                    "stage": stage_name,
                    "job_type": job_type,
                    "template_name": template_name,
                    "message": f"Slurm stage {stage_name} must use an allowlisted sbatch template.",
                }
            )
        expected_template = DEFAULT_JOB_TYPE_TEMPLATES.get(job_type)
        if template_name in allowed_names and template_name != expected_template:
            check["expected_template_name"] = expected_template
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_TEMPLATE_MISMATCH",
                    "field": f"slurm_job_type_templates.{job_type}",
                    "stage": stage_name,
                    "job_type": job_type,
                    "template_name": template_name,
                    "expected_template_name": expected_template,
                    "message": f"Slurm stage {stage_name} must use the template assigned to its job type.",
                }
            )
        if stage_name in SLURM_ARRAY_STAGE_NAMES and not str(template_name or "").endswith("_array.sbatch"):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ARRAY_TEMPLATE_REQUIRED",
                    "field": f"slurm_job_type_templates.{job_type}",
                    "stage": stage_name,
                    "job_type": job_type,
                    "template_name": template_name,
                    "message": f"Slurm stage {stage_name} requires an array-capable template.",
                }
            )
    return {"stage_templates": checks}, blockers


def _slurm_env_check(env: Mapping[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    sanitized: dict[str, str] = {}
    for key, value in env.items():
        key_text = str(key)
        value_text = str(value)
        key_secret_reason = secret_manifest_key_reason(key_text)
        if key_secret_reason is not None:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_SECRET_REJECTED",
                    "field": "slurm_env.[redacted]",
                    "reason": key_secret_reason,
                    "message": "Slurm scheduler evidence and exports reject secret-shaped environment keys.",
                }
            )
            sanitized["[redacted]"] = "[redacted]"
            continue
        if not SAFE_SLURM_ENV_KEY_RE.fullmatch(key_text):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_KEY_UNSAFE",
                    "field": f"slurm_env.{key_text}",
                    "message": "Slurm exported environment keys must be uppercase shell identifiers.",
                }
            )
            continue
        reserved_reason = reserved_slurm_env_reason(key_text)
        if reserved_reason is not None:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_RESERVED_REJECTED",
                    "field": f"slurm_env.{key_text}",
                    "reason": reserved_reason,
                    "message": "Slurm exported environment cannot override reserved runtime variables.",
                }
            )
            sanitized[key_text] = "[reserved]"
            continue
        if len(value_text) > MAX_SLURM_ENV_VALUE_LENGTH:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_VALUE_TOO_LONG",
                    "field": f"slurm_env.{key_text}",
                    "max_length": MAX_SLURM_ENV_VALUE_LENGTH,
                    "message": "Slurm exported environment values must be bounded.",
                }
            )
            sanitized[key_text] = value_text[:64] + "...[truncated]"
            continue
        secret_url_reason = secret_bearing_url_reason(value_text)
        if secret_url_reason is not None:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_SECRET_REJECTED",
                    "field": f"slurm_env.{key_text}",
                    "reason": secret_url_reason,
                    "message": (
                        "Slurm exported environment values must not contain URL credentials or secret query parameters."
                    ),
                }
            )
            sanitized[key_text] = "[redacted]"
            continue
        if SHELL_META_RE.search(value_text) or not SAFE_SLURM_ENV_VALUE_RE.fullmatch(value_text):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_VALUE_UNSAFE",
                    "field": f"slurm_env.{key_text}",
                    "message": "Slurm exported environment values must be shell-safe.",
                }
            )
            sanitized[key_text] = "[unsafe]"
            continue
        sanitized[key_text] = value_text
    return {"count": len(env), "sanitized": sanitized}, blockers


def _production_slurm_env(explicit_env: Mapping[str, Any]) -> dict[str, str]:
    env = {str(key): str(value) for key, value in dict(explicit_env).items()}
    for key in PRODUCTION_SLURM_ENV_PASSTHROUGH_KEYS:
        if key in env:
            continue
        value = os.getenv(key)
        if value not in (None, ""):
            env[key] = str(value)
    return env
