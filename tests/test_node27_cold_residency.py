"""CLI/wrapper/receipt tests for the node-27 cold-residency runner."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_receipt import intent_path_for, validate_receipt
from packages.common.node27_timeseries_lifecycle_lock import LIFECYCLE_LOCK_PATH
from scripts import node27_cold_residency as runner
from tests.cold_residency_fakes import (
    CUTOFF,
    FakeConnection,
    chunk,
    complete_relations,
)

_ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)


def _args(**overrides: object) -> argparse.Namespace:
    defaults = {"enforce": False, "receipt_path": None, "lock_path": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _base_env(tmp_path: Path, *, override: dict[str, str | None] | None = None) -> dict[str, str]:
    env: dict[str, str] = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
        "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
        "NODE27_COLD_RESIDENCY_PER_TICK_BOUND": "1",
    }
    if override:
        for key, value in override.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    return env


def test_missing_reserve_bytes_refuse_pre_connect(tmp_path: Path) -> None:
    with pytest.raises(runner.ColdResidencyConfigError, match="COLD_RESERVE_BYTES"):
        runner.config_from_args(
            _args(),
            _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": None}),
        )
    with pytest.raises(runner.ColdResidencyConfigError, match="WAL_RESERVE_BYTES"):
        runner.config_from_args(_args(), _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "0"}))


def test_example_template_has_no_reserve_defaults() -> None:
    text = (_ROOT / "infra/env/node27-cold-residency.example").read_text(encoding="utf-8")
    assert "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES=" in text
    assert not any(
        line.startswith("NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES=") and line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if not line.startswith("#")
    )
    assert not any(
        line.startswith("NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES=") and line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if not line.startswith("#")
    )


def test_budget_literals_are_mechanically_ordered() -> None:
    statement_wall = runner._ceil_div(runner._DEFAULT_STATEMENT_TIMEOUT_MS, 1000)
    assert runner._DEFAULT_WRAPPER_WALL_SECONDS > statement_wall + runner._CLEANUP_MARGIN_SECONDS
    sequential = (
        runner._DEFAULT_COMPRESSION_WRAPPER_WALL_SECONDS
        + runner._DEFAULT_WRAPPER_WALL_SECONDS
        + runner._SYSTEMD_MARGIN_SECONDS
    )
    assert sequential == 7_841
    assert runner._DEFAULT_SYSTEMD_WALL_SECONDS > sequential
    service = (_ROOT / "infra/systemd/nhms-node27-timeseries-compression.service").read_text(encoding="utf-8")
    assert "TimeoutStartSec=7842" in service
    wrapper = (_ROOT / "scripts/node27_cold_residency_once.sh").read_text(encoding="utf-8")
    assert "WALL=${NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS:-3901}" in wrapper
    compression_wrapper = (_ROOT / "scripts/node27_timeseries_compression_once.sh").read_text(encoding="utf-8")
    assert "WALL=${NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS:-3900}" in compression_wrapper


def test_no_new_cold_residency_timer_exists() -> None:
    systemd = _ROOT / "infra/systemd"
    assert not (systemd / "nhms-node27-timeseries-cold-residency.timer").exists()
    assert not (systemd / "nhms-node27-cold-residency.timer").exists()
    service = (systemd / "nhms-node27-timeseries-compression.service").read_text(encoding="utf-8")
    exec_lines = [line for line in service.splitlines() if line.startswith("ExecStart=")]
    assert exec_lines[0].endswith("node27_timeseries_compression_once.sh --enforce")
    assert exec_lines[1].endswith("node27_cold_residency_once.sh --enforce")
    timer = (systemd / "nhms-node27-timeseries-compression.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 04:25:00 UTC" in timer
    retention = (systemd / "nhms-node27-timeseries-retention.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 06:36:00 UTC" in retention


def test_lifecycle_lock_path_is_wired() -> None:
    assert str(LIFECYCLE_LOCK_PATH) == "/tmp/nhms-node27-timeseries-lifecycle.lock"
    source = (_ROOT / "scripts/node27_cold_residency.py").read_text(encoding="utf-8")
    assert "acquire_timeseries_lifecycle_lock" in source
    assert "acquire_lock(config.lock_path)" in source
    assert "os.environ.get(\"NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH\")" not in source
    assert "env.get(\"NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH\")" not in source


def test_connect_factory_uses_configured_statement_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []

    class _Cursor:
        def execute(self, sql: str, params: object = None) -> None:
            del params
            statements.append(sql)

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(runner, "_attributed_connect", lambda *_args, **_kwargs: _Connection())
    connect = runner._connect_factory("postgresql://user@127.0.0.1:55432/nhms", 3_600_000)
    connect()
    assert statements == ["SET statement_timeout = 3600000"]
    assert statements != ["SET statement_timeout = 60000"]


def test_receipt_lock_and_sidecar_must_be_disjoint(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="alias"):
        runner.config_from_args(
            _args(),
            _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "receipt.json")}),
        )


def test_service_wall_equality_is_refused(tmp_path: Path) -> None:
    with pytest.raises(runner.ColdResidencyConfigError, match="exceed"):
        runner.config_from_args(
            _args(),
            _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS": "7841"}),
        )


def test_lag_reuses_compression_contract_without_changing_default(tmp_path: Path) -> None:
    config = runner.config_from_args(_args(), _base_env(tmp_path))
    assert config.lag_seconds == 604800
    config = runner.config_from_args(
        _args(),
        _base_env(tmp_path, override={"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "172800"}),
    )
    assert config.lag_seconds == 172800


def _connect_factory(connection: FakeConnection):
    def factory() -> FakeConnection:
        return connection

    return factory


def _ready(config: runner.RunnerConfig) -> runner.RunnerConfig:
    return config.__class__(
        **{
            **config.__dict__,
            "cold_free_bytes": 10_000,
            "hot_free_bytes": 10_000,
            "expected_device_identity": "8:1",
            "inspect_target": lambda: {
                "container_name": "nhms-db",
                "container_bind": "/data/GHDC/nhms-cold-tablespace",
                "host_path": "/data/GHDC/nhms-cold-tablespace",
                "device_identity": "8:1",
            },
        }
    )


def test_dry_run_records_already_cold_without_consuming_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_PER_TICK_BOUND": "1"})
    config = _ready(runner.config_from_args(_args(), env))
    connection = FakeConnection()
    cold = chunk(origin_oid=10, origin_name="_hyper_1_1_chunk")
    hot = chunk(
        origin_oid=11,
        origin_name="_hyper_1_2_chunk",
        compressed_oid=21,
        compressed_name="compress_21",
        range_start=CUTOFF,
        range_end=CUTOFF + timedelta(days=0),
    )
    connection.load_group(cold, complete_relations(origin_space="nhms_cold"))
    connection.load_group(
        hot,
        complete_relations(
            origin_oid=11,
            compressed_oid=21,
            origin_name="_hyper_1_2_chunk",
            compressed_name="compress_21",
        ),
    )
    monkeypatch.setattr(runner, "_current_head_sha", lambda **_kwargs: "a" * 40)
    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect_factory(connection),
        fetch_watermark=lambda: _NOW,
    )
    outcomes = [item["outcome"] for item in receipt["selected"]]
    assert "already_cold" in outcomes
    assert receipt["deferred"] or "planned" in outcomes


def test_exact_cutoff_is_selected(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    config = runner.config_from_args(_args(), env)
    connection = FakeConnection()
    item = chunk(range_end=CUTOFF)
    connection.load_group(item, complete_relations())
    receipt = runner.run_tick(
        _ready(config),
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect_factory(connection),
        fetch_watermark=lambda: _NOW,
    )
    assert receipt["selected"]
    assert receipt["selected"][0]["durable"]["range_end"] == "2026-07-04T12:00:00Z"


def test_empty_selection_is_no_op(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    config = runner.config_from_args(_args(), env)
    connection = FakeConnection()
    receipt = runner.run_tick(
        _ready(config),
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect_factory(connection),
        fetch_watermark=lambda: _NOW,
    )
    assert receipt["outcome"] == "no_op"
    assert receipt["selected"] == []


def test_redaction_strips_dsn_from_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setattr(runner, "_observe_head", lambda *_args, **_kwargs: (None, False, False))
    code = runner.main([])
    assert code == 1
    err = capsys.readouterr().err
    assert "secretpw" not in err
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["class"]
    assert payload["stage"]


def test_intent_sidecar_is_authoritative_on_startup(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    config = runner.config_from_args(_args(enforce=True), env)
    intent = json.loads((_ROOT / "schemas/examples/timeseries_cold_residency_receipt.intent.example.json").read_text())
    intent_path = intent_path_for(config.receipt_path)
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    intent_path.chmod(0o600)
    (config.receipt_path).write_text(
        (_ROOT / "schemas/examples/timeseries_cold_residency_receipt.example.json").read_text(),
        encoding="utf-8",
    )
    connection = FakeConnection()
    item = chunk()
    connection.load_group(item, complete_relations(origin_space="nhms_cold"))
    receipt = runner.run_tick(
        _ready(config),
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect_factory(connection),
        fetch_watermark=lambda: _NOW,
    )
    assert receipt["recovery"]["blocked_new_selection"] is True or receipt["outcome"] in {"clean", "no_op"}
    if receipt["outcome"] not in {"clean", "no_op"}:
        assert intent_path.exists()


def test_corrupt_intent_is_not_overwritten(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    config = runner.config_from_args(_args(enforce=True), env)
    intent_path = intent_path_for(config.receipt_path)
    intent_path.write_text("{not-json", encoding="utf-8")
    intent_path.chmod(0o600)
    connection = FakeConnection()
    with pytest.raises(Exception, match="corrupt|unreadable"):
        runner.run_tick(
            _ready(config),
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=_connect_factory(connection),
            fetch_watermark=lambda: _NOW,
        )
    assert intent_path.read_text(encoding="utf-8") == "{not-json"


def test_ci_selector_owns_wrapper_schema_and_shared_modules() -> None:
    from scripts.select_ci_tests import select_tests

    selected = select_tests(["scripts/node27_cold_residency_once.sh"], repo_root=_ROOT)
    assert "tests/test_node27_cold_residency.py" in selected
    assert "tests/test_node27_wrapper_pythonpath.py" in selected
    selected = select_tests(["packages/common/node27_timeseries_lifecycle_lock.py"], repo_root=_ROOT)
    assert "tests/test_node27_timeseries_lifecycle_lock.py" in selected
    selected = select_tests(["infra/systemd/nhms-node27-timeseries-compression.service"], repo_root=_ROOT)
    assert "tests/test_node27_cold_residency.py" in selected


def test_no_hypertable_attach_sql_in_production_modules() -> None:
    for relative in (
        "packages/common/compressed_chunk_cold_runtime.py",
        "packages/common/compressed_chunk_cold_runtime_catalog.py",
        "scripts/node27_cold_residency.py",
    ):
        text = (_ROOT / relative).read_text(encoding="utf-8")
        assert "attach_tablespace" not in text
        assert "from packages.common.compressed_chunk_cold_probe" not in text
        assert "import packages.common.compressed_chunk_cold_probe" not in text


def test_schema_examples_are_valid() -> None:
    for name in (
        "timeseries_cold_residency_receipt.example.json",
        "timeseries_cold_residency_receipt.noop.example.json",
        "timeseries_cold_residency_receipt.intent.example.json",
        "timeseries_cold_residency_receipt.partial.example.json",
        "timeseries_cold_residency_receipt.error.example.json",
    ):
        payload = json.loads((_ROOT / "schemas/examples" / name).read_text(encoding="utf-8"))
        validate_receipt(payload)


def test_example_template_requires_unassigned_device_identity() -> None:
    text = (_ROOT / "infra/env/node27-cold-residency.example").read_text(encoding="utf-8")
    assert "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=" in text
    assigned = [
        line
        for line in text.splitlines()
        if line.startswith("NODE27_COLD_RESIDENCY_DEVICE_IDENTITY=") and line.split("=", 1)[1].strip()
    ]
    assert assigned == []
    assert "fixed production defaults" in text
    runbook = (_ROOT / "docs/runbooks/tier-node27-timeseries-storage.md").read_text(encoding="utf-8")
    assert "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY" in runbook


def test_ci_selector_owns_timing_module() -> None:
    from scripts.select_ci_tests import select_tests

    selected = select_tests(["packages/common/compressed_chunk_cold_runtime_timing.py"], repo_root=_ROOT)
    assert "tests/test_compressed_chunk_cold_runtime.py" in selected
    assert "tests/test_node27_cold_residency_phase2.py" in selected


def test_production_expected_container_name_is_fixed_nhms_db(tmp_path: Path) -> None:
    from packages.common.compressed_chunk_cold_runtime import LIVE_CONTAINER_NAME
    from packages.common.compressed_chunk_cold_tick import runtime_config

    env = _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_CONTAINER_NAME": "evil"})
    config = runner.config_from_args(_args(), env)
    assert LIVE_CONTAINER_NAME == "nhms-db"
    assert config.expected_container_name == "nhms-db"
    assert runtime_config(config).expected_container_name == "nhms-db"
    source = (_ROOT / "scripts/node27_cold_residency.py").read_text(encoding="utf-8")
    assert "NODE27_COLD_RESIDENCY_CONTAINER_NAME" not in source
