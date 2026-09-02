"""Production owner for the cold-residency target-preflight identity contract.

Split out of ``compressed_chunk_cold_runtime`` so the expected/observed
numeric-runtime-identity validation added by #1929 has one narrow home and the
movement module keeps owning only the shell-first sequence plus its catalog
read helpers. ``RuntimeConfig``, ``TargetIdentity``,
``preflight_target_identity`` and ``require_runtime_exec_identity`` are
re-exported from ``compressed_chunk_cold_runtime`` unchanged, so no existing
import site moves and no wrapper or duplicate logic is introduced.

This module holds the preflight boundary only: it reads the tablespace catalog,
invokes the #1929 inspector and compares identities. It never issues movement
SQL and never imports ``compressed_chunk_cold_probe``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from packages.common.compressed_chunk_cold_residency import (
    ALLOWED_HYPERTABLES,
    COLD_TABLESPACE_NAME,
    ColdResidencyError,
    validate_catalog_path,
)
from packages.common.compressed_chunk_cold_runtime_catalog import (
    ColdRuntimeError,
    attached_tablespaces,
    tablespace_catalog_location,
)
from packages.common.compressed_chunk_cold_runtime_timing import Clock, default_clock
from packages.common.compressed_chunk_cold_target import (
    CONTAINER_COLD_PATH,
    CONTAINER_EXEC_ID_MAX,
    CONTAINER_EXEC_ID_MIN,
    HOST_COLD_PATH,
    LIVE_CONTAINER_NAME,
    production_inspect_target,
    require_container_exec_pair,
)

DEFAULT_LOCK_TIMEOUT = "30s"
DEFAULT_STATEMENT_TIMEOUT = "3600s"
DEFAULT_MAX_MEMBERS = 64

InspectTarget = Callable[[], Mapping[str, Any]]


@dataclass(frozen=True)
class TargetIdentity:
    catalog_name: str
    catalog_location: str
    container_bind: str
    host_path: str
    device_identity: str
    container_exec_uid: int
    container_exec_gid: int


@dataclass(frozen=True)
class RuntimeConfig:
    """Expected values only; nothing here is ever reported as observed truth.

    ``expected_container_exec_uid``/``expected_container_exec_gid`` are mandatory
    (#1929): the zero defaults are *invalid* identities, so a half-configured or
    omitted pair fails closed at validation instead of silently falling back to
    an image user name, root, or a UID-only principal.
    """

    lock_timeout: str = DEFAULT_LOCK_TIMEOUT
    statement_timeout: str = DEFAULT_STATEMENT_TIMEOUT
    max_members: int = DEFAULT_MAX_MEMBERS
    expected_catalog_location: str = CONTAINER_COLD_PATH
    expected_container_bind: str = HOST_COLD_PATH
    expected_host_path: str = HOST_COLD_PATH
    expected_device_identity: str = ""
    expected_container_name: str = LIVE_CONTAINER_NAME
    expected_container_exec_uid: int = 0
    expected_container_exec_gid: int = 0
    inspect_target: InspectTarget | None = None
    clock: Clock = default_clock


def require_runtime_exec_identity(config: RuntimeConfig) -> tuple[int, int]:
    """Validate the expected numeric principal; refuses before any connection.

    #1929: the writable probe is only meaningful for a principal that is
    explicitly configured. Zero (the dataclass default), a half pair, a bool, or
    an out-of-range component can never authorize anything.
    """

    try:
        return require_container_exec_pair(
            config.expected_container_exec_uid,
            config.expected_container_exec_gid,
            uid_name="expected_container_exec_uid/NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID",
            gid_name="expected_container_exec_gid/NODE27_COLD_RESIDENCY_CONTAINER_EXEC_GID",
        )
    except ColdRuntimeError as error:
        raise ColdRuntimeError(
            str(error),
            error_class="config",
            stage="config",
        ) from error


def _observed_exec_component(observed: Mapping[str, Any], key: str) -> int:
    if key not in observed:
        raise ColdRuntimeError(
            f"target inspector did not observe {key}",
            error_class="target_identity",
            stage="target_identity",
        )
    value = observed.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ColdRuntimeError(
            f"target inspector observed a non-integral {key}",
            error_class="target_identity",
            stage="target_identity",
        )
    if not CONTAINER_EXEC_ID_MIN <= value <= CONTAINER_EXEC_ID_MAX:
        raise ColdRuntimeError(
            f"target inspector observed an out-of-range {key}",
            error_class="target_identity",
            stage="target_identity",
        )
    return int(value)


def _require_observed_exec_identity(observed: Mapping[str, Any]) -> tuple[int, int]:
    """Strictly validate the pair an inspector claims to have observed.

    Both keys must be present and integral; a missing field is never filled in
    from expected config, so an injected inspector cannot pass by echoing.
    """

    return (
        _observed_exec_component(observed, "container_exec_uid"),
        _observed_exec_component(observed, "container_exec_gid"),
    )


def preflight_target_identity(
    execute: Callable[..., list[Mapping[str, Any]]],
    config: RuntimeConfig,
    *,
    require_device_identity: bool = False,
) -> TargetIdentity:
    expected_uid, expected_gid = require_runtime_exec_identity(config)
    catalog_location = tablespace_catalog_location(execute, COLD_TABLESPACE_NAME)
    try:
        validate_catalog_path(catalog_location=catalog_location, expected_location=config.expected_catalog_location)
    except ColdResidencyError as error:
        raise ColdRuntimeError(str(error), error_class="target_identity", stage="target_identity") from error
    inspector = config.inspect_target or (
        lambda: production_inspect_target(
            expected_container_exec_uid=expected_uid,
            expected_container_exec_gid=expected_gid,
        )
    )
    observed = dict(inspector())
    observed_uid, observed_gid = _require_observed_exec_identity(observed)
    if (observed_uid, observed_gid) != (expected_uid, expected_gid):
        raise ColdRuntimeError(
            "observed container runtime identity drifted from expected numeric uid:gid",
            error_class="target_identity",
            stage="target_identity",
        )
    container_name = str(observed.get("container_name") or "")
    container_bind = str(observed.get("container_bind") or "")
    host_path = str(observed.get("host_path") or "")
    device_identity = str(observed.get("device_identity") or "")
    if not container_name or not container_bind or not host_path:
        raise ColdRuntimeError(
            "target inspector did not observe container/bind/host identity",
            error_class="target_identity",
            stage="target_identity",
        )
    if container_name != config.expected_container_name:
        raise ColdRuntimeError(
            "container identity drifted",
            error_class="target_identity",
            stage="target_identity",
        )
    if container_bind != config.expected_container_bind:
        raise ColdRuntimeError(
            "container bind identity drifted",
            error_class="target_identity",
            stage="target_identity",
        )
    if host_path != config.expected_host_path:
        raise ColdRuntimeError(
            "host path identity drifted",
            error_class="target_identity",
            stage="target_identity",
        )
    if require_device_identity and not config.expected_device_identity:
        raise ColdRuntimeError(
            "expected device identity must be explicit for enforce",
            error_class="target_identity",
            stage="target_identity",
        )
    if config.expected_device_identity and device_identity != config.expected_device_identity:
        raise ColdRuntimeError(
            "device identity drifted",
            error_class="target_identity",
            stage="target_identity",
        )
    for schema, name in ALLOWED_HYPERTABLES:
        attached = attached_tablespaces(execute, schema, name)
        if COLD_TABLESPACE_NAME in attached:
            raise ColdRuntimeError(
                f"{schema}.{name} has {COLD_TABLESPACE_NAME} attached",
                error_class="hypertable_attach",
                stage="target_identity",
            )
    return TargetIdentity(
        catalog_name=COLD_TABLESPACE_NAME,
        catalog_location=catalog_location,
        container_bind=container_bind,
        host_path=host_path,
        device_identity=device_identity,
        container_exec_uid=observed_uid,
        container_exec_gid=observed_gid,
    )
