from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from packages.common.redaction import redact_payload


def _slurm_preflight(config: Any) -> dict[str, Any]:
    if not config.slurm_execution_enabled:
        return {
            "status": "not_required",
            "enabled": False,
            "blockers": [],
            "checks": {},
        }

    blockers: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    # The Slurm preflight never consults DATABASE_URL: the scheduler runs DB-free
    # only, and a configured one is refused before the lock by the DB-free runtime
    # preflight (database_url_forbidden).
    checks["database"] = {
        "configured": bool(getattr(config, "database_url_configured", False)),
        "required": False,
        "db_free_runtime": True,
        "compute_node_reachable": "not_required",
    }

    roots = {
        "workspace_root": config.workspace_root,
        "object_store_root": config.object_store_root,
        "log_root": config.log_root,
        "runtime_root": config.runtime_root,
    }
    # The allowed roots are adjudicated before the per-root loop: every root below
    # is judged against that product. The adjudication admits an unresolvable root
    # lexically, so its blocker list is empty; it is still extended first so any
    # blocker it carried would lead the per-root verdicts it causes.
    allowed_roots, allowed_root_blockers = _scheduler._preflight_allowed_roots(config)
    blockers.extend(allowed_root_blockers)
    root_checks: dict[str, Any] = {}
    for field_name, value in roots.items():
        root_check, blocker = _scheduler._storage_root_check(
            field_name,
            value,
            allowed_roots,
            evidence_safe_paths=True,
        )
        root_checks[field_name] = root_check
        if blocker is not None:
            blockers.append(blocker)
    checks["storage_roots"] = root_checks
    checks["allowed_roots"] = ["[local-path]" for _root in allowed_roots]

    template_check, template_blockers = _scheduler._slurm_template_allowlist_check(config)
    checks["templates"] = template_check
    blockers.extend(template_blockers)

    env_check, env_blockers = _scheduler._slurm_env_check(config.slurm_env, evidence_safe_values=True)
    checks["environment"] = env_check
    blockers.extend(env_blockers)

    shud_check, shud_blockers = _scheduler._slurm_shud_executable_check(config)
    checks["shud_executable"] = shud_check
    blockers.extend(shud_blockers)

    gateway_check, gateway_blockers = _scheduler._slurm_gateway_check(config)
    checks["gateway"] = gateway_check
    blockers.extend(gateway_blockers)

    grib_check, grib_blockers = _scheduler._slurm_grib_env_check(config, evidence_safe_paths=True)
    checks["grib_env"] = grib_check
    blockers.extend(grib_blockers)

    return {
        "status": "blocked" if blockers else "ready",
        "enabled": True,
        "blockers": blockers,
        "checks": checks,
    }


_GATEWAY_SELF_HOSTS = frozenset(
    {"localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0", "::", "ip6-localhost", "ip6-loopback"}
)


def _slurm_gateway_backend() -> str:
    """Resolve the configured Slurm gateway backend without touching the network."""

    from services.slurm_gateway.config import SlurmGatewaySettings

    try:
        return str(SlurmGatewaySettings().backend or "").strip().lower()
    except Exception:  # noqa: BLE001 - config read must not break the pass.
        return ""


_GATEWAY_HEALTH_PATH = "/api/v1/slurm/health"
_GATEWAY_REQUIRED_BINARIES = ("sbatch", "squeue", "sacct", "scancel")
_GATEWAY_PROBE_TIMEOUT_SECONDS = 10.0


def _configured_service_token() -> str | None:
    """One token contract: delegated to packages.common.request_auth."""
    from packages.common.request_auth import read_configured_service_token

    return read_configured_service_token()


def _default_gateway_probe(config: Any) -> dict[str, Any]:
    """Bounded, fail-safe gateway health probe."""

    from services.slurm_gateway.config import SlurmGatewaySettings

    try:
        mode = str(SlurmGatewaySettings().backend or "")
    except Exception:  # noqa: BLE001 - config read must not break the probe.
        mode = ""

    if mode != "slurm":
        return _scheduler._in_process_gateway_probe(mode)

    base_url = str(config.slurm_gateway_url or "").strip()
    if not base_url:
        return {
            "mode": mode,
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": "SLURM_GATEWAY_URL is not configured.",
        }

    token = _configured_service_token()
    if token is None:
        return {
            "mode": mode,
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": (
                "SLURM_GATEWAY_SERVICE_TOKEN is not configured with a usable "
                "value; the gateway rejects anonymous mutations, so submit "
                "capability cannot be confirmed from anonymous health alone."
            ),
        }

    url = base_url.rstrip("/") + _GATEWAY_HEALTH_PATH
    try:
        import httpx

        with httpx.Client(timeout=_GATEWAY_PROBE_TIMEOUT_SECONDS) as client:
            response = client.get(url)
        if response.status_code // 100 != 2:
            return {
                "mode": mode,
                "healthy": False,
                "submit_capable": False,
                "accounting_available": False,
                "reason": f"gateway health returned HTTP {response.status_code}",
            }
        payload = response.json()
        if not isinstance(payload, Mapping):
            return {
                "mode": mode,
                "healthy": False,
                "submit_capable": False,
                "accounting_available": False,
                "reason": "gateway health returned a non-object body",
            }
        return _scheduler._interpret_gateway_health(payload, mode=mode, required_backend="slurm")
    except Exception as error:  # noqa: BLE001 - probe must be fail-safe, never raise.
        return {
            "mode": mode,
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": str(redact_payload(str(error))),
        }


def _slurm_gateway_check(
    config: Any,
    *,
    probe: Callable[[Any], Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the Slurm gateway of the compute role before any submission."""

    checks: dict[str, Any] = {}
    blockers: list[dict[str, Any]] = []

    backend = _scheduler._slurm_gateway_backend()
    self_blocker, endpoint = _scheduler._gateway_self_reference_blocker(
        config.slurm_gateway_url, config.service_port, backend=backend
    )
    checks["endpoint"] = endpoint
    checks["self_reference"] = self_blocker is not None
    if self_blocker is not None:
        blockers.append(self_blocker)
        return redact_payload(checks), redact_payload(blockers)

    probe_fn = probe or _scheduler._default_gateway_probe
    try:
        result = dict(probe_fn(config))
    except Exception as error:  # noqa: BLE001 - injected probe must not break the pass.
        result = {
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": str(redact_payload(str(error))),
        }

    checks["mode"] = result.get("mode")
    if result.get("backend") is not None:
        checks["backend"] = result.get("backend")
    if result.get("version") is not None:
        checks["version"] = result.get("version")
    healthy = bool(result.get("healthy"))
    submit_capable = bool(result.get("submit_capable", healthy))
    accounting_available = bool(result.get("accounting_available", healthy))
    checks["healthy"] = healthy
    checks["submit_capable"] = submit_capable
    checks["accounting_available"] = accounting_available

    if not (healthy and submit_capable and accounting_available):
        blockers.append(
            {
                "code": "SLURM_GATEWAY_UNAVAILABLE",
                "field": "SLURM_GATEWAY_URL",
                "message": (
                    "Slurm gateway is unavailable, unhealthy, or cannot confirm "
                    "submit/accounting capability before submission."
                ),
                "host": endpoint.get("host"),
                "port": endpoint.get("port"),
                **({"reason": str(result["reason"])} if result.get("reason") else {}),
            }
        )

    return redact_payload(checks), redact_payload(blockers)


from services.orchestrator import scheduler as _scheduler  # noqa: E402
