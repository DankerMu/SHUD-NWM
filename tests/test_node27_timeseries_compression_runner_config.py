"""Public runner-config contracts for the compression-only budget owner."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from packages.common import node27_timeseries_compression_budget as budget
from scripts import node27_timeseries_compression as compression

_DIRECT_KEYS = (
    "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS",
    "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND",
    "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS",
    "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS",
)
_ASSEMBLY_KEYS = (
    "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_WRAPPER_WALL_SECONDS",
    "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_SERVICE_WALL_SECONDS",
    "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_STATEMENT_TIMEOUT_MS",
    "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_PER_TICK_BOUND",
)
_RETIRED_KEYS = (
    "NODE27_COLD_RESIDENCY_STATEMENT_TIMEOUT_MS",
    "NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS",
    "NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS",
    "NODE27_TIMESERIES_SEQUENTIAL_BUDGET_ASSEMBLED",
)


def _args() -> argparse.Namespace:
    return argparse.Namespace(enforce=False, receipt_path=None, lock_path=None)


def _base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "604800",
        "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(tmp_path / "compression-receipt.json"),
        "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(tmp_path / "compression.lock"),
    }


def _catch_up() -> dict[str, str]:
    return {
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "5400000",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "1",
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "5700",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "5741",
    }


def _assembled(
    *,
    wrapper: str = "5700",
    service: str = "5741",
    statement: str = "5400000",
    bound: str = "1",
) -> dict[str, str]:
    return {
        "NODE27_TIMESERIES_COMPRESSION_BUDGET_ASSEMBLED": "1",
        "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_WRAPPER_WALL_SECONDS": wrapper,
        "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_SERVICE_WALL_SECONDS": service,
        "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_STATEMENT_TIMEOUT_MS": statement,
        "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_PER_TICK_BOUND": bound,
    }


def test_direct_runner_config_accepts_no_pair_declarations_as_coherent_defaults(tmp_path: Path) -> None:
    config = compression.config_from_args(_args(), _base_env(tmp_path))

    assert config.compress_timeout_ms == 3_600_000
    assert config.wrapper_wall_seconds == 3900
    assert config.systemd_wall_seconds == 3941
    assert config.per_tick_bound == 4
    assert "NODE27_COLD_RESIDENCY_ENV_FILE" not in _base_env(tmp_path)
    assert not any(key.startswith("NODE27_COLD_") for key in _base_env(tmp_path))


def test_direct_runner_config_accepts_a_complete_coherent_catch_up(tmp_path: Path) -> None:
    config = compression.config_from_args(_args(), {**_base_env(tmp_path), **_catch_up()})

    assert config.compress_timeout_ms == 5_400_000
    assert config.wrapper_wall_seconds == 5700
    assert config.systemd_wall_seconds == 5741
    assert config.per_tick_bound == 1


@pytest.mark.parametrize("partial_key", _DIRECT_KEYS)
def test_direct_runner_config_refuses_partial_direct_declarations(tmp_path: Path, partial_key: str) -> None:
    with pytest.raises(compression.CompressionConfigError, match="full set"):
        compression.config_from_args(_args(), {**_base_env(tmp_path), partial_key: "3900"})


def test_direct_runner_config_refuses_partial_assembly_marker(tmp_path: Path) -> None:
    with pytest.raises(compression.CompressionConfigError, match="incomplete"):
        compression.config_from_args(
            _args(),
            {**_base_env(tmp_path), "NODE27_TIMESERIES_COMPRESSION_BUDGET_ASSEMBLED": "1"},
        )


@pytest.mark.parametrize("partial_key", _ASSEMBLY_KEYS)
def test_direct_runner_config_refuses_assembly_fields_without_marker(tmp_path: Path, partial_key: str) -> None:
    with pytest.raises(compression.CompressionConfigError, match="require the assembly marker"):
        compression.config_from_args(_args(), {**_base_env(tmp_path), partial_key: "3900"})


@pytest.mark.parametrize("retired_key", _RETIRED_KEYS)
def test_direct_runner_config_refuses_retired_cold_or_sequential_keys(tmp_path: Path, retired_key: str) -> None:
    with pytest.raises(compression.CompressionConfigError, match="retired cold or sequential pair keys"):
        compression.config_from_args(_args(), {**_base_env(tmp_path), retired_key: "1"})


def test_assembled_runner_config_accepts_coherent_catch_up_and_refuses_forged_mismatch(tmp_path: Path) -> None:
    config = compression.config_from_args(_args(), {**_base_env(tmp_path), **_assembled()})
    assert config.wrapper_wall_seconds == 5700
    assert config.systemd_wall_seconds == 5741
    assert config.compress_timeout_ms == 5_400_000
    assert config.per_tick_bound == 1

    forged = {
        **_base_env(tmp_path),
        **_assembled(),
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "5400000",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "1",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "5741",
    }
    with pytest.raises(compression.CompressionConfigError, match="runner declarations"):
        compression.config_from_args(_args(), forged)


def test_service_equality_at_wrapper_plus_margin_is_refused_by_runner_config(tmp_path: Path) -> None:
    env = {
        **_base_env(tmp_path),
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "3600000",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "4",
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "3940",
    }
    with pytest.raises(compression.CompressionConfigError, match="service wall"):
        compression.config_from_args(_args(), env)


def test_wrapper_minimum_equality_is_accepted_by_runner_config(tmp_path: Path) -> None:
    env = {
        **_base_env(tmp_path),
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "3600000",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "4",
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "3941",
    }
    config = compression.config_from_args(_args(), env)
    assert config.wrapper_wall_seconds == 3900
    assert config.systemd_wall_seconds == 3941
    assert budget.SERVICE_WALL_SECONDS == 3941
