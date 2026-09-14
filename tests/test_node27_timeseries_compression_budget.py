"""Public-contract tests for node-27's ordinary compression budget owner."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from packages.common import node27_timeseries_compression_budget as budget
from scripts import node27_timeseries_compression as compression

_ROOT = Path(__file__).resolve().parents[1]
_PREFLIGHT = _ROOT / "scripts" / "node27_timeseries_budget_preflight.py"
_COMPRESSION_RECEIPT_EXAMPLE = _ROOT / "schemas" / "examples" / "timeseries_compression_receipt.example.json"
_COMPRESSION_RECEIPT_SCHEMA = _ROOT / "schemas" / "timeseries_compression_receipt.schema.json"
_SECRET_DSN = "postgresql://alice:super-secret-password@127.0.0.1:55432/nhms?signed=very-secret-token"


def _compression_env(**overrides: str) -> dict[str, str]:
    values = {
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "3600000",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "4",
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "3941",
    }
    values.update(overrides)
    return values


def _write_env(path: Path, values: dict[str, str], *, mode: int = 0o600) -> Path:
    lines = [f"DATABASE_URL='{_SECRET_DSN}'"]
    lines.extend(f"{name}={value}" for name, value in values.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _preflight(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_PREFLIGHT), *args],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolver_accepts_default_env_and_coherent_enlarged_catch_up() -> None:
    default = budget.resolve_compression_env(_compression_env())
    assert default.budget == budget.CompressionServiceBudget(3900, 3941, 40)
    assert default.compression_statement_timeout_ms == 3_600_000
    assert default.compression_per_tick_bound == 4
    assert default.budget.wrapper_wall_seconds == 3_600 + budget.COMPRESSION_CLEANUP_MARGIN_SECONDS
    assert default.budget.service_wall_seconds == 3_900 + 40 + 1

    catch_up = budget.resolve_compression_env(
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS="5400000",
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="1",
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="5700",
            NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="5741",
        )
    )
    assert catch_up.budget == budget.CompressionServiceBudget(5700, 5741, 40)
    assert catch_up.compression_statement_timeout_ms == 5_400_000
    assert catch_up.compression_per_tick_bound == 1


def test_canonical_compression_receipt_example_matches_the_default_budget() -> None:
    example = json.loads(_COMPRESSION_RECEIPT_EXAMPLE.read_text(encoding="utf-8"))
    schema = json.loads(_COMPRESSION_RECEIPT_SCHEMA.read_text(encoding="utf-8"))

    jsonschema.validate(example, schema)
    resolved = budget.resolve_compression_env(
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS=str(example["budget"]["compress_timeout_ms"]),
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=str(example["per_tick_bound"]),
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS=str(example["budget"]["wrapper_wall_seconds"]),
            NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS=str(example["budget"]["systemd_wall_seconds"]),
        )
    )

    assert example["schema_version"] == "2.1"
    assert example["budget"]["cleanup_margin_seconds"] == budget.COMPRESSION_CLEANUP_MARGIN_SECONDS
    assert example["budget"]["systemd_wall_seconds"] == 3941
    assert resolved.budget == budget.CompressionServiceBudget(3900, 3941, 40)
    assert resolved.compression_statement_timeout_ms == 3_600_000
    assert resolved.compression_per_tick_bound == 1


def test_compression_receipt_schema_keeps_2_1_budgets_without_cleanup_margin() -> None:
    example = json.loads(_COMPRESSION_RECEIPT_EXAMPLE.read_text(encoding="utf-8"))
    schema = json.loads(_COMPRESSION_RECEIPT_SCHEMA.read_text(encoding="utf-8"))
    assert example["schema_version"] == "2.1"
    example["budget"].pop("cleanup_margin_seconds")

    jsonschema.validate(example, schema)


def test_compression_receipt_records_the_shared_cleanup_margin(tmp_path: Path) -> None:
    config = compression.config_from_args(
        argparse.Namespace(enforce=False, receipt_path=None, lock_path=None),
        {
            "DATABASE_URL": _SECRET_DSN,
            "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "604800",
            "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
            "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(tmp_path / "runner.lock"),
        },
    )

    receipt = compression.build_receipt(
        config,
        now_utc=datetime(2026, 8, 30, tzinfo=UTC),
        fetch_chunks=lambda _dsn: [],
        measure_chunk_bytes=lambda _dsn, _chunk, **_kwargs: 0,
        compress_chunk=lambda _dsn, _chunk: None,
        head_sha="a" * 40,
    )

    assert budget.COMPRESSION_CLEANUP_MARGIN_SECONDS == 300
    assert receipt["budget"]["cleanup_margin_seconds"] == budget.COMPRESSION_CLEANUP_MARGIN_SECONDS
    assert compression._CLEANUP_MARGIN_SECONDS is budget.COMPRESSION_CLEANUP_MARGIN_SECONDS
    assert config.systemd_wall_seconds == 3941


@pytest.mark.parametrize(
    "values",
    [
        _compression_env(NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="3940"),
        _compression_env(NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="3899"),
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS="5400000",
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="5699",
            NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="5741",
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="1",
        ),
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS="5400000",
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="5700",
            NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="5741",
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="2",
        ),
    ],
)
def test_resolver_refuses_invalid_compression_contract(values: dict[str, str]) -> None:
    with pytest.raises(budget.CompressionBudgetError):
        budget.resolve_compression_env(values)


def test_service_equality_at_wrapper_plus_margin_is_refused() -> None:
    with pytest.raises(budget.CompressionBudgetError, match="exceed wrapper wall plus systemd margin"):
        budget.validate_actual_compression_walls(
            wrapper_wall_seconds=3900,
            service_wall_seconds=3940,
            statement_seconds=3600,
        )


def test_wrapper_minimum_equality_is_accepted() -> None:
    resolved = budget.validate_actual_compression_walls(
        wrapper_wall_seconds=3900,
        service_wall_seconds=3941,
        statement_seconds=3600,
    )
    assert resolved.wrapper_wall_seconds == 3900
    assert resolved.service_wall_seconds == 3941


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS", " 3900"),
        ("NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS", "3900 "),
        ("NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS", "03900"),
        ("NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS", "+3900"),
        ("NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS", "3_900"),
        ("NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS", "3900.0"),
    ],
)
def test_resolver_refuses_ambiguous_budget_integer_forms(name: str, raw: str) -> None:
    with pytest.raises(budget.CompressionBudgetError):
        budget.resolve_compression_env(_compression_env(**{name: raw}))


def test_resolver_refuses_retired_cold_or_sequential_pair_keys() -> None:
    with pytest.raises(budget.CompressionBudgetError, match="retired cold or sequential pair keys"):
        budget.resolve_compression_env(
            _compression_env(NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS="3901")
        )
    with pytest.raises(budget.CompressionBudgetError, match="retired cold or sequential pair keys"):
        budget.resolve_runner_budget({"NODE27_TIMESERIES_SEQUENTIAL_BUDGET_ASSEMBLED": "1"})


def test_preflight_reads_mode_0600_env_without_a_cold_file_and_emits_only_wrapper_wall(
    tmp_path: Path,
) -> None:
    compression_env = _write_env(tmp_path / "compression.env", _compression_env())
    assert not (tmp_path / "cold.env").exists()

    result = _preflight(
        "--compression-env",
        str(compression_env),
        "--lane",
        "compression",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "3900\n"
    assert result.stderr == ""
    checked = _preflight(
        "--compression-env",
        str(compression_env),
        "--check",
    )
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout == ""
    assert checked.stderr == ""


def test_preflight_assembly_output_is_bounded_canonical_nonsecret_integers(tmp_path: Path) -> None:
    compression_env = _write_env(
        tmp_path / "compression.env",
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS="5400000",
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="1",
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="5700",
            NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="5741",
        ),
    )

    result = _preflight(
        "--compression-env",
        str(compression_env),
        "--lane",
        "compression",
        "--format",
        "assembly",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "5700,5741,5400000,1\n"
    assert result.stderr == ""
    assert _SECRET_DSN not in result.stdout
    assert "super-secret-password" not in result.stdout


def test_preflight_refuses_removed_cold_flags(tmp_path: Path) -> None:
    compression_env = _write_env(tmp_path / "compression.env", _compression_env())
    result = _preflight(
        "--compression-env",
        str(compression_env),
        "--cold-env",
        str(tmp_path / "missing-cold.env"),
        "--check",
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "invalid arguments" in result.stderr
    launched = _preflight(
        "--compression-env",
        str(compression_env),
        "--launch",
        "cold",
    )
    assert launched.returncode == 2
    assert launched.stdout == ""
    assert "invalid arguments" in launched.stderr


@pytest.mark.parametrize("unsafe_kind", ["symlink", "wrong-mode", "directory", "oversized", "command", "duplicate"])
def test_preflight_refuses_unsafe_or_ambiguous_input_without_leaking_secrets(
    tmp_path: Path, unsafe_kind: str
) -> None:
    compression_env = _write_env(tmp_path / "compression.env", _compression_env())
    if unsafe_kind == "symlink":
        target = tmp_path / "compression-target.env"
        compression_env.rename(target)
        compression_env.symlink_to(target)
    elif unsafe_kind == "wrong-mode":
        compression_env.chmod(0o640)
    elif unsafe_kind == "directory":
        compression_env.unlink()
        compression_env.mkdir()
    elif unsafe_kind == "oversized":
        compression_env.write_bytes(b"A" * (128 * 1024))
        compression_env.chmod(0o600)
    elif unsafe_kind == "command":
        compression_env.write_text(
            f"DATABASE_URL='{_SECRET_DSN}'\n"
            "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS=$(id)\n",
            encoding="utf-8",
        )
        compression_env.chmod(0o600)
    elif unsafe_kind == "duplicate":
        compression_env.write_text(
            compression_env.read_text(encoding="utf-8")
            + "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS=3900\n",
            encoding="utf-8",
        )
        compression_env.chmod(0o600)
    else:
        raise AssertionError(unsafe_kind)

    result = _preflight(
        "--compression-env",
        str(compression_env),
        "--lane",
        "compression",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert _SECRET_DSN not in result.stderr
    assert "super-secret-password" not in result.stderr
    assert "very-secret-token" not in result.stderr


def test_preflight_rejects_relevant_whitespace_and_missing_compression_bound(tmp_path: Path) -> None:
    compression_env = _write_env(
        tmp_path / "compression.env",
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="",
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS=" 3900",
        ),
    )

    result = _preflight(
        "--compression-env",
        str(compression_env),
        "--lane",
        "compression",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert _SECRET_DSN not in result.stderr


def test_preflight_requires_absolute_lane_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compression_env = _write_env(tmp_path / "compression.env", _compression_env())
    monkeypatch.chdir(tmp_path)

    result = _preflight(
        "--compression-env",
        compression_env.name,
        "--check",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert _SECRET_DSN not in result.stderr


def test_preflight_hides_secret_like_absolute_path_on_read_refusal(tmp_path: Path) -> None:
    secret_path = f"/missing/{_SECRET_DSN}"

    result = _preflight(
        "--compression-env",
        secret_path,
        "--check",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert _SECRET_DSN not in result.stderr
    assert "super-secret-password" not in result.stderr
    assert "very-secret-token" not in result.stderr


def _assembled_env(
    *,
    compression_wall: str = "5700",
    service_wall: str = "5741",
    compression_timeout: str = "5400000",
    bound: str = "1",
) -> dict[str, str]:
    return {
        "NODE27_TIMESERIES_COMPRESSION_BUDGET_ASSEMBLED": "1",
        "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_WRAPPER_WALL_SECONDS": compression_wall,
        "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_SERVICE_WALL_SECONDS": service_wall,
        "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_STATEMENT_TIMEOUT_MS": compression_timeout,
        "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_PER_TICK_BOUND": bound,
    }


def test_assembled_runner_values_allow_coherent_catch_up_and_refuse_declaration_mismatch() -> None:
    resolved = budget.resolve_runner_budget(_assembled_env())
    assert resolved.budget == budget.CompressionServiceBudget(5700, 5741, 40)
    mismatched = {
        **_assembled_env(),
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "5400000",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "1",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "5741",
    }
    with pytest.raises(budget.CompressionBudgetError, match="runner declarations"):
        budget.resolve_runner_budget(mismatched)


def test_partial_assembly_and_direct_partial_declaration_fail_closed() -> None:
    with pytest.raises(budget.CompressionBudgetError, match="incomplete"):
        budget.resolve_runner_budget({"NODE27_TIMESERIES_COMPRESSION_BUDGET_ASSEMBLED": "1"})
    with pytest.raises(budget.CompressionBudgetError, match="full set"):
        budget.resolve_runner_budget({"NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "5700"})


def test_quoted_inert_values_remain_data_and_cannot_execute(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-run"
    compression_env = _write_env(
        tmp_path / "compression.env",
        {
            **_compression_env(),
            "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": f"'$(touch {marker})'",
        },
    )
    parsed = budget.read_compression_env_data(compression_env)
    assert parsed.compression_env["NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"] == f"$(touch {marker})"
    assert not marker.exists()


def test_systemd_preflight_precedes_the_single_compression_execstart_and_matches_default() -> None:
    service = (_ROOT / "infra/systemd/nhms-node27-timeseries-compression.service").read_text(encoding="utf-8")
    lines = service.splitlines()
    preflight_index = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("ExecStartPre=") and "node27_timeseries_budget_preflight.py" in line
    )
    starts = [index for index, line in enumerate(lines) if line.startswith("ExecStart=")]
    assert len(starts) == 1
    assert preflight_index < starts[0]
    assert "--compression-env /home/nwm/NWM/infra/env/node27-timeseries-compression.env" in service
    assert "--cold-env" not in service
    assert "node27_cold_residency_once.sh" not in service
    configured_wall = int(next(line for line in lines if line.startswith("TimeoutStartSec=")).split("=", 1)[1])
    assert configured_wall == 3941
    assert budget.resolve_compression_env(_compression_env()).budget.service_wall_seconds == configured_wall
    assert budget.SERVICE_WALL_SECONDS == 3941


def test_forged_assembly_marker_without_matching_walls_is_refused() -> None:
    forged = {
        **_assembled_env(),
        "NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_SERVICE_WALL_SECONDS": "3940",
    }
    with pytest.raises(budget.CompressionBudgetError, match="service wall"):
        budget.resolve_runner_budget(forged)


def test_partial_assembly_fields_without_marker_are_refused() -> None:
    with pytest.raises(budget.CompressionBudgetError, match="require the assembly marker"):
        budget.resolve_runner_budget(
            {"NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_WRAPPER_WALL_SECONDS": "5700"}
        )


def test_absent_cold_keys_succeed_for_single_compression_env() -> None:
    env = _compression_env()
    assert all(not name.startswith("NODE27_COLD_") for name in env)
    resolved = budget.resolve_compression_env(env)
    assert resolved.budget.service_wall_seconds == 3941
    assert resolved.budget.wrapper_wall_seconds == 3900


def test_unsafe_noncanonical_path_is_refused_before_parse(tmp_path: Path) -> None:
    relative = Path("compression.env")
    with pytest.raises(budget.CompressionBudgetError, match="absolute"):
        budget.read_compression_env_data(relative)
    with pytest.raises(budget.CompressionBudgetError, match="unavailable or unsafe"):
        budget.read_compression_env_data(tmp_path / "missing.env")

