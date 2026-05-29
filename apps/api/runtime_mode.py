from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ServiceRole(StrEnum):
    DEV_MONOLITH = "dev_monolith"
    COMPUTE_CONTROL = "compute_control"
    DISPLAY_READONLY = "display_readonly"
    SLURM_GATEWAY = "slurm_gateway"


PRODUCTION_AUTH_MODES = frozenset({"production", "live", "live_idp"})
LIVE_AUTH_BACKENDS = frozenset({"live", "live_idp", "oidc", "saml"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
_DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS = (
    "WORKSPACE_ROOT",
    "RUN_WORKSPACE_ROOT",
    "SHARED_LOG_ROOT",
    "OBJECT_STORE_ROOT",
    "NHMS_BASINS_ROOT",
    "NHMS_MODEL_ASSET_ROOT",
    "SLURM_GATEWAY_TEMPLATE_DIR",
    "SLURM_GATEWAY_WORKSPACE_DIR",
    "MUNGE_SOCKET",
    "MUNGE_KEY",
    "SHUD_EXECUTABLE",
    "DOCKER_HOST",
)


@dataclass(frozen=True)
class RuntimeModeError(RuntimeError):
    code: str
    message: str
    details: dict[str, Any]

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class DisplayBoundaryBlocker:
    code: str
    env_var: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "env_var": self.env_var, "message": self.message}


@dataclass(frozen=True)
class RuntimeConfig:
    service_role: ServiceRole
    service_role_explicit: bool
    require_service_role: bool
    auth_mode: str | None
    production_like: bool

    @property
    def control_mutations_enabled(self) -> bool:
        return self.service_role in {ServiceRole.DEV_MONOLITH, ServiceRole.COMPUTE_CONTROL}

    @property
    def slurm_routes_enabled(self) -> bool:
        return self.service_role in {ServiceRole.DEV_MONOLITH, ServiceRole.COMPUTE_CONTROL}

    @property
    def display_readonly(self) -> bool:
        return self.service_role == ServiceRole.DISPLAY_READONLY

    @property
    def queue_depth_mode(self) -> str:
        if self.display_readonly:
            return "display_readonly_unavailable"
        return "slurm_gateway"

    def public_dict(self) -> dict[str, Any]:
        return {
            "service_role": self.service_role.value,
            "control_mutations_enabled": self.control_mutations_enabled,
            "slurm_routes_enabled": self.slurm_routes_enabled,
            "queue_depth_mode": self.queue_depth_mode,
            "display_readonly": self.display_readonly,
        }


def load_runtime_config(env: Mapping[str, str] | None = None) -> RuntimeConfig:
    source_env = os.environ if env is None else env
    raw_role = source_env.get("NHMS_SERVICE_ROLE", "").strip().lower()
    require_service_role = _parse_bool_env(source_env, "NHMS_REQUIRE_SERVICE_ROLE")
    auth_mode = source_env.get("NHMS_AUTH_MODE", "").strip().lower() or None
    auth_backend = source_env.get("AUTH_BACKEND", "").strip().lower()
    live_auth_backend = auth_backend in LIVE_AUTH_BACKENDS
    production_like = require_service_role or auth_mode in PRODUCTION_AUTH_MODES or live_auth_backend

    if not raw_role:
        if production_like:
            raise RuntimeModeError(
                code="SERVICE_ROLE_REQUIRED",
                message="NHMS_SERVICE_ROLE is required for production-like API startup.",
                details={
                    "env_var": "NHMS_SERVICE_ROLE",
                    "require_service_role": require_service_role,
                    "auth_mode": auth_mode,
                    "live_auth_backend": live_auth_backend,
                    "supported_service_roles": [role.value for role in ServiceRole],
                },
            )
        role = ServiceRole.DEV_MONOLITH
        explicit = False
    else:
        try:
            role = ServiceRole(raw_role)
        except ValueError as error:
            raise RuntimeModeError(
                code="SERVICE_ROLE_UNSUPPORTED",
                message="NHMS_SERVICE_ROLE is not supported.",
                details={
                    "env_var": "NHMS_SERVICE_ROLE",
                    "service_role": raw_role,
                    "supported_service_roles": [role.value for role in ServiceRole],
                },
            ) from error
        explicit = True

    if role == ServiceRole.SLURM_GATEWAY:
        raise RuntimeModeError(
            code="SERVICE_ROLE_RESERVED",
            message="NHMS_SERVICE_ROLE=slurm_gateway is reserved and cannot start the full API.",
            details={
                "service_role": role.value,
                "bounded_gateway_app": False,
            },
        )

    if role == ServiceRole.DISPLAY_READONLY:
        blockers = display_boundary_blockers(source_env)
        if blockers:
            raise RuntimeModeError(
                code="DISPLAY_BOUNDARY_CONFIG_UNSAFE",
                message="display_readonly startup is blocked by compute-control configuration.",
                details={"service_role": role.value, "blockers": [blocker.to_dict() for blocker in blockers]},
            )

    return RuntimeConfig(
        service_role=role,
        service_role_explicit=explicit,
        require_service_role=require_service_role,
        auth_mode=auth_mode,
        production_like=production_like,
    )


def display_boundary_blockers(env: Mapping[str, str]) -> tuple[DisplayBoundaryBlocker, ...]:
    blockers: list[DisplayBoundaryBlocker] = []
    if "SLURM_GATEWAY_URL" in env:
        blockers.append(
            DisplayBoundaryBlocker(
                code="DISPLAY_SLURM_GATEWAY_URL_FORBIDDEN",
                env_var="SLURM_GATEWAY_URL",
                message="display_readonly must not configure a Slurm gateway URL.",
            )
        )
    if "SLURM_GATEWAY_BACKEND" in env:
        blockers.append(
            DisplayBoundaryBlocker(
                code="DISPLAY_SLURM_BACKEND_FORBIDDEN",
                env_var="SLURM_GATEWAY_BACKEND",
                message="display_readonly must not configure a Slurm gateway backend.",
            )
        )
    for env_var in _DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS:
        if env_var in env:
            blockers.append(
                DisplayBoundaryBlocker(
                    code="DISPLAY_COMPUTE_PATH_FORBIDDEN",
                    env_var=env_var,
                    message=f"display_readonly must not configure compute-only path env {env_var}.",
                )
            )
    display_disable_mutations = env.get("NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS")
    if display_disable_mutations is not None and _falsy(display_disable_mutations):
        blockers.append(
            DisplayBoundaryBlocker(
                code="DISPLAY_CONTROL_MUTATIONS_FORBIDDEN",
                env_var="NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS",
                message="display_readonly cannot opt into control mutations.",
            )
        )
    return tuple(blockers)


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _falsy(value: str) -> bool:
    return value.strip().lower() in _FALSY


def _parse_bool_env(env: Mapping[str, str], key: str) -> bool:
    raw_value = env.get(key)
    if raw_value is None:
        return False
    value = raw_value.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise RuntimeModeError(
        code="SERVICE_ROLE_REQUIRE_FLAG_INVALID",
        message=f"{key} must be a recognized boolean value.",
        details={
            "env_var": key,
            "accepted_truthy_values": sorted(_TRUTHY),
            "accepted_falsy_values": sorted(_FALSY),
        },
    )
