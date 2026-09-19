"""Resolved wall budget for node-27 ordinary timeseries compression.

One descriptor-bound compression env file is the operator-owned input.  This
module parses its relevant declarations into one actual budget; it never treats
the committed defaults as an equality authority, and it never loads a cold env
or paired-lane fields.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from packages.common.safe_fs import SafeFilesystemError, open_file_no_follow

DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS = 3_600_000
DEFAULT_COMPRESSION_PER_TICK_BOUND = 2
COMPRESSION_WRAPPER_WALL_SECONDS = 3_900
SYSTEMD_MARGIN_SECONDS = 40
SERVICE_WALL_SECONDS = COMPRESSION_WRAPPER_WALL_SECONDS + SYSTEMD_MARGIN_SECONDS + 1
COMPRESSION_CLEANUP_MARGIN_SECONDS = 300
MIN_STATEMENT_TIMEOUT_MS = 1_000
MAX_LANE_ENV_BYTES = 64 * 1024
MAX_LANE_ENV_LINES = 512
_MAX_INTEGER_DIGITS = 18

COMPRESSION_STATEMENT_TIMEOUT_KEY = "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS"
COMPRESSION_PER_TICK_BOUND_KEY = "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND"
COMPRESSION_WRAPPER_WALL_KEY = "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS"
COMPRESSION_SERVICE_WALL_KEY = "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS"

ASSEMBLY_MARKER_KEY = "NODE27_TIMESERIES_COMPRESSION_BUDGET_ASSEMBLED"
ASSEMBLY_COMPRESSION_WRAPPER_KEY = "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_WRAPPER_WALL_SECONDS"
ASSEMBLY_SERVICE_WALL_KEY = "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_SERVICE_WALL_SECONDS"
ASSEMBLY_COMPRESSION_STATEMENT_KEY = "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_STATEMENT_TIMEOUT_MS"
ASSEMBLY_COMPRESSION_BOUND_KEY = "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_PER_TICK_BOUND"

_RETIRED_PAIR_KEYS = (
    "NODE27_COLD_RESIDENCY_STATEMENT_TIMEOUT_MS",
    "NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS",
    "NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS",
    "NODE27_TIMESERIES_SEQUENTIAL_BUDGET_ASSEMBLED",
    "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_WRAPPER_WALL_SECONDS",
    "NODE27_TIMESERIES_SEQUENTIAL_COLD_WRAPPER_WALL_SECONDS",
    "NODE27_TIMESERIES_SEQUENTIAL_SERVICE_WALL_SECONDS",
    "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_STATEMENT_TIMEOUT_MS",
    "NODE27_TIMESERIES_SEQUENTIAL_COLD_STATEMENT_TIMEOUT_MS",
    "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_PER_TICK_BOUND",
)

_DIRECT_KEYS = (
    COMPRESSION_STATEMENT_TIMEOUT_KEY,
    COMPRESSION_PER_TICK_BOUND_KEY,
    COMPRESSION_WRAPPER_WALL_KEY,
    COMPRESSION_SERVICE_WALL_KEY,
)
_ASSEMBLY_KEYS = (
    ASSEMBLY_COMPRESSION_WRAPPER_KEY,
    ASSEMBLY_SERVICE_WALL_KEY,
    ASSEMBLY_COMPRESSION_STATEMENT_KEY,
    ASSEMBLY_COMPRESSION_BOUND_KEY,
)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_BARE_VALUE_RE = re.compile(r"[A-Za-z0-9_./:@%+=?,~!-]*\Z")
_CANONICAL_POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z")


class CompressionBudgetError(RuntimeError):
    """Fail-closed compression budget refusal before database work starts."""


@dataclass(frozen=True)
class CompressionServiceBudget:
    """The actual wall values resolved from the compression env declaration."""

    wrapper_wall_seconds: int
    service_wall_seconds: int
    systemd_margin_seconds: int = SYSTEMD_MARGIN_SECONDS

    def required_service_wall_seconds(self) -> int:
        return self.wrapper_wall_seconds + self.systemd_margin_seconds + 1


@dataclass(frozen=True)
class ResolvedCompressionBudget:
    """Validated actual walls plus the statement and bound declarations behind them."""

    budget: CompressionServiceBudget
    compression_statement_timeout_ms: int
    compression_per_tick_bound: int

    def assembly_values(self) -> tuple[int, int, int, int]:
        """Return the stable, non-secret machine contract consumed by the wrapper."""

        return (
            self.budget.wrapper_wall_seconds,
            self.budget.service_wall_seconds,
            self.compression_statement_timeout_ms,
            self.compression_per_tick_bound,
        )


@dataclass(frozen=True)
class ParsedCompressionEnv:
    """One descriptor-bound compression env read, retained for a safe launch."""

    compression_env: Mapping[str, str]
    resolved: ResolvedCompressionBudget


DEFAULT_COMPRESSION_SERVICE_BUDGET = CompressionServiceBudget(
    wrapper_wall_seconds=COMPRESSION_WRAPPER_WALL_SECONDS,
    service_wall_seconds=SERVICE_WALL_SECONDS,
)
AUTHORITATIVE_BUDGET = DEFAULT_COMPRESSION_SERVICE_BUDGET


def compression_service_budget() -> CompressionServiceBudget:
    """Return committed defaults, not a claim about a live deployment."""

    return DEFAULT_COMPRESSION_SERVICE_BUDGET


def _require_positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CompressionBudgetError(f"{name} must be a positive integer")
    return value


def parse_positive_int_env(raw: object, *, name: str) -> int | None:
    """Parse an unset/empty or canonical decimal environment declaration."""

    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise CompressionBudgetError(f"{name} must be a canonical positive integer")
    if len(raw) > _MAX_INTEGER_DIGITS or _CANONICAL_POSITIVE_INTEGER_RE.fullmatch(raw) is None:
        raise CompressionBudgetError(f"{name} must be a canonical positive integer")
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
        raise CompressionBudgetError(f"{name} must be set")
    return parsed


def _statement_timeout(env: Mapping[str, str], name: str, *, default: int) -> int:
    value = resolve_declared_wall(env, name, default=default)
    if value < MIN_STATEMENT_TIMEOUT_MS:
        raise CompressionBudgetError(f"{name} must be >= {MIN_STATEMENT_TIMEOUT_MS}")
    return value


def _ceil_seconds(milliseconds: int) -> int:
    return -(-milliseconds // 1_000)


def _refuse_retired_pair_keys(env: Mapping[str, str]) -> None:
    present = [name for name in _RETIRED_PAIR_KEYS if name in env]
    if present:
        raise CompressionBudgetError("retired cold or sequential pair keys are unsupported")


def validate_actual_compression_walls(
    *,
    wrapper_wall_seconds: object,
    service_wall_seconds: object,
    statement_seconds: object | None = None,
    systemd_margin_seconds: object = SYSTEMD_MARGIN_SECONDS,
) -> CompressionServiceBudget:
    """Validate actual walls without requiring them to equal committed defaults."""

    wrapper = _require_positive_int(wrapper_wall_seconds, name="compression wrapper wall")
    service = _require_positive_int(service_wall_seconds, name="service wall")
    margin = _require_positive_int(systemd_margin_seconds, name="systemd margin")
    if statement_seconds is not None:
        statement = _require_positive_int(statement_seconds, name="compression statement wall")
        if wrapper < statement + COMPRESSION_CLEANUP_MARGIN_SECONDS:
            raise CompressionBudgetError(
                "compression wrapper wall must cover statement wall plus cleanup margin: "
                f"got {wrapper}, need at least {statement + COMPRESSION_CLEANUP_MARGIN_SECONDS}"
            )
    if service <= wrapper + margin:
        raise CompressionBudgetError(
            "service wall must exceed wrapper wall plus systemd margin: "
            f"got {service}, need above {wrapper + margin}"
        )
    return CompressionServiceBudget(
        wrapper_wall_seconds=wrapper,
        service_wall_seconds=service,
        systemd_margin_seconds=margin,
    )


def _validated_resolution(
    *,
    compression_statement_timeout_ms: object,
    compression_per_tick_bound: object,
    wrapper_wall_seconds: object,
    service_wall_seconds: object,
) -> ResolvedCompressionBudget:
    compression_statement = _require_positive_int(
        compression_statement_timeout_ms,
        name="compression statement timeout",
    )
    bound = _require_positive_int(compression_per_tick_bound, name="compression per-tick bound")
    if compression_statement < MIN_STATEMENT_TIMEOUT_MS:
        raise CompressionBudgetError("compression statement timeout must be >= 1000")
    resolved_budget = validate_actual_compression_walls(
        wrapper_wall_seconds=wrapper_wall_seconds,
        service_wall_seconds=service_wall_seconds,
        statement_seconds=_ceil_seconds(compression_statement),
    )
    if compression_statement > DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS and bound != 1:
        raise CompressionBudgetError(
            "compression catch-up window (§4.5) requires NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=1"
        )
    return ResolvedCompressionBudget(
        budget=resolved_budget,
        compression_statement_timeout_ms=compression_statement,
        compression_per_tick_bound=bound,
    )


def default_resolved_compression_budget() -> ResolvedCompressionBudget:
    """Return the coherent default for direct, unassembled invocations."""

    return _validated_resolution(
        compression_statement_timeout_ms=DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS,
        compression_per_tick_bound=DEFAULT_COMPRESSION_PER_TICK_BOUND,
        wrapper_wall_seconds=COMPRESSION_WRAPPER_WALL_SECONDS,
        service_wall_seconds=SERVICE_WALL_SECONDS,
    )


def resolve_compression_env(compression_env: Mapping[str, str]) -> ResolvedCompressionBudget:
    """Resolve and validate one compression env as the actual wall budget."""

    _refuse_retired_pair_keys(compression_env)
    compression_statement = _statement_timeout(
        compression_env,
        COMPRESSION_STATEMENT_TIMEOUT_KEY,
        default=DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS,
    )
    compression_bound = _require_declared_positive_int(compression_env, COMPRESSION_PER_TICK_BOUND_KEY)
    wrapper = resolve_declared_wall(
        compression_env,
        COMPRESSION_WRAPPER_WALL_KEY,
        default=COMPRESSION_WRAPPER_WALL_SECONDS,
    )
    service = resolve_declared_wall(
        compression_env,
        COMPRESSION_SERVICE_WALL_KEY,
        default=SERVICE_WALL_SECONDS,
    )
    return _validated_resolution(
        compression_statement_timeout_ms=compression_statement,
        compression_per_tick_bound=compression_bound,
        wrapper_wall_seconds=wrapper,
        service_wall_seconds=service,
    )


def _decode_env_value(value: str) -> str:
    if value == "":
        return ""
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'") or "'" in value[1:-1]:
            raise CompressionBudgetError("lane env contains unsupported assignment syntax")
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
            raise CompressionBudgetError("lane env contains unsupported assignment syntax")
        return value[1:-1]
    if _BARE_VALUE_RE.fullmatch(value) is None:
        raise CompressionBudgetError("lane env contains unsupported assignment syntax")
    return value


def parse_lane_env_data(text: str) -> dict[str, str]:
    """Parse a deliberately small shell-compatible env subset as inert data."""

    if "\x00" in text or "\r" in text:
        raise CompressionBudgetError("lane env contains unsupported assignment syntax")
    lines = text.split("\n")
    if len(lines) > MAX_LANE_ENV_LINES:
        raise CompressionBudgetError("lane env exceeds the line ceiling")
    values: dict[str, str] = {}
    for line in lines:
        if not line or not line.strip() or line.lstrip().startswith("#"):
            continue
        if line != line.lstrip():
            raise CompressionBudgetError("lane env contains unsupported assignment syntax")
        name, separator, raw_value = line.partition("=")
        if not separator or _NAME_RE.fullmatch(name) is None:
            raise CompressionBudgetError("lane env contains unsupported assignment syntax")
        if name in values:
            raise CompressionBudgetError("lane env contains duplicate assignments")
        values[name] = _decode_env_value(raw_value)
    return values


def read_lane_env_file(path: Path, *, lane: str = "compression") -> dict[str, str]:
    """Read one mode-0600 regular non-symlink lane env through one descriptor."""

    if not path.is_absolute() or "\x00" in str(path):
        raise CompressionBudgetError(f"{lane} env path must be absolute")
    try:
        fd = open_file_no_follow(path)
    except (OSError, ValueError, SafeFilesystemError):
        raise CompressionBudgetError(f"{lane} env file is unavailable or unsafe") from None
    try:
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise CompressionBudgetError(f"{lane} env file is unavailable or unsafe")
            if stat.S_IMODE(before.st_mode) != 0o600:
                raise CompressionBudgetError(f"{lane} env file must have mode 0600")
            if before.st_size > MAX_LANE_ENV_BYTES:
                raise CompressionBudgetError(f"{lane} env file exceeds the byte ceiling")
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
                raise CompressionBudgetError(f"{lane} env file changed while being read")
        except CompressionBudgetError:
            raise
        except OSError:
            raise CompressionBudgetError(f"{lane} env file is unavailable or unsafe") from None
    finally:
        os.close(fd)
    try:
        return parse_lane_env_data(bytes(raw).decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        raise CompressionBudgetError(f"{lane} env file is not valid UTF-8") from None


def read_compression_env_data(compression_env_path: Path) -> ParsedCompressionEnv:
    """Read one compression env file once and retain the exact inert map."""

    compression_env = read_lane_env_file(compression_env_path, lane="compression")
    resolved = resolve_compression_env(compression_env)
    return ParsedCompressionEnv(
        compression_env=MappingProxyType(compression_env),
        resolved=resolved,
    )


def _assembly_value(env: Mapping[str, str], name: str) -> int:
    raw = _raw_value(env, name)
    parsed = parse_positive_int_env(raw, name=name)
    if parsed is None:
        raise CompressionBudgetError("assembled compression budget is incomplete")
    return parsed


def _cross_check_runner_declarations(
    env: Mapping[str, str],
    *,
    resolved: ResolvedCompressionBudget,
) -> None:
    expected: dict[str, tuple[int, int | None]] = {
        COMPRESSION_STATEMENT_TIMEOUT_KEY: (
            resolved.compression_statement_timeout_ms,
            DEFAULT_COMPRESSION_STATEMENT_TIMEOUT_MS,
        ),
        COMPRESSION_PER_TICK_BOUND_KEY: (resolved.compression_per_tick_bound, None),
        COMPRESSION_WRAPPER_WALL_KEY: (
            resolved.budget.wrapper_wall_seconds,
            COMPRESSION_WRAPPER_WALL_SECONDS,
        ),
        COMPRESSION_SERVICE_WALL_KEY: (resolved.budget.service_wall_seconds, SERVICE_WALL_SECONDS),
    }
    for name, (expected_value, default) in expected.items():
        if name not in env:
            continue
        actual = (
            _require_declared_positive_int(env, name)
            if default is None
            else resolve_declared_wall(env, name, default=default)
        )
        if actual != expected_value:
            raise CompressionBudgetError("runner declarations disagree with the assembled compression budget")


def _assembled_runner_budget(env: Mapping[str, str]) -> ResolvedCompressionBudget:
    marker = _raw_value(env, ASSEMBLY_MARKER_KEY)
    if marker != "1":
        raise CompressionBudgetError("assembled compression budget marker is invalid")
    resolved = _validated_resolution(
        compression_statement_timeout_ms=_assembly_value(env, ASSEMBLY_COMPRESSION_STATEMENT_KEY),
        compression_per_tick_bound=_assembly_value(env, ASSEMBLY_COMPRESSION_BOUND_KEY),
        wrapper_wall_seconds=_assembly_value(env, ASSEMBLY_COMPRESSION_WRAPPER_KEY),
        service_wall_seconds=_assembly_value(env, ASSEMBLY_SERVICE_WALL_KEY),
    )
    _cross_check_runner_declarations(env, resolved=resolved)
    return resolved


def _direct_runner_budget(env: Mapping[str, str]) -> ResolvedCompressionBudget:
    if any(name in env for name in _ASSEMBLY_KEYS):
        raise CompressionBudgetError("assembled compression budget fields require the assembly marker")
    if all(name in env for name in _DIRECT_KEYS):
        return resolve_compression_env(env)
    if not any(name in env for name in _DIRECT_KEYS):
        _refuse_retired_pair_keys(env)
        return default_resolved_compression_budget()
    raise CompressionBudgetError(
        "direct runner compression values must declare the full set "
        "(including NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND)"
    )


def resolve_runner_budget(env: Mapping[str, str]) -> ResolvedCompressionBudget:
    """Resolve a runner's budget, requiring wrapper assembly when it is marked."""

    _refuse_retired_pair_keys(env)
    if ASSEMBLY_MARKER_KEY in env:
        return _assembled_runner_budget(env)
    return _direct_runner_budget(env)


def budget_literals() -> dict[str, int]:
    """Expose defaults for parity tests and documentation generators."""

    return {
        "wrapper_wall_seconds": COMPRESSION_WRAPPER_WALL_SECONDS,
        "service_wall_seconds": SERVICE_WALL_SECONDS,
        "systemd_margin_seconds": SYSTEMD_MARGIN_SECONDS,
        "compression_cleanup_margin_seconds": COMPRESSION_CLEANUP_MARGIN_SECONDS,
    }
