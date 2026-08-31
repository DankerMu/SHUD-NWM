"""Public runner-config contracts for direct sequential budget declarations."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_cold_residency as cold
from scripts import node27_timeseries_compression as compression

_ConfigParser = Callable[[argparse.Namespace, dict[str, str]], Any]
_PAIR_KEYS = (
    "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS",
    "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND",
    "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS",
    "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS",
    "NODE27_COLD_RESIDENCY_STATEMENT_TIMEOUT_MS",
    "NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS",
    "NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS",
)
_ASSEMBLY_KEYS = (
    "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_WRAPPER_WALL_SECONDS",
    "NODE27_TIMESERIES_SEQUENTIAL_COLD_WRAPPER_WALL_SECONDS",
    "NODE27_TIMESERIES_SEQUENTIAL_SERVICE_WALL_SECONDS",
    "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_STATEMENT_TIMEOUT_MS",
    "NODE27_TIMESERIES_SEQUENTIAL_COLD_STATEMENT_TIMEOUT_MS",
    "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_PER_TICK_BOUND",
)


def _args() -> argparse.Namespace:
    return argparse.Namespace(enforce=False, receipt_path=None, lock_path=None)


def _compression_env(tmp_path: Path) -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "604800",
        "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(tmp_path / "compression-receipt.json"),
        "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(tmp_path / "compression.lock"),
    }


def _cold_env(tmp_path: Path) -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "cold-receipt.json"),
        "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "cold.lock"),
        "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
        "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
    }


def _catch_up_pair() -> dict[str, str]:
    return {
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "5400000",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "1",
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "5700",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "9642",
        "NODE27_COLD_RESIDENCY_STATEMENT_TIMEOUT_MS": "3600000",
        "NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS": "3901",
        "NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS": "9642",
    }


@pytest.mark.parametrize(
    ("parser", "base_env", "expected_timeout", "expected_wrapper", "expected_bound"),
    [
        (compression.config_from_args, _compression_env, 3_600_000, 3900, 4),
        (cold.config_from_args, _cold_env, 3_600_000, 3901, 1),
    ],
)
def test_direct_runner_config_accepts_no_pair_declarations_as_coherent_defaults(
    tmp_path: Path,
    parser: _ConfigParser,
    base_env: Callable[[Path], dict[str, str]],
    expected_timeout: int,
    expected_wrapper: int,
    expected_bound: int,
) -> None:
    config = parser(_args(), base_env(tmp_path))

    timeout = getattr(config, "compress_timeout_ms", getattr(config, "statement_timeout_ms", None))
    assert timeout == expected_timeout
    assert config.wrapper_wall_seconds == expected_wrapper
    assert config.systemd_wall_seconds == 7842
    assert config.per_tick_bound == expected_bound


@pytest.mark.parametrize(
    ("parser", "base_env", "expected_timeout", "expected_wrapper", "expected_bound"),
    [
        (compression.config_from_args, _compression_env, 5_400_000, 5700, 1),
        (cold.config_from_args, _cold_env, 3_600_000, 3901, 1),
    ],
)
def test_direct_runner_config_accepts_a_complete_coherent_catch_up_pair(
    tmp_path: Path,
    parser: _ConfigParser,
    base_env: Callable[[Path], dict[str, str]],
    expected_timeout: int,
    expected_wrapper: int,
    expected_bound: int,
) -> None:
    config = parser(_args(), {**base_env(tmp_path), **_catch_up_pair()})

    timeout = getattr(config, "compress_timeout_ms", getattr(config, "statement_timeout_ms", None))
    assert timeout == expected_timeout
    assert config.wrapper_wall_seconds == expected_wrapper
    assert config.systemd_wall_seconds == 9642
    assert config.per_tick_bound == expected_bound


@pytest.mark.parametrize(
    ("parser", "base_env"),
    [
        (compression.config_from_args, _compression_env),
        (cold.config_from_args, _cold_env),
    ],
)
@pytest.mark.parametrize("partial_key", _PAIR_KEYS)
def test_direct_runner_config_refuses_same_lane_and_sibling_partial_pair_declarations(
    tmp_path: Path,
    parser: _ConfigParser,
    base_env: Callable[[Path], dict[str, str]],
    partial_key: str,
) -> None:
    with pytest.raises((compression.CompressionConfigError, cold.ColdResidencyConfigError), match="full pair"):
        parser(_args(), {**base_env(tmp_path), partial_key: "3900"})


@pytest.mark.parametrize(
    ("parser", "base_env"),
    [
        (compression.config_from_args, _compression_env),
        (cold.config_from_args, _cold_env),
    ],
)
@pytest.mark.parametrize("partial_key", [None, *_ASSEMBLY_KEYS])
def test_direct_runner_config_refuses_partial_assembly_marker(
    tmp_path: Path,
    parser: _ConfigParser,
    base_env: Callable[[Path], dict[str, str]],
    partial_key: str | None,
) -> None:
    declarations = {"NODE27_TIMESERIES_SEQUENTIAL_BUDGET_ASSEMBLED": "1"}
    if partial_key is not None:
        declarations[partial_key] = "3900"
    with pytest.raises((compression.CompressionConfigError, cold.ColdResidencyConfigError), match="incomplete"):
        parser(_args(), {**base_env(tmp_path), **declarations})


@pytest.mark.parametrize(
    ("parser", "base_env"),
    [
        (compression.config_from_args, _compression_env),
        (cold.config_from_args, _cold_env),
    ],
)
@pytest.mark.parametrize("partial_key", _ASSEMBLY_KEYS)
def test_direct_runner_config_refuses_assembly_fields_without_marker(
    tmp_path: Path,
    parser: _ConfigParser,
    base_env: Callable[[Path], dict[str, str]],
    partial_key: str,
) -> None:
    with pytest.raises(
        (compression.CompressionConfigError, cold.ColdResidencyConfigError),
        match="require the assembly marker",
    ):
        parser(_args(), {**base_env(tmp_path), partial_key: "3900"})
