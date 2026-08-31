"""Resolved sequential wall budget for node-27 compression and cold residency.

The two lane environment files are the operator-owned inputs.  This module
parses their relevant declarations into one actual budget; it never treats the
committed defaults as an equality authority.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from packages.common.safe_fs import SafeFilesystemError, open_file_no_follow

DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS = 3_600_000
DEFAULT_COMPRESSION_PER_TICK_BOUND = 4
DEFAULT_COLD_STATEMENT_TIMEOUT_MS = 3_600_000
COMPRESSION_WRAPPER_WALL_SECONDS = 3_900
COLD_WRAPPER_WALL_SECONDS = 3_901
SYSTEMD_MARGIN_SECONDS = 40
SERVICE_WALL_SECONDS = COMPRESSION_WRAPPER_WALL_SECONDS + COLD_WRAPPER_WALL_SECONDS + SYSTEMD_MARGIN_SECONDS + 1
COMPRESSION_CLEANUP_MARGIN_SECONDS = 300
COLD_CLEANUP_MARGIN_SECONDS = 300
MIN_STATEMENT_TIMEOUT_MS = 1_000
MAX_LANE_ENV_BYTES = 64 * 1024
MAX_LANE_ENV_LINES = 512
_MAX_INTEGER_DIGITS = 18

COMPRESSION_STATEMENT_TIMEOUT_KEY = "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS"
COMPRESSION_PER_TICK_BOUND_KEY = "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND"
COMPRESSION_WRAPPER_WALL_KEY = "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS"
COMPRESSION_SERVICE_WALL_KEY = "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS"
COLD_STATEMENT_TIMEOUT_KEY = "NODE27_COLD_RESIDENCY_STATEMENT_TIMEOUT_MS"
COLD_WRAPPER_WALL_KEY = "NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS"
COLD_SERVICE_WALL_KEY = "NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS"

ASSEMBLY_MARKER_KEY = "NODE27_TIMESERIES_SEQUENTIAL_BUDGET_ASSEMBLED"
ASSEMBLY_COMPRESSION_WRAPPER_KEY = "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_WRAPPER_WALL_SECONDS"
ASSEMBLY_COLD_WRAPPER_KEY = "NODE27_TIMESERIES_SEQUENTIAL_COLD_WRAPPER_WALL_SECONDS"
ASSEMBLY_SERVICE_WALL_KEY = "NODE27_TIMESERIES_SEQUENTIAL_SERVICE_WALL_SECONDS"
ASSEMBLY_COMPRESSION_STATEMENT_KEY = "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_STATEMENT_TIMEOUT_MS"
ASSEMBLY_COLD_STATEMENT_KEY = "NODE27_TIMESERIES_SEQUENTIAL_COLD_STATEMENT_TIMEOUT_MS"
ASSEMBLY_COMPRESSION_BOUND_KEY = "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_PER_TICK_BOUND"

_PAIR_KEYS = (
    COMPRESSION_STATEMENT_TIMEOUT_KEY,
    COMPRESSION_PER_TICK_BOUND_KEY,
    COMPRESSION_WRAPPER_WALL_KEY,
    COMPRESSION_SERVICE_WALL_KEY,
    COLD_STATEMENT_TIMEOUT_KEY,
    COLD_WRAPPER_WALL_KEY,
    COLD_SERVICE_WALL_KEY,
)
_ASSEMBLY_KEYS = (
    ASSEMBLY_COMPRESSION_WRAPPER_KEY,
    ASSEMBLY_COLD_WRAPPER_KEY,
    ASSEMBLY_SERVICE_WALL_KEY,
    ASSEMBLY_COMPRESSION_STATEMENT_KEY,
    ASSEMBLY_COLD_STATEMENT_KEY,
    ASSEMBLY_COMPRESSION_BOUND_KEY,
)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_BARE_VALUE_RE = re.compile(r"[A-Za-z0-9_./:@%+=?,~!-]*\Z")
_CANONICAL_POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z")


class SequentialBudgetError(RuntimeError):
    """Fail-closed sequential budget refusal before database work starts."""


@dataclass(frozen=True)
class SequentialServiceBudget:
    """The actual wall values resolved from the two lane declarations."""

    compression_wrapper_wall_seconds: int
    cold_wrapper_wall_seconds: int
    service_wall_seconds: int
    systemd_margin_seconds: int = SYSTEMD_MARGIN_SECONDS

    def required_service_wall_seconds(self) -> int:
        return (
            self.compression_wrapper_wall_seconds
            + self.cold_wrapper_wall_seconds
            + self.systemd_margin_seconds
            + 1
        )


@dataclass(frozen=True)
class ResolvedSequentialBudget:
    """Validated actual pair plus the statement and bound declarations behind it."""

    budget: SequentialServiceBudget
    compression_statement_timeout_ms: int
    cold_statement_timeout_ms: int
    compression_per_tick_bound: int

    def assembly_values(self) -> tuple[int, int, int, int, int, int]:
        """Return the stable, non-secret machine contract consumed by wrappers."""

        return (
            self.budget.compression_wrapper_wall_seconds,
            self.budget.cold_wrapper_wall_seconds,
            self.budget.service_wall_seconds,
            self.compression_statement_timeout_ms,
            self.cold_statement_timeout_ms,
            self.compression_per_tick_bound,
        )


@dataclass(frozen=True)
class ParsedLaneEnvPair:
    """One descriptor-bound pair read, retained for safe launch assembly."""

    compression_env: Mapping[str, str]
    cold_env: Mapping[str, str]
    resolved: ResolvedSequentialBudget


DEFAULT_SEQUENTIAL_SERVICE_BUDGET = SequentialServiceBudget(
    compression_wrapper_wall_seconds=COMPRESSION_WRAPPER_WALL_SECONDS,
    cold_wrapper_wall_seconds=COLD_WRAPPER_WALL_SECONDS,
    service_wall_seconds=SERVICE_WALL_SECONDS,
)
# Kept as a compatibility name for callers that use the committed defaults.
AUTHORITATIVE_BUDGET = DEFAULT_SEQUENTIAL_SERVICE_BUDGET


def sequential_service_budget() -> SequentialServiceBudget:
    """Return committed defaults, not a claim about a live deployment."""

    return DEFAULT_SEQUENTIAL_SERVICE_BUDGET


def _require_positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SequentialBudgetError(f"{name} must be a positive integer")
    return value


def parse_positive_int_env(raw: object, *, name: str) -> int | None:
    """Parse an unset/empty or canonical decimal environment declaration."""

    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise SequentialBudgetError(f"{name} must be a canonical positive integer")
    if len(raw) > _MAX_INTEGER_DIGITS or _CANONICAL_POSITIVE_INTEGER_RE.fullmatch(raw) is None:
        raise SequentialBudgetError(f"{name} must be a canonical positive integer")
    return int(raw)


def _raw_value(env: Mapping[str, str], name: str) -> object:
    try:
        return env[name] if name in env else None
    except (KeyError, TypeError):
        return None


def resolve_declared_wall(env: Mapping[str, str], name: str, *, default: int) -> int:
    parsed = parse_positive_int_env(_raw_value(env, name), name=name)
    return default if parsed is None else parsed


def _require_declared_positive_int(env: Mapping[str, str], name: str) -> int:
    parsed = parse_positive_int_env(_raw_value(env, name), name=name)
    if parsed is None:
        raise SequentialBudgetError(f"{name} must be set")
    return parsed


def _statement_timeout(
    env: Mapping[str, str], name: str, *, default: int
) -> int:
    value = resolve_declared_wall(env, name, default=default)
    if value < MIN_STATEMENT_TIMEOUT_MS:
        raise SequentialBudgetError(f"{name} must be >= {MIN_STATEMENT_TIMEOUT_MS}")
    return value


def _ceil_seconds(milliseconds: int) -> int:
    return -(-milliseconds // 1_000)


def validate_actual_sequential_walls(
    *,
    compression_wrapper_wall_seconds: object,
    cold_wrapper_wall_seconds: object,
    service_wall_seconds: object,
    compression_statement_seconds: object | None = None,
    cold_statement_seconds: object | None = None,
    systemd_margin_seconds: object = SYSTEMD_MARGIN_SECONDS,
) -> SequentialServiceBudget:
    """Validate actual walls without requiring them to equal committed defaults."""

    compression = _require_positive_int(
        compression_wrapper_wall_seconds,
        name="compression wrapper wall",
    )
    cold = _require_positive_int(cold_wrapper_wall_seconds, name="cold wrapper wall")
    service = _require_positive_int(service_wall_seconds, name="service wall")
    margin = _require_positive_int(systemd_margin_seconds, name="systemd margin")
    if compression_statement_seconds is not None:
        statement = _require_positive_int(
            compression_statement_seconds,
            name="compression statement wall",
        )
        if compression < statement + COMPRESSION_CLEANUP_MARGIN_SECONDS:
            raise SequentialBudgetError(
                "compression wrapper wall must cover statement wall plus cleanup margin: "
                f"got {compression}, need at least {statement + COMPRESSION_CLEANUP_MARGIN_SECONDS}"
            )
    if cold_statement_seconds is not None:
        statement = _require_positive_int(cold_statement_seconds, name="cold statement wall")
        if cold < statement + COLD_CLEANUP_MARGIN_SECONDS:
            raise SequentialBudgetError(
                "cold wrapper wall must cover statement wall plus cleanup margin: "
                f"got {cold}, need at least {statement + COLD_CLEANUP_MARGIN_SECONDS}"
            )
    if service <= compression + cold + margin:
        raise SequentialBudgetError(
            "service wall must exceed both wrapper walls plus systemd margin: "
            f"got {service}, need above {compression + cold + margin}"
        )
    return SequentialServiceBudget(
        compression_wrapper_wall_seconds=compression,
        cold_wrapper_wall_seconds=cold,
        service_wall_seconds=service,
        systemd_margin_seconds=margin,
    )


def _validated_resolution(
    *,
    compression_statement_timeout_ms: object,
    compression_per_tick_bound: object,
    compression_wrapper_wall_seconds: object,
    cold_statement_timeout_ms: object,
    cold_wrapper_wall_seconds: object,
    service_wall_seconds: object,
) -> ResolvedSequentialBudget:
    compression_statement = _require_positive_int(
        compression_statement_timeout_ms,
        name="compression statement timeout",
    )
    cold_statement = _require_positive_int(cold_statement_timeout_ms, name="cold statement timeout")
    bound = _require_positive_int(compression_per_tick_bound, name="compression per-tick bound")
    if compression_statement < MIN_STATEMENT_TIMEOUT_MS:
        raise SequentialBudgetError("compression statement timeout must be >= 1000")
    if cold_statement < MIN_STATEMENT_TIMEOUT_MS:
        raise SequentialBudgetError("cold statement timeout must be >= 1000")
    resolved_budget = validate_actual_sequential_walls(
        compression_wrapper_wall_seconds=compression_wrapper_wall_seconds,
        cold_wrapper_wall_seconds=cold_wrapper_wall_seconds,
        service_wall_seconds=service_wall_seconds,
        compression_statement_seconds=_ceil_seconds(compression_statement),
        cold_statement_seconds=_ceil_seconds(cold_statement),
    )
    if compression_statement > DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS and bound != 1:
        raise SequentialBudgetError(
            "compression catch-up window (§4.5) requires NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=1"
        )
    return ResolvedSequentialBudget(
        budget=resolved_budget,
        compression_statement_timeout_ms=compression_statement,
        cold_statement_timeout_ms=cold_statement,
        compression_per_tick_bound=bound,
    )


def default_resolved_sequential_budget() -> ResolvedSequentialBudget:
    """Return the coherent default pair for direct, unassembled invocations."""

    return _validated_resolution(
        compression_statement_timeout_ms=DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS,
        compression_per_tick_bound=DEFAULT_COMPRESSION_PER_TICK_BOUND,
        compression_wrapper_wall_seconds=COMPRESSION_WRAPPER_WALL_SECONDS,
        cold_statement_timeout_ms=DEFAULT_COLD_STATEMENT_TIMEOUT_MS,
        cold_wrapper_wall_seconds=COLD_WRAPPER_WALL_SECONDS,
        service_wall_seconds=SERVICE_WALL_SECONDS,
    )


def _cross_check_mirrors(
    compression_env: Mapping[str, str],
    cold_env: Mapping[str, str],
    resolved: ResolvedSequentialBudget,
) -> None:
    expected = {
        COMPRESSION_STATEMENT_TIMEOUT_KEY: (
            resolved.compression_statement_timeout_ms,
            DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS,
        ),
        COMPRESSION_PER_TICK_BOUND_KEY: (resolved.compression_per_tick_bound, None),
        COMPRESSION_WRAPPER_WALL_KEY: (
            resolved.budget.compression_wrapper_wall_seconds,
            COMPRESSION_WRAPPER_WALL_SECONDS,
        ),
        COMPRESSION_SERVICE_WALL_KEY: (
            resolved.budget.service_wall_seconds,
            SERVICE_WALL_SECONDS,
        ),
        COLD_STATEMENT_TIMEOUT_KEY: (
            resolved.cold_statement_timeout_ms,
            DEFAULT_COLD_STATEMENT_TIMEOUT_MS,
        ),
        COLD_WRAPPER_WALL_KEY: (
            resolved.budget.cold_wrapper_wall_seconds,
            COLD_WRAPPER_WALL_SECONDS,
        ),
        COLD_SERVICE_WALL_KEY: (
            resolved.budget.service_wall_seconds,
            SERVICE_WALL_SECONDS,
        ),
    }
    for env, source in ((cold_env, "cold"), (compression_env, "compression")):
        for name, (expected_value, default) in expected.items():
            if name not in env:
                continue
            actual = (
                _require_declared_positive_int(env, name)
                if default is None
                else resolve_declared_wall(env, name, default=default)
            )
            if actual != expected_value:
                raise SequentialBudgetError(f"{source} lane env disagrees with the resolved sequential pair")


def resolve_lane_env_pair(
    compression_env: Mapping[str, str],
    cold_env: Mapping[str, str],
) -> ResolvedSequentialBudget:
    """Resolve and validate both lane declarations as one actual sequential pair."""

    compression_statement = _statement_timeout(
        compression_env,
        COMPRESSION_STATEMENT_TIMEOUT_KEY,
        default=DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS,
    )
    compression_bound = _require_declared_positive_int(compression_env, COMPRESSION_PER_TICK_BOUND_KEY)
    compression_wrapper = resolve_declared_wall(
        compression_env,
        COMPRESSION_WRAPPER_WALL_KEY,
        default=COMPRESSION_WRAPPER_WALL_SECONDS,
    )
    compression_service = resolve_declared_wall(
        compression_env,
        COMPRESSION_SERVICE_WALL_KEY,
        default=SERVICE_WALL_SECONDS,
    )
    cold_statement = _statement_timeout(
        cold_env,
        COLD_STATEMENT_TIMEOUT_KEY,
        default=DEFAULT_COLD_STATEMENT_TIMEOUT_MS,
    )
    cold_wrapper = resolve_declared_wall(
        cold_env,
        COLD_WRAPPER_WALL_KEY,
        default=COLD_WRAPPER_WALL_SECONDS,
    )
    cold_service = resolve_declared_wall(
        cold_env,
        COLD_SERVICE_WALL_KEY,
        default=SERVICE_WALL_SECONDS,
    )
    if compression_service != cold_service:
        raise SequentialBudgetError("lane env files disagree on the actual service wall")
    resolved = _validated_resolution(
        compression_statement_timeout_ms=compression_statement,
        compression_per_tick_bound=compression_bound,
        compression_wrapper_wall_seconds=compression_wrapper,
        cold_statement_timeout_ms=cold_statement,
        cold_wrapper_wall_seconds=cold_wrapper,
        service_wall_seconds=compression_service,
    )
    _cross_check_mirrors(compression_env, cold_env, resolved)
    return resolved


def validate_lane_env_pair(
    compression_env: Mapping[str, str],
    cold_env: Mapping[str, str],
) -> SequentialServiceBudget:
    """Compatibility seam returning the actual validated wall tuple."""

    return resolve_lane_env_pair(compression_env, cold_env).budget


def _decode_env_value(value: str) -> str:
    if value == "":
        return ""
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'") or "'" in value[1:-1]:
            raise SequentialBudgetError("lane env contains unsupported assignment syntax")
        return value[1:-1]
    if value.startswith('"'):
        if (
            len(value) < 2
            or not value.endswith('"')
            or '"' in value[1:-1]
            or "\\" in value[1:-1]
            or "$" in value[1:-1]
            or "`" in value[1:-1]
        ):
            raise SequentialBudgetError("lane env contains unsupported assignment syntax")
        return value[1:-1]
    if _BARE_VALUE_RE.fullmatch(value) is None:
        raise SequentialBudgetError("lane env contains unsupported assignment syntax")
    return value


def parse_lane_env_data(text: str) -> dict[str, str]:
    """Parse a deliberately small shell-compatible env subset as inert data."""

    if "\x00" in text or "\r" in text:
        raise SequentialBudgetError("lane env contains unsupported assignment syntax")
    lines = text.split("\n")
    if len(lines) > MAX_LANE_ENV_LINES:
        raise SequentialBudgetError("lane env exceeds the line ceiling")
    values: dict[str, str] = {}
    for line in lines:
        if not line or not line.strip() or line.lstrip().startswith("#"):
            continue
        if line != line.lstrip():
            raise SequentialBudgetError("lane env contains unsupported assignment syntax")
        name, separator, raw_value = line.partition("=")
        if not separator or _NAME_RE.fullmatch(name) is None:
            raise SequentialBudgetError("lane env contains unsupported assignment syntax")
        if name in values:
            raise SequentialBudgetError("lane env contains duplicate assignments")
        values[name] = _decode_env_value(raw_value)
    return values


def read_lane_env_file(path: Path, *, lane: str) -> dict[str, str]:
    """Read one mode-0600 regular non-symlink lane env through one descriptor."""

    if not path.is_absolute() or "\x00" in str(path):
        raise SequentialBudgetError(f"{lane} env path must be absolute")
    try:
        fd = open_file_no_follow(path)
    except (OSError, ValueError, SafeFilesystemError):
        raise SequentialBudgetError(f"{lane} env file is unavailable or unsafe") from None
    try:
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise SequentialBudgetError(f"{lane} env file is unavailable or unsafe")
            if stat.S_IMODE(before.st_mode) != 0o600:
                raise SequentialBudgetError(f"{lane} env file must have mode 0600")
            if before.st_size > MAX_LANE_ENV_BYTES:
                raise SequentialBudgetError(f"{lane} env file exceeds the byte ceiling")
            raw = bytearray()
            while len(raw) < before.st_size:
                chunk = os.read(fd, min(8 * 1024, before.st_size - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(fd)
            if (
                len(raw) != before.st_size
                or os.read(fd, 1)
                or (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
            ):
                raise SequentialBudgetError(f"{lane} env file changed while being read")
        except SequentialBudgetError:
            raise
        except OSError:
            raise SequentialBudgetError(f"{lane} env file is unavailable or unsafe") from None
    finally:
        os.close(fd)
    try:
        return parse_lane_env_data(bytes(raw).decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        raise SequentialBudgetError(f"{lane} env file is not valid UTF-8") from None


def read_lane_env_pair_data(
    compression_env_path: Path,
    cold_env_path: Path,
) -> ParsedLaneEnvPair:
    """Read both lane files once and retain exact inert maps for a safe launch."""

    compression_env = read_lane_env_file(compression_env_path, lane="compression")
    cold_env = read_lane_env_file(cold_env_path, lane="cold")
    resolved = resolve_lane_env_pair(compression_env, cold_env)
    return ParsedLaneEnvPair(
        compression_env=MappingProxyType(compression_env),
        cold_env=MappingProxyType(cold_env),
        resolved=resolved,
    )


def read_lane_env_pair(
    compression_env_path: Path,
    cold_env_path: Path,
) -> ResolvedSequentialBudget:
    """Read both deployed lane files and validate the pair from that invocation."""

    return read_lane_env_pair_data(compression_env_path, cold_env_path).resolved


def _assembly_value(env: Mapping[str, str], name: str) -> int:
    raw = _raw_value(env, name)
    parsed = parse_positive_int_env(raw, name=name)
    if parsed is None:
        raise SequentialBudgetError("assembled sequential budget is incomplete")
    return parsed


def _cross_check_runner_declarations(
    env: Mapping[str, str],
    *,
    lane: Literal["compression", "cold"],
    resolved: ResolvedSequentialBudget,
) -> None:
    expected: dict[str, tuple[int, int | None]] = {
        COMPRESSION_STATEMENT_TIMEOUT_KEY: (
            resolved.compression_statement_timeout_ms,
            DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS,
        ),
        COMPRESSION_PER_TICK_BOUND_KEY: (resolved.compression_per_tick_bound, None),
        COMPRESSION_WRAPPER_WALL_KEY: (
            resolved.budget.compression_wrapper_wall_seconds,
            COMPRESSION_WRAPPER_WALL_SECONDS,
        ),
        COMPRESSION_SERVICE_WALL_KEY: (resolved.budget.service_wall_seconds, SERVICE_WALL_SECONDS),
        COLD_STATEMENT_TIMEOUT_KEY: (
            resolved.cold_statement_timeout_ms,
            DEFAULT_COLD_STATEMENT_TIMEOUT_MS,
        ),
        COLD_WRAPPER_WALL_KEY: (resolved.budget.cold_wrapper_wall_seconds, COLD_WRAPPER_WALL_SECONDS),
        COLD_SERVICE_WALL_KEY: (resolved.budget.service_wall_seconds, SERVICE_WALL_SECONDS),
    }
    del lane
    for name, (expected_value, default) in expected.items():
        if name not in env:
            continue
        actual = (
            _require_declared_positive_int(env, name)
            if default is None
            else resolve_declared_wall(env, name, default=default)
        )
        if actual != expected_value:
            raise SequentialBudgetError("runner declarations disagree with the assembled sequential budget")


def _assembled_runner_budget(
    env: Mapping[str, str], *, lane: Literal["compression", "cold"]
) -> ResolvedSequentialBudget:
    marker = _raw_value(env, ASSEMBLY_MARKER_KEY)
    if marker != "1":
        raise SequentialBudgetError("assembled sequential budget marker is invalid")
    resolved = _validated_resolution(
        compression_statement_timeout_ms=_assembly_value(env, ASSEMBLY_COMPRESSION_STATEMENT_KEY),
        compression_per_tick_bound=_assembly_value(env, ASSEMBLY_COMPRESSION_BOUND_KEY),
        compression_wrapper_wall_seconds=_assembly_value(env, ASSEMBLY_COMPRESSION_WRAPPER_KEY),
        cold_statement_timeout_ms=_assembly_value(env, ASSEMBLY_COLD_STATEMENT_KEY),
        cold_wrapper_wall_seconds=_assembly_value(env, ASSEMBLY_COLD_WRAPPER_KEY),
        service_wall_seconds=_assembly_value(env, ASSEMBLY_SERVICE_WALL_KEY),
    )
    _cross_check_runner_declarations(env, lane=lane, resolved=resolved)
    return resolved


def _direct_runner_budget(
    env: Mapping[str, str], *, lane: Literal["compression", "cold"]
) -> ResolvedSequentialBudget:
    del lane
    if any(name in env for name in _ASSEMBLY_KEYS):
        raise SequentialBudgetError("assembled sequential budget fields require the assembly marker")
    if all(name in env for name in _PAIR_KEYS):
        return resolve_lane_env_pair(env, env)
    if not any(name in env for name in _PAIR_KEYS):
        return default_resolved_sequential_budget()
    raise SequentialBudgetError(
        "direct runner sequential values must declare the full pair "
        "(including NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND)"
    )


def resolve_runner_budget(
    env: Mapping[str, str], *, lane: Literal["compression", "cold"]
) -> ResolvedSequentialBudget:
    """Resolve a runner's budget, requiring wrapper assembly when it is marked."""

    if lane not in {"compression", "cold"}:
        raise ValueError("lane must be compression or cold")
    if ASSEMBLY_MARKER_KEY in env:
        return _assembled_runner_budget(env, lane=lane)
    return _direct_runner_budget(env, lane=lane)


def budget_literals() -> dict[str, int]:
    """Expose defaults for parity tests and documentation generators."""

    return {
        "compression_wrapper_wall_seconds": COMPRESSION_WRAPPER_WALL_SECONDS,
        "cold_wrapper_wall_seconds": COLD_WRAPPER_WALL_SECONDS,
        "service_wall_seconds": SERVICE_WALL_SECONDS,
        "systemd_margin_seconds": SYSTEMD_MARGIN_SECONDS,
        "compression_cleanup_margin_seconds": COMPRESSION_CLEANUP_MARGIN_SECONDS,
        "cold_cleanup_margin_seconds": COLD_CLEANUP_MARGIN_SECONDS,
    }
