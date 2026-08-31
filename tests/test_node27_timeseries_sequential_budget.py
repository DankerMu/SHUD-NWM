"""Public-contract tests for node-27's sequential compression/cold budget."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from packages.common import node27_timeseries_sequential_budget as budget
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
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "7842",
    }
    values.update(overrides)
    return values


def _cold_env(**overrides: str) -> dict[str, str]:
    values = {
        "NODE27_COLD_RESIDENCY_STATEMENT_TIMEOUT_MS": "3600000",
        "NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS": "3901",
        "NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS": "7842",
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


def test_resolver_accepts_default_pair_and_coherent_enlarged_catch_up() -> None:
    default = budget.resolve_lane_env_pair(_compression_env(), _cold_env())
    assert default.budget == budget.SequentialServiceBudget(3900, 3901, 7842, 40)
    assert default.compression_statement_timeout_ms == 3_600_000
    assert default.cold_statement_timeout_ms == 3_600_000
    assert default.compression_per_tick_bound == 4
    assert default.budget.compression_wrapper_wall_seconds == 3_600 + budget.COMPRESSION_CLEANUP_MARGIN_SECONDS

    catch_up = budget.resolve_lane_env_pair(
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS="5400000",
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="1",
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="5700",
            NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="9642",
        ),
        _cold_env(NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS="9642"),
    )
    assert catch_up.budget == budget.SequentialServiceBudget(5700, 3901, 9642, 40)
    assert catch_up.compression_statement_timeout_ms == 5_400_000
    assert catch_up.compression_per_tick_bound == 1


def test_canonical_compression_receipt_example_matches_the_cold_default_pair() -> None:
    example = json.loads(_COMPRESSION_RECEIPT_EXAMPLE.read_text(encoding="utf-8"))
    schema = json.loads(_COMPRESSION_RECEIPT_SCHEMA.read_text(encoding="utf-8"))

    jsonschema.validate(example, schema)
    resolved = budget.resolve_lane_env_pair(
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS=str(example["budget"]["compress_timeout_ms"]),
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=str(example["per_tick_bound"]),
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS=str(example["budget"]["wrapper_wall_seconds"]),
            NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS=str(example["budget"]["systemd_wall_seconds"]),
        ),
        _cold_env(
            NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS=str(example["budget"]["systemd_wall_seconds"])
        ),
    )

    assert example["budget"]["cleanup_margin_seconds"] == budget.COMPRESSION_CLEANUP_MARGIN_SECONDS
    assert resolved.budget == budget.SequentialServiceBudget(3900, 3901, 7842, 40)
    assert resolved.compression_statement_timeout_ms == 3_600_000
    assert resolved.cold_statement_timeout_ms == 3_600_000
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


@pytest.mark.parametrize(
    ("compression", "cold"),
    [
        (
            _compression_env(NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="7841"),
            _cold_env(NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS="7841"),
        ),
        (
            _compression_env(),
            _cold_env(NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS="7843"),
        ),
        (
            _compression_env(NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="3899"),
            _cold_env(),
        ),
        (
            _compression_env(
                NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS="5400000",
                NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="5699",
                NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="9642",
                NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="1",
            ),
            _cold_env(NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS="9642"),
        ),
        (
            _compression_env(
                NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS="5400000",
                NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="5700",
                NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="9642",
                NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="2",
            ),
            _cold_env(NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS="9642"),
        ),
    ],
)
def test_resolver_refuses_invalid_sequential_contract(
    compression: dict[str, str], cold: dict[str, str]
) -> None:
    with pytest.raises(budget.SequentialBudgetError):
        budget.resolve_lane_env_pair(compression, cold)


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
    compression = _compression_env(**{name: raw})
    with pytest.raises(budget.SequentialBudgetError):
        budget.resolve_lane_env_pair(compression, _cold_env())


def test_resolver_refuses_mirrored_compression_wall_disagreement() -> None:
    cold = _cold_env(NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="3901")
    with pytest.raises(budget.SequentialBudgetError):
        budget.resolve_lane_env_pair(_compression_env(), cold)


def test_preflight_reads_mode_0600_pair_and_emits_only_canonical_lane_wall(tmp_path: Path) -> None:
    compression = _write_env(tmp_path / "compression.env", _compression_env())
    cold = _write_env(tmp_path / "cold.env", _cold_env())

    result = _preflight(
        "--compression-env",
        str(compression),
        "--cold-env",
        str(cold),
        "--lane",
        "compression",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "3900\n"
    assert result.stderr == ""
    checked = _preflight(
        "--compression-env",
        str(compression),
        "--cold-env",
        str(cold),
        "--check",
    )
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout == ""
    assert checked.stderr == ""


@pytest.mark.parametrize("lane", ["compression", "cold"])
def test_preflight_assembly_output_is_bounded_canonical_nonsecret_integers(tmp_path: Path, lane: str) -> None:
    compression = _write_env(
        tmp_path / "compression.env",
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS="5400000",
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="1",
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS="5700",
            NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS="9642",
        ),
    )
    cold = _write_env(
        tmp_path / "cold.env",
        _cold_env(NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS="9642"),
    )

    result = _preflight(
        "--compression-env",
        str(compression),
        "--cold-env",
        str(cold),
        "--lane",
        lane,
        "--format",
        "assembly",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "5700,3901,9642,5400000,3600000,1\n"
    assert result.stderr == ""


@pytest.mark.parametrize("unsafe_kind", ["symlink", "wrong-mode", "directory", "oversized", "command", "duplicate"])
def test_preflight_refuses_unsafe_or_ambiguous_input_without_leaking_secrets(
    tmp_path: Path, unsafe_kind: str
) -> None:
    compression = _write_env(tmp_path / "compression.env", _compression_env())
    cold = _write_env(tmp_path / "cold.env", _cold_env())
    if unsafe_kind == "symlink":
        target = tmp_path / "compression-target.env"
        compression.rename(target)
        compression.symlink_to(target)
    elif unsafe_kind == "wrong-mode":
        compression.chmod(0o640)
    elif unsafe_kind == "directory":
        compression.unlink()
        compression.mkdir()
    elif unsafe_kind == "oversized":
        compression.write_bytes(b"A" * (128 * 1024))
        compression.chmod(0o600)
    elif unsafe_kind == "command":
        compression.write_text(
            f"DATABASE_URL='{_SECRET_DSN}'\n"
            "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS=$(id)\n",
            encoding="utf-8",
        )
        compression.chmod(0o600)
    elif unsafe_kind == "duplicate":
        compression.write_text(
            compression.read_text(encoding="utf-8")
            + "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS=3900\n",
            encoding="utf-8",
        )
        compression.chmod(0o600)
    else:
        raise AssertionError(unsafe_kind)

    result = _preflight(
        "--compression-env",
        str(compression),
        "--cold-env",
        str(cold),
        "--lane",
        "compression",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert _SECRET_DSN not in result.stderr
    assert "super-secret-password" not in result.stderr
    assert "very-secret-token" not in result.stderr


def test_preflight_rejects_relevant_whitespace_and_missing_compression_bound(tmp_path: Path) -> None:
    compression = _write_env(
        tmp_path / "compression.env",
        _compression_env(
            NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND="",
            NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS=" 3900",
        ),
    )
    cold = _write_env(tmp_path / "cold.env", _cold_env())

    result = _preflight(
        "--compression-env",
        str(compression),
        "--cold-env",
        str(cold),
        "--lane",
        "compression",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert _SECRET_DSN not in result.stderr


def test_preflight_requires_absolute_lane_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compression = _write_env(tmp_path / "compression.env", _compression_env())
    cold = _write_env(tmp_path / "cold.env", _cold_env())
    monkeypatch.chdir(tmp_path)

    result = _preflight(
        "--compression-env",
        compression.name,
        "--cold-env",
        str(cold),
        "--check",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert _SECRET_DSN not in result.stderr


def test_preflight_hides_secret_like_absolute_path_on_read_refusal(tmp_path: Path) -> None:
    cold = _write_env(tmp_path / "cold.env", _cold_env())
    secret_path = f"/missing/{_SECRET_DSN}"

    result = _preflight(
        "--compression-env",
        secret_path,
        "--cold-env",
        str(cold),
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
    cold_wall: str = "3901",
    service_wall: str = "9642",
    compression_timeout: str = "5400000",
    cold_timeout: str = "3600000",
    bound: str = "1",
) -> dict[str, str]:
    return {
        "NODE27_TIMESERIES_SEQUENTIAL_BUDGET_ASSEMBLED": "1",
        "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_WRAPPER_WALL_SECONDS": compression_wall,
        "NODE27_TIMESERIES_SEQUENTIAL_COLD_WRAPPER_WALL_SECONDS": cold_wall,
        "NODE27_TIMESERIES_SEQUENTIAL_SERVICE_WALL_SECONDS": service_wall,
        "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_STATEMENT_TIMEOUT_MS": compression_timeout,
        "NODE27_TIMESERIES_SEQUENTIAL_COLD_STATEMENT_TIMEOUT_MS": cold_timeout,
        "NODE27_TIMESERIES_SEQUENTIAL_COMPRESSION_PER_TICK_BOUND": bound,
    }


def test_assembled_runner_values_allow_coherent_catch_up_and_refuse_declaration_mismatch() -> None:
    resolved = budget.resolve_runner_budget(_assembled_env(), lane="compression")
    assert resolved.budget == budget.SequentialServiceBudget(5700, 3901, 9642, 40)
    mismatched = {
        **_assembled_env(),
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "5400000",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "1",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "9642",
    }
    with pytest.raises(budget.SequentialBudgetError, match="runner declarations"):
        budget.resolve_runner_budget(mismatched, lane="compression")


def test_partial_assembly_and_direct_nondefault_sibling_declaration_fail_closed() -> None:
    with pytest.raises(budget.SequentialBudgetError, match="incomplete"):
        budget.resolve_runner_budget(
            {"NODE27_TIMESERIES_SEQUENTIAL_BUDGET_ASSEMBLED": "1"}, lane="cold"
        )
    with pytest.raises(budget.SequentialBudgetError, match="full pair"):
        budget.resolve_runner_budget(
            {"NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "5700"}, lane="cold"
        )


@pytest.mark.parametrize(
    ("changed_path", "required_suites"),
    [
        (
            "infra/env/node27-timeseries-compression.example",
            {
                "tests/test_node27_timeseries_compression.py",
                "tests/test_node27_timeseries_sequential_budget.py",
                "tests/test_node27_timeseries_sequential_runner_config.py",
                "tests/test_node27_timeseries_sequential_wrappers.py",
            },
        ),
        (
            "infra/env/node27-cold-residency.example",
            {
                "tests/test_node27_cold_residency.py",
                "tests/test_node27_timeseries_sequential_budget.py",
                "tests/test_node27_timeseries_sequential_runner_config.py",
                "tests/test_node27_timeseries_sequential_wrappers.py",
            },
        ),
        (
            "docs/runbooks/tier-node27-timeseries-storage.md",
            {
                "tests/test_node27_timeseries_compression.py",
                "tests/test_node27_cold_residency.py",
                "tests/test_node27_timeseries_sequential_budget.py",
                "tests/test_node27_timeseries_sequential_runner_config.py",
                "tests/test_node27_timeseries_sequential_wrappers.py",
            },
        ),
        (
            "schemas/examples/timeseries_compression_receipt.example.json",
            {
                "tests/test_node27_timeseries_compression.py",
                "tests/test_node27_timeseries_sequential_budget.py",
            },
        ),
    ],
)
def test_pair_owned_paths_select_contract_suites(
    changed_path: str, required_suites: set[str]
) -> None:
    from scripts.select_ci_tests import select_tests

    selected = select_tests([changed_path], repo_root=_ROOT)

    assert selected
    assert required_suites <= set(selected)


def test_systemd_preflight_precedes_both_sequential_execstarts_and_matches_default() -> None:
    service = (_ROOT / "infra/systemd/nhms-node27-timeseries-compression.service").read_text(encoding="utf-8")
    lines = service.splitlines()
    preflight_index = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("ExecStartPre=") and "node27_timeseries_budget_preflight.py" in line
    )
    starts = [index for index, line in enumerate(lines) if line.startswith("ExecStart=")]
    assert len(starts) == 2
    assert preflight_index < starts[0] < starts[1]
    assert "--compression-env /home/nwm/NWM/infra/env/node27-timeseries-compression.env" in service
    assert "--cold-env /home/nwm/NWM/infra/env/node27-cold-residency.env --check" in service
    configured_wall = int(next(line for line in lines if line.startswith("TimeoutStartSec=")).split("=", 1)[1])
    assert configured_wall == 7842
    assert budget.resolve_lane_env_pair(_compression_env(), _cold_env()).budget.service_wall_seconds == configured_wall
