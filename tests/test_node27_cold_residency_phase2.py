"""Discriminating tests for Issue #1893 Phase 2 remaining checklist."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_receipt import (
    ColdReceiptError,
    intent_path_for,
    publish_intent,
    publish_receipt,
    read_intent,
    remove_intent,
    sidecar_status,
)
from packages.common.safe_fs import SafeFilesystemError
from scripts import node27_cold_residency as runner
from tests.cold_residency_fakes import FakeConnection, chunk, complete_relations
from tests.test_node27_cold_residency import _args, _base_env, _ready

_ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
_INTENT = json.loads((_ROOT / "schemas/examples/timeseries_cold_residency_receipt.intent.example.json").read_text())
_TERMINAL = json.loads((_ROOT / "schemas/examples/timeseries_cold_residency_receipt.example.json").read_text())


def _connect(connection: FakeConnection):
    def factory() -> FakeConnection:
        return connection

    return factory


def _owned_fds(tmp_path: Path) -> dict[str, int]:
    return {
        "lifecycle": os.open(tmp_path / "life.fd", os.O_RDWR | os.O_CREAT, 0o600),
        "lane": os.open(tmp_path / "lane.fd", os.O_RDWR | os.O_CREAT, 0o600),
    }


def _install_owned_locks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, int]:
    owned = _owned_fds(tmp_path)
    monkeypatch.setattr(runner, "acquire_timeseries_lifecycle_lock", lambda *_args, **_kwargs: owned["lifecycle"])
    monkeypatch.setattr(runner, "acquire_lock", lambda *_args, **_kwargs: owned["lane"])

    def release(fd: int) -> None:
        os.close(fd)
        if fd == owned["lifecycle"]:
            owned["lifecycle"] = -1

    monkeypatch.setattr(runner, "release_timeseries_lifecycle_lock", release)
    return owned


def test_intent_replacement_sibling_is_unknown(tmp_path: Path) -> None:
    config = _ready(runner.config_from_args(_args(enforce=True), _base_env(tmp_path)))
    sidecar = intent_path_for(config.receipt_path)
    publish_intent(sidecar, _INTENT)
    connection = FakeConnection()
    item = chunk(compressed_oid=99, compressed_name="compress_replaced")
    connection.load_group(
        item,
        complete_relations(compressed_oid=99, compressed_name="compress_replaced"),
    )
    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect(connection),
        fetch_watermark=lambda: _NOW,
    )
    assert receipt["recovery"]["classification"] == "unknown"
    assert receipt["recovery"]["blocked_new_selection"] is True
    assert sidecar_status(sidecar) == "present"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)
    public = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert public["outcome"] == "failed"
    assert public["recovery"]["authority"] == "sidecar"


def test_intent_without_before_evidence_fails_schema(tmp_path: Path) -> None:
    sidecar = tmp_path / ".receipt.json.intent"
    payload = json.loads(json.dumps(_INTENT))
    payload["selected"][0].pop("before", None)
    payload["selected"][0].pop("before_parity", None)
    payload["selected"][0].pop("capacity", None)
    with pytest.raises((ColdReceiptError, Exception)):
        publish_intent(sidecar, payload)
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    sidecar.chmod(0o600)
    with pytest.raises(ColdReceiptError, match="corrupt|unreadable|missing"):
        read_intent(sidecar)


def test_second_group_refuses_when_free_space_shrinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_PER_TICK_BOUND": "2"})
    config = runner.config_from_args(_args(enforce=True), env)
    samples = {"cold": [10_000, 1]}

    def shrinking(path: str) -> int:
        if path == config.expected_host_path:
            remaining = samples["cold"]
            return remaining.pop(0) if remaining else 1
        return 10_000

    config = config.__class__(
        **{
            **config.__dict__,
            "cold_free_bytes": None,
            "hot_free_bytes": 10_000,
            "inspect_target": lambda: {
                "container_name": "nhms-db",
                "container_bind": "/data/GHDC/nhms-cold-tablespace",
                "host_path": "/data/GHDC/nhms-cold-tablespace",
                "device_identity": "8:1",
            },
            "expected_device_identity": "8:1",
        }
    )
    monkeypatch.setattr("packages.common.compressed_chunk_cold_tick.device_free_bytes", shrinking)
    connection = FakeConnection()
    first = chunk(origin_oid=10, origin_name="_hyper_1_1_chunk")
    second = chunk(
        origin_oid=11,
        origin_name="_hyper_1_2_chunk",
        compressed_oid=21,
        compressed_name="compress_21",
    )
    connection.load_group(first, complete_relations())
    connection.load_group(
        second,
        complete_relations(
            origin_oid=11,
            compressed_oid=21,
            origin_name="_hyper_1_2_chunk",
            compressed_name="compress_21",
        ),
    )
    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect(connection),
        fetch_watermark=lambda: _NOW,
    )
    outcomes = [item["outcome"] for item in receipt["selected"]]
    assert "migrated" in outcomes
    assert "refused" in outcomes
    moved = [sql for sql, _params in connection.executed if "SET TABLESPACE" in sql]
    assert moved
    assert all("_hyper_1_2_chunk" not in sql for sql in moved)


def test_intent_preimage_drift_before_runtime_refuses_movement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _base_env(tmp_path)
    config = _ready(runner.config_from_args(_args(enforce=True), env))
    connection = FakeConnection()
    item = chunk()
    connection.load_group(item, complete_relations())
    original_inspect = __import__(
        "packages.common.compressed_chunk_cold_tick", fromlist=["inspect_residency_group"]
    ).inspect_residency_group
    calls = {"n": 0}

    def inspect_then_replace(*args: object, **kwargs: object):
        observation = original_inspect(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            extra = complete_relations(extra_indexes=1)[-1]
            connection.relations[extra.oid] = extra
        return observation

    monkeypatch.setattr("packages.common.compressed_chunk_cold_tick.inspect_residency_group", inspect_then_replace)
    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect(connection),
        fetch_watermark=lambda: _NOW,
    )
    assert receipt["outcome"] == "failed"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)
    sidecar = intent_path_for(config.receipt_path)
    if sidecar.exists():
        intent = json.loads(sidecar.read_text(encoding="utf-8"))
        assert intent["selected"][0]["before"]["compressed"]["oid"] == 20


def test_lock_refusal_does_not_fabricate_observed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runner, "_observe_head", lambda *_args, **_kwargs: ("a" * 40, True, False))
    monkeypatch.setattr(runner, "acquire_timeseries_lifecycle_lock", lambda *_args, **_kwargs: None)
    code = runner.main([])
    assert code == 0
    receipt = json.loads(Path(env["NODE27_COLD_RESIDENCY_RECEIPT_PATH"]).read_text())
    assert receipt["outcome"] == "refused_lock"
    assert receipt["cluster"]["observed"] is False
    assert receipt["cluster"]["server_version"] is None
    assert receipt["target"]["observed"] is False
    assert receipt["target"]["device_identity"] is None
    assert receipt["inventory"]["observed"] is False
    assert receipt["inventory"]["digest"] is None


def test_stale_success_is_replaced_on_watermark_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from packages.common.display_watermark import DisplayWatermarkError

    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    receipt_path = Path(env["NODE27_COLD_RESIDENCY_RECEIPT_PATH"])
    publish_receipt(receipt_path, _TERMINAL)
    monkeypatch.setattr(runner, "_observe_head", lambda *_args, **_kwargs: ("a" * 40, True, False))
    owned = _install_owned_locks(monkeypatch, tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise DisplayWatermarkError("missing")

    monkeypatch.setattr(runner, "run_tick", boom)
    code = runner.main(["--enforce"])
    assert code == 1
    current = json.loads(receipt_path.read_text())
    assert current["outcome"] != "clean"
    assert current["error"]["class"] == "watermark"
    with pytest.raises(OSError):
        os.fstat(owned["lifecycle"])
    with pytest.raises(OSError):
        os.fstat(owned["lane"])


def test_missing_reserve_after_receipt_path_publishes_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": None})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES", raising=False)
    receipt_path = Path(env["NODE27_COLD_RESIDENCY_RECEIPT_PATH"])
    publish_receipt(receipt_path, _TERMINAL)
    code = runner.main([])
    assert code == 1
    current = json.loads(receipt_path.read_text())
    assert current["outcome"] == "refused_config"
    assert current["cluster"]["observed"] is False
    assert current["config_observed"] is False
    assert current["lag_seconds"] is None
    assert current["per_tick_bound"] is None
    assert current["max_members"] is None
    assert current["budget"] is None
    err = capsys.readouterr().err
    assert "secretpw" not in err


def test_invalid_budget_after_safe_paths_overwrites_stale_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS": "7841"})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    receipt_path = Path(env["NODE27_COLD_RESIDENCY_RECEIPT_PATH"])
    publish_receipt(receipt_path, _TERMINAL)
    code = runner.main([])
    assert code == 1
    current = json.loads(receipt_path.read_text())
    assert current["outcome"] == "refused_config"
    assert current["config_observed"] is False
    assert current["budget"] is None


def test_missing_database_url_after_safe_paths_overwrites_stale_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _base_env(tmp_path, override={"DATABASE_URL": None})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    receipt_path = Path(env["NODE27_COLD_RESIDENCY_RECEIPT_PATH"])
    publish_receipt(receipt_path, _TERMINAL)
    code = runner.main([])
    assert code == 1
    current = json.loads(receipt_path.read_text())
    assert current["outcome"] == "refused_config"
    assert current["config_observed"] is False
    assert current["lag_seconds"] is None


def test_receipt_lock_alias_with_missing_reserve_is_stderr_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt_path = tmp_path / "receipt.json"
    env = _base_env(
        tmp_path,
        override={
            "NODE27_COLD_RESIDENCY_LOCK_PATH": str(receipt_path),
            "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": None,
        },
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES", raising=False)
    publish_receipt(receipt_path, _TERMINAL)
    code = runner.main([])
    assert code == 1
    assert json.loads(receipt_path.read_text())["outcome"] == "clean"
    err = capsys.readouterr().err
    assert "secretpw" not in err
    assert "alias" in err or "class" in err


def test_lifecycle_env_override_with_missing_reserve_is_stderr_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt_path = tmp_path / "receipt.json"
    env = _base_env(
        tmp_path,
        override={
            "NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH": str(receipt_path),
            "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": None,
        },
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES", raising=False)
    publish_receipt(receipt_path, _TERMINAL)
    code = runner.main([])
    assert code == 1
    assert json.loads(receipt_path.read_text())["outcome"] == "clean"
    err = capsys.readouterr().err
    assert "secretpw" not in err
    assert "cannot override" in err or "class" in err


def test_migrated_observation_without_timing_fails_schema() -> None:
    payload = json.loads(json.dumps(_TERMINAL))
    payload["selected"][0].pop("timing", None)
    with pytest.raises((ColdReceiptError, Exception)):
        from packages.common.compressed_chunk_cold_receipt import validate_receipt

        validate_receipt(payload)


def test_unsafe_receipt_path_is_stderr_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NODE27_COLD_RESIDENCY_RECEIPT_PATH", raising=False)
    code = runner.main([])
    assert code == 1
    err = capsys.readouterr().err
    assert "receipt path" in err or "class" in err


def test_dangling_symlink_sidecar_blocks_overwrite(tmp_path: Path) -> None:
    config = _ready(runner.config_from_args(_args(enforce=True), _base_env(tmp_path)))
    sidecar = intent_path_for(config.receipt_path)
    sidecar.symlink_to(tmp_path / "missing-intent.json")
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations())
    with pytest.raises(ColdReceiptError, match="symlink"):
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=_connect(connection),
            fetch_watermark=lambda: _NOW,
        )
    assert sidecar.is_symlink()


def test_durable_unlink_fsync_failure_is_indeterminate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sidecar = tmp_path / ".receipt.json.intent"
    publish_intent(sidecar, _INTENT)

    def boom(_fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr("packages.common.safe_fs.os.fsync", boom)
    with pytest.raises(Exception) as raised:
        remove_intent(sidecar)
    assert getattr(raised.value, "error_class", "") == "publication_indeterminate"


def test_durable_unlink_parent_identity_failure_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = tmp_path / ".receipt.json.intent"
    publish_intent(sidecar, _INTENT)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise SafeFilesystemError("parent changed", kind="unsafe")

    monkeypatch.setattr("packages.common.safe_fs._verify_fd_matches_path", boom)
    with pytest.raises(Exception) as raised:
        remove_intent(sidecar)
    assert getattr(raised.value, "error_class", "") == "publication_indeterminate"


class ProgressCrash(RuntimeError):
    pass


def _two_group_config(tmp_path: Path, **overrides: object) -> runner.RunnerConfig:
    env = _base_env(tmp_path, override={"NODE27_COLD_RESIDENCY_PER_TICK_BOUND": "2"})
    config = runner.config_from_args(_args(enforce=True), env)
    payload = {
        **config.__dict__,
        "cold_free_bytes": 10_000,
        "hot_free_bytes": 10_000,
        "inspect_target": lambda: {
            "container_name": "nhms-db",
            "container_bind": "/data/GHDC/nhms-cold-tablespace",
            "host_path": "/data/GHDC/nhms-cold-tablespace",
            "device_identity": "8:1",
        },
        "expected_device_identity": "8:1",
    }
    payload.update(overrides)
    return config.__class__(**payload)


def _install_shrinking_free(monkeypatch: pytest.MonkeyPatch, samples: list[int]) -> None:
    remaining = list(samples)

    def shrinking(path: str) -> int:
        if path.endswith("nhms-cold-tablespace"):
            return remaining.pop(0) if remaining else samples[-1]
        return 10_000

    monkeypatch.setattr("packages.common.compressed_chunk_cold_tick.device_free_bytes", shrinking)


def _load_two_groups(connection: FakeConnection) -> None:
    first = chunk(origin_oid=10, origin_name="_hyper_1_1_chunk")
    second = chunk(
        origin_oid=11,
        origin_name="_hyper_1_2_chunk",
        compressed_oid=21,
        compressed_name="compress_21",
    )
    connection.load_group(first, complete_relations())
    connection.load_group(
        second,
        complete_relations(
            origin_oid=11,
            compressed_oid=21,
            origin_name="_hyper_1_2_chunk",
            compressed_name="compress_21",
            toast_bias=1000,
        ),
    )
    connection.compression_bytes["_hyper_1_1_chunk"] = 1000
    connection.compression_bytes["_hyper_1_2_chunk"] = 2500


def test_group1_progress_is_durable_before_group2_and_startup_classifies_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crashed = {"rank": None}

    def crash(rank: int, payload: object) -> None:
        crashed["rank"] = rank
        raise ProgressCrash("stop after group1 progress")

    _install_shrinking_free(monkeypatch, [10_000, 8_000])
    config = _two_group_config(tmp_path, after_group_progress=crash, cold_free_bytes=None, hot_free_bytes=10_000)
    connection = FakeConnection()
    _load_two_groups(connection)
    with pytest.raises(ProgressCrash):
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=_connect(connection),
            fetch_watermark=lambda: _NOW,
        )
    sidecar = intent_path_for(config.receipt_path)
    intent = read_intent(sidecar)
    assert crashed["rank"] == 0
    assert intent["selected"][0]["outcome"] == "migrated"
    assert intent["selected"][0]["reconciliation"] == "complete_target"
    assert intent["selected"][1]["outcome"] == "planned"
    assert intent["selected"][1]["before"]["compressed"]["oid"] == 21
    first_moves = [sql for sql, _params in connection.executed if "SET TABLESPACE" in sql]
    assert any("_hyper_1_1_chunk" in sql for sql in first_moves)
    assert all("_hyper_1_2_chunk" not in sql for sql in first_moves)
    connection.executed.clear()
    recovered = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=_connect(connection),
        fetch_watermark=lambda: _NOW,
    )
    assert recovered["outcome"] == "clean"
    assert recovered["recovery"]["replayed"] is False
    classes = [item["reconciliation"] for item in recovered["selected"]]
    assert classes[0] == "complete_target"
    assert classes[1] == "complete_source"
    assert recovered["selected"][0]["before"]["compressed"]["oid"] == 20
    assert recovered["selected"][1]["before"]["compressed"]["oid"] == 21
    assert sidecar_status(sidecar) == "absent"
    assert recovered["recovery"]["authority"] == "closed"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)
    first_capacity = intent["selected"][0]["capacity"]
    second_capacity = intent["selected"][1]["capacity"]
    assert first_capacity["before_compression_total_bytes"] != second_capacity["before_compression_total_bytes"]
    assert first_capacity["cold_free_bytes"] != second_capacity["cold_free_bytes"]
    assert first_capacity["cold_headroom_bytes"] != second_capacity["cold_headroom_bytes"]
    recovered_first = recovered["selected"][0]["capacity"]
    recovered_second = recovered["selected"][1]["capacity"]
    assert recovered_first["before_compression_total_bytes"] == first_capacity["before_compression_total_bytes"]
    assert recovered_second["before_compression_total_bytes"] == second_capacity["before_compression_total_bytes"]


def test_copying_group1_capacity_onto_group2_reddens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    crashed = {"rank": None}

    def crash(rank: int, payload: object) -> None:
        crashed["rank"] = rank
        raise ProgressCrash("stop after group1 progress")

    _install_shrinking_free(monkeypatch, [10_000, 8_000])
    config = _two_group_config(tmp_path, after_group_progress=crash, cold_free_bytes=None, hot_free_bytes=10_000)
    connection = FakeConnection()
    _load_two_groups(connection)
    with pytest.raises(ProgressCrash):
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=_connect(connection),
            fetch_watermark=lambda: _NOW,
        )
    intent = read_intent(intent_path_for(config.receipt_path))
    first = intent["selected"][0]["capacity"]
    second = intent["selected"][1]["capacity"]
    assert first != second
    with pytest.raises(AssertionError):
        assert first == second


def test_progress_publication_failure_prevents_group2_movement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from packages.common.compressed_chunk_cold_receipt import publish_intent as real_publish

    def fail_progress(path: Path, payload: object, **_kwargs: object) -> object:
        selected = payload.get("selected") if isinstance(payload, dict) else []
        if any(isinstance(item, dict) and item.get("outcome") == "migrated" for item in selected):
            raise ColdReceiptError("progress publication failed")
        return real_publish(path, payload)

    monkeypatch.setattr("packages.common.compressed_chunk_cold_tick.publish_intent", fail_progress)
    config = _two_group_config(tmp_path)
    connection = FakeConnection()
    _load_two_groups(connection)
    with pytest.raises(ColdReceiptError, match="progress"):
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=_connect(connection),
            fetch_watermark=lambda: _NOW,
        )
    moved = [sql for sql, _params in connection.executed if "SET TABLESPACE" in sql]
    assert any("_hyper_1_1_chunk" in sql for sql in moved)
    assert all("_hyper_1_2_chunk" not in sql for sql in moved)
