"""Publication, sidecar, and lock-order tests for cold residency."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_receipt import (
    ColdReceiptError,
    intent_path_for,
    publish_intent,
    publish_receipt,
    read_intent,
    read_public_receipt,
    remove_intent,
    validate_receipt,
)
from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
from packages.common.safe_fs import SafeFilesystemError
from scripts import node27_cold_residency as runner
from tests.cold_residency_fakes import FakeConnection, chunk, complete_relations
from tests.test_node27_cold_residency import _base_env, _ready

_ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
_INTENT = json.loads((_ROOT / "schemas/examples/timeseries_cold_residency_receipt.intent.example.json").read_text())
_TERMINAL = json.loads((_ROOT / "schemas/examples/timeseries_cold_residency_receipt.example.json").read_text())


class FreshUnknownConnection(FakeConnection):
    def __init__(self) -> None:
        super().__init__()
        self._unknown_origin_name = ""
        self._transitioned = False

    def transition_to_uncompressed_on_reload(self, origin_name: str) -> None:
        self._unknown_origin_name = origin_name

    def dispatch(self, sql: str, params: object):
        text = " ".join(sql.split())
        if (
            not self._transitioned
            and "timescaledb_information.chunks" in text
            and "range_end <=" not in text
            and isinstance(params, tuple)
            and params[-1] == self._unknown_origin_name
        ):
            current = self.chunks[self._unknown_origin_name]
            self.chunks[self._unknown_origin_name] = replace(
                current,
                compressed_oid=None,
                compressed_schema=None,
                compressed_name=None,
                is_compressed=False,
            )
            self._transitioned = True
        return super().dispatch(sql, params)


def test_intent_then_public_receipt_then_sidecar_removal(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    sidecar = intent_path_for(receipt_path)
    publish_intent(sidecar, _INTENT)
    assert sidecar.exists()
    assert sidecar.stat().st_mode & 0o777 == 0o600
    publish_receipt(receipt_path, _INTENT)
    assert json.loads(receipt_path.read_text())["outcome"] == "in_progress"
    publish_receipt(receipt_path, _TERMINAL)
    remove_intent(sidecar)
    assert not sidecar.exists()
    assert json.loads(receipt_path.read_text())["outcome"] == "clean"


def test_publication_failure_does_not_replay_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt_path = tmp_path / "receipt.json"
    sidecar = intent_path_for(receipt_path)
    publish_intent(sidecar, _INTENT)
    calls = {"count": 0}

    def boom(*_args: object, **_kwargs: object) -> None:
        calls["count"] += 1
        raise SafeFilesystemError("fsync failed", kind="indeterminate")

    monkeypatch.setattr("packages.common.compressed_chunk_cold_receipt.atomic_write_bytes_no_follow", boom)
    with pytest.raises(ColdReceiptError, match="publication"):
        publish_receipt(receipt_path, _TERMINAL)
    assert sidecar.exists()
    assert calls["count"] == 1


def test_symlink_receipt_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text("{}\n", encoding="utf-8")
    alias = tmp_path / "receipt.json"
    alias.symlink_to(target)
    with pytest.raises((ColdReceiptError, SafeFilesystemError)):
        publish_receipt(alias, _TERMINAL)


def test_stale_success_cannot_outrank_sidecar(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    sidecar = intent_path_for(receipt_path)
    publish_receipt(receipt_path, _TERMINAL)
    publish_intent(sidecar, _INTENT)
    parsed = read_intent(sidecar)
    assert parsed["outcome"] == "in_progress"
    assert json.loads(receipt_path.read_text())["outcome"] == "clean"


def test_mixed_unresolved_intent_blocks_new_selection(tmp_path: Path) -> None:
    env = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
        "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
    }
    args = runner._parser().parse_args(["--enforce"])
    config = runner.config_from_args(args, env)
    sidecar = intent_path_for(config.receipt_path)
    publish_intent(sidecar, _INTENT)
    connection = FakeConnection()
    item = chunk()
    connection.load_group(item, complete_relations(other_space="nhms_cold"))
    receipt = runner.run_tick(
        _ready(config),
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )
    assert receipt["recovery"]["blocked_new_selection"] is True
    assert receipt["recovery"]["classification"] in {"mixed", "unknown"}
    assert sidecar.exists()
    public = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert public["outcome"] == "failed"
    assert public["state"] == public["recovery"]["classification"]
    assert public["recovery"]["authority"] == "sidecar"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_unknown_unresolved_intent_replaces_stale_public_success(tmp_path: Path) -> None:
    env = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
        "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
    }
    args = runner._parser().parse_args(["--enforce"])
    config = runner.config_from_args(args, env)
    sidecar = intent_path_for(config.receipt_path)
    publish_intent(sidecar, _INTENT)
    publish_receipt(config.receipt_path, _TERMINAL)
    connection = FakeConnection()
    receipt = runner.run_tick(
        _ready(config),
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )
    assert receipt["recovery"]["classification"] == "unknown"
    assert sidecar.exists()
    public = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert public["outcome"] == "failed"
    assert public["state"] == "unknown"
    assert public["recovery"]["authority"] == "sidecar"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_pending_public_authority_survives_post_unlink_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
        "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
    }
    args = runner._parser().parse_args(["--enforce"])
    config = _ready(runner.config_from_args(args, env))
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations())

    def indeterminate_unlink(path: Path, **_kwargs: object) -> None:
        path.unlink(missing_ok=True)
        raise SafeFilesystemError("unlink completed but parent fsync failed", kind="indeterminate")

    monkeypatch.setattr(
        "packages.common.compressed_chunk_cold_receipt.unlink_no_follow_durable", indeterminate_unlink
    )

    def arm(_rank: int, _payload: object) -> None:
        return None

    config = config.__class__(**{**config.__dict__, "after_group_progress": arm})
    with pytest.raises(Exception) as raised:
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=lambda: connection,
            fetch_watermark=lambda: _NOW,
        )
    assert getattr(raised.value, "error_class", "") == "publication_indeterminate"
    sidecar = intent_path_for(config.receipt_path)
    assert not sidecar.exists()
    public = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert public["recovery"]["authority"] == "pending_cleanup"
    assert public["recovery"]["blocked_new_selection"] is True
    later = FakeConnection()
    later.load_group(
        chunk(origin_oid=11, origin_name="_hyper_1_2_chunk", compressed_oid=21, compressed_name="compress_21"),
        complete_relations(
            origin_oid=11,
            compressed_oid=21,
            origin_name="_hyper_1_2_chunk",
            compressed_name="compress_21",
        ),
    )
    recovered = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: later,
        fetch_watermark=lambda: _NOW,
    )
    assert not any("SET TABLESPACE" in sql for sql, _params in later.executed)
    assert recovered["recovery"]["blocked_new_selection"] is True


def test_pending_public_absent_sidecar_reconciles_then_closes_without_movement(tmp_path: Path) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args(["--enforce"]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    pending = json.loads(json.dumps(_INTENT))
    pending["recovery"] = {
        "classification": "complete_source",
        "sidecar_present": False,
        "replayed": False,
        "blocked_new_selection": True,
        "authority": "pending_cleanup",
        "cleanup_pending": True,
    }
    pending["outcome"] = "clean"
    pending["state"] = "complete_target"
    pending["recovery"]["classification"] = "complete_target"
    publish_receipt(config.receipt_path, pending)
    connection = FakeConnection()
    connection.load_group(
        chunk(),
        complete_relations(other_space="nhms_cold"),
    )

    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )

    assert receipt["recovery"]["authority"] in {"closed", "pending_cleanup"}
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_resurrected_sidecar_outranks_pending_public_authority(tmp_path: Path) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args(["--enforce"]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    pending = json.loads(json.dumps(_INTENT))
    pending["recovery"] = {
        "classification": "complete_source",
        "sidecar_present": False,
        "replayed": False,
        "blocked_new_selection": True,
        "authority": "pending_cleanup",
        "cleanup_pending": True,
    }
    pending["outcome"] = "clean"
    pending["state"] = "complete_source"
    publish_receipt(config.receipt_path, pending)
    publish_intent(config.intent_path, _INTENT)
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations(other_space="nhms_cold"))

    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )

    assert receipt["recovery"]["authority"] == "sidecar"
    assert config.intent_path.exists()
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_final_closed_replace_indeterminate_recovery_never_selects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args(["--enforce"]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    first = FakeConnection()
    first.load_group(chunk(), complete_relations())
    tick_module = __import__("packages.common.compressed_chunk_cold_tick", fromlist=["publish_receipt"])
    real_publish = tick_module.publish_receipt
    calls = {"closed": 0}

    def replace_then_fail(path: Path, payload: object) -> object:
        result = real_publish(path, payload)
        recovery = payload.get("recovery") if isinstance(payload, dict) else None
        if isinstance(recovery, dict) and recovery.get("authority") == "closed":
            calls["closed"] += 1
            if calls["closed"] == 1:
                raise ColdReceiptError(
                    "replace completed but durability is unproven",
                    error_class="publication_indeterminate",
                    stage="publish_receipt",
                )
        return result

    monkeypatch.setattr("packages.common.compressed_chunk_cold_tick.publish_receipt", replace_then_fail)
    with pytest.raises(ColdReceiptError) as raised:
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=lambda: first,
            fetch_watermark=lambda: _NOW,
        )
    assert raised.value.error_class == "publication_indeterminate"
    assert read_public_receipt(config.receipt_path)["recovery"]["authority"] == "closed"

    second = FakeConnection()
    second.load_group(chunk(), complete_relations())
    monkeypatch.setattr(
        "packages.common.compressed_chunk_cold_tick.ranked_candidates",
        lambda *_args, **_kwargs: [],
    )
    recovered = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: second,
        fetch_watermark=lambda: _NOW,
    )
    assert recovered["recovery"]["authority"] == "closed"
    assert not any("SET TABLESPACE" in sql for sql, _params in second.executed)


def test_closed_public_proof_failure_blocks_new_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args(["--enforce"]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    closed = json.loads(json.dumps(_TERMINAL))
    closed["recovery"] = {
        "classification": "complete_target",
        "sidecar_present": False,
        "replayed": False,
        "blocked_new_selection": False,
        "authority": "closed",
        "cleanup_pending": False,
    }
    publish_receipt(config.receipt_path, closed)
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations())

    def fail_closed_proof(*_args: object, **_kwargs: object) -> None:
        raise SafeFilesystemError("receipt parent changed", kind="indeterminate")

    monkeypatch.setattr(
        "packages.common.compressed_chunk_cold_receipt.read_bytes_durable_no_follow",
        fail_closed_proof,
    )
    with pytest.raises(ColdReceiptError) as raised:
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=lambda: connection,
            fetch_watermark=lambda: _NOW,
        )
    assert raised.value.error_class == "publication_indeterminate"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


@pytest.mark.parametrize(
    ("attribute", "off_pin"),
    [("server_version", "16.1"), ("timescaledb_version", "2.11.0")],
)
def test_main_off_pin_engine_replaces_stale_clean_without_movement_or_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    attribute: str,
    off_pin: str,
) -> None:
    env = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
        "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    receipt_path = Path(env["NODE27_COLD_RESIDENCY_RECEIPT_PATH"])
    publish_receipt(receipt_path, _TERMINAL)
    connection = FakeConnection()
    setattr(connection, attribute, off_pin)
    connection.load_group(chunk(), complete_relations())
    owned = {
        "lifecycle": os.open(tmp_path / "lifecycle.lock", os.O_RDWR | os.O_CREAT, 0o600),
        "lane": os.open(tmp_path / "lane.lock", os.O_RDWR | os.O_CREAT, 0o600),
    }

    monkeypatch.setattr(runner, "_observe_head", lambda *_args, **_kwargs: ("a" * 40, True, False))
    monkeypatch.setattr(runner, "acquire_timeseries_lifecycle_lock", lambda *_args, **_kwargs: owned["lifecycle"])
    monkeypatch.setattr(runner, "acquire_lock", lambda *_args, **_kwargs: owned["lane"])
    monkeypatch.setattr(runner, "release_timeseries_lifecycle_lock", lambda fd: os.close(fd))

    code = runner.main(["--enforce"], now_utc=_NOW, connect=lambda: connection)

    assert code == 1
    current = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert current["outcome"] not in {"clean", "no_op"}
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)
    assert "secretpw" not in capsys.readouterr().err
    for fd in owned.values():
        with pytest.raises(OSError):
            os.fstat(fd)


def test_closed_receipt_proof_allows_next_enforce_selection(tmp_path: Path) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args(["--enforce"]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    publish_receipt(config.receipt_path, _TERMINAL)
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations())

    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )

    assert receipt["outcome"] == "clean"
    assert any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_valid_sidecar_outranks_corrupt_public_receipt(tmp_path: Path) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args(["--enforce"]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    publish_intent(config.intent_path, _INTENT)
    config.receipt_path.write_text("{not-json", encoding="utf-8")
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations(other_space="nhms_cold"))

    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )

    assert receipt["recovery"]["authority"] == "sidecar"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_dry_run_sidecar_is_read_only_and_refuses_recovery(tmp_path: Path) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args([]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    publish_intent(config.intent_path, _INTENT)
    publish_receipt(config.receipt_path, _TERMINAL)
    before_intent = config.intent_path.read_bytes()
    before_public = config.receipt_path.read_bytes()
    connection = FakeConnection()

    with pytest.raises(ColdRuntimeError) as raised:
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=lambda: connection,
            fetch_watermark=lambda: _NOW,
        )

    assert raised.value.error_class == "recovery_required"
    assert config.intent_path.read_bytes() == before_intent
    assert config.receipt_path.read_bytes() == before_public
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_dry_run_pending_public_is_read_only_and_refuses_recovery(tmp_path: Path) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args([]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    pending = json.loads(json.dumps(_INTENT))
    pending["outcome"] = "clean"
    pending["state"] = "complete_source"
    pending["recovery"] = {
        "classification": "complete_source",
        "sidecar_present": False,
        "replayed": False,
        "blocked_new_selection": True,
        "authority": "pending_cleanup",
        "cleanup_pending": True,
    }
    publish_receipt(config.receipt_path, pending)
    before_public = config.receipt_path.read_bytes()
    connection = FakeConnection()

    with pytest.raises(ColdRuntimeError) as raised:
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=lambda: connection,
            fetch_watermark=lambda: _NOW,
        )

    assert raised.value.error_class == "recovery_required"
    assert config.receipt_path.read_bytes() == before_public
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_post_commit_mixed_reconciliation_retains_sidecar_authority(tmp_path: Path) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args(["--enforce"]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations())

    def make_mixed(owner: FakeConnection) -> None:
        relation = owner.relations[10]
        owner.relations[10] = relation.__class__(
            relation.oid,
            relation.schema,
            relation.name,
            relation.relkind,
            "pg_default",
            relation.bytes,
            relation.toast_oid,
            relation.heap_oid,
        )

    connection.commit_hook = make_mixed
    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )

    assert connection.committed is True
    assert any("SET TABLESPACE" in sql for sql, _params in connection.executed)
    assert receipt["outcome"] == "failed"
    assert receipt["state"] in {"mixed", "unknown"}
    assert receipt["recovery"]["classification"] == receipt["state"]
    assert receipt["recovery"]["authority"] == "sidecar"
    assert receipt["recovery"]["sidecar_present"] is True
    assert receipt["recovery"]["cleanup_pending"] is False
    assert config.intent_path.exists()
    intent = read_intent(config.intent_path)
    assert intent["selected"][0]["before"]
    assert intent["selected"][0]["before_parity"]
    assert intent["selected"][0]["capacity"]

    connection.executed.clear()
    recovered = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )
    assert recovered["recovery"]["authority"] == "sidecar"
    assert config.intent_path.exists()
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_pending_public_mixed_reconciliation_preserves_blocker(tmp_path: Path) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args(["--enforce"]),
            {
                "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
                "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
                "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
                "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
                "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
            },
        )
    )
    pending = json.loads(json.dumps(_INTENT))
    pending["outcome"] = "clean"
    pending["state"] = "complete_source"
    pending["recovery"] = {
        "classification": "complete_source",
        "sidecar_present": False,
        "replayed": False,
        "blocked_new_selection": True,
        "authority": "pending_cleanup",
        "cleanup_pending": True,
    }
    publish_receipt(config.receipt_path, pending)
    connection = FakeConnection()
    connection.load_group(chunk(), complete_relations(other_space="nhms_cold"))

    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )

    assert receipt["state"] in {"mixed", "unknown"}
    assert receipt["recovery"]["classification"] == receipt["state"]
    assert receipt["recovery"]["authority"] == "pending_cleanup"
    assert receipt["recovery"]["sidecar_present"] is False
    assert receipt["recovery"]["blocked_new_selection"] is True
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


@pytest.mark.parametrize(
    ("blocker", "preceding_outcome"),
    [
        ("mixed", None),
        ("unknown", None),
        ("mixed", "planned"),
        ("unknown", "planned"),
        ("mixed", "already_cold"),
        ("unknown", "already_cold"),
        ("mixed", "deferred"),
        ("unknown", "deferred"),
    ],
)
def test_dry_run_fresh_blocker_returns_schema_valid_failure_without_movement(
    tmp_path: Path,
    blocker: str,
    preceding_outcome: str | None,
) -> None:
    config = _ready(
        runner.config_from_args(
            runner._parser().parse_args([]),
            _base_env(
                tmp_path,
                override={"NODE27_COLD_RESIDENCY_PER_TICK_BOUND": "1" if preceding_outcome == "deferred" else "2"},
            ),
        )
    )
    connection = FreshUnknownConnection() if blocker == "unknown" else FakeConnection()
    if preceding_outcome == "deferred":
        planned = chunk(origin_oid=10, origin_name="_hyper_1_1_chunk")
        deferred = chunk(
            origin_oid=11,
            origin_name="_hyper_1_2_chunk",
            compressed_oid=21,
            compressed_name="compress_21",
        )
        connection.load_group(planned, complete_relations())
        connection.load_group(
            deferred,
            complete_relations(
                origin_oid=11,
                compressed_oid=21,
                origin_name="_hyper_1_2_chunk",
                compressed_name="compress_21",
            ),
        )
        blocker_item = chunk(
            origin_oid=12,
            origin_name="_hyper_1_3_chunk",
            compressed_oid=22,
            compressed_name="compress_22",
        )
    elif preceding_outcome is not None:
        preceding = chunk(origin_oid=10, origin_name="_hyper_1_1_chunk")
        connection.load_group(
            preceding,
            complete_relations(origin_space="nhms_cold" if preceding_outcome == "already_cold" else "pg_default"),
        )
        blocker_item = chunk(
            origin_oid=11,
            origin_name="_hyper_1_2_chunk",
            compressed_oid=21,
            compressed_name="compress_21",
            schema="met",
            name="forcing_station_timeseries",
        )
    else:
        blocker_item = chunk()
    connection.load_group(
        blocker_item,
        complete_relations(
            origin_oid=blocker_item.origin_oid,
            compressed_oid=blocker_item.compressed_oid or 20,
            origin_name=blocker_item.origin_name,
            compressed_name=blocker_item.compressed_name or "compress_hyper_2_2_chunk",
            other_space="nhms_cold" if blocker == "mixed" else None,
        ),
    )
    if blocker == "unknown":
        assert isinstance(connection, FreshUnknownConnection)
        connection.transition_to_uncompressed_on_reload(blocker_item.origin_name)

    receipt = runner.run_tick(
        config,
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )

    assert receipt["mode"] == "dry-run"
    assert receipt["outcome"] == "failed"
    assert receipt["state"] == blocker
    assert receipt["error"]["class"] == blocker
    assert receipt["selected"][-1]["outcome"] == "blocked"
    assert receipt["selected"][-1]["reconciliation"] == blocker
    assert receipt["selected"][-1]["error"]["class"] == blocker
    if preceding_outcome == "deferred":
        assert receipt["deferred"] and receipt["deferred"][0]["reason"] == "per_tick_bound"
    elif preceding_outcome is not None:
        assert receipt["selected"][0]["outcome"] == preceding_outcome
    assert validate_receipt(receipt) == receipt
    assert not any(
        token in sql
        for sql, _params in connection.executed
        for token in ("SET TABLESPACE", "decompress_chunk", "compress_chunk")
    )


@pytest.mark.parametrize("blocker", ["mixed", "unknown"])
def test_dry_run_main_replaces_stale_success_for_fresh_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    config = _ready(runner.config_from_args(runner._parser().parse_args([]), _base_env(tmp_path)))
    publish_receipt(config.receipt_path, _TERMINAL)
    connection = FreshUnknownConnection() if blocker == "unknown" else FakeConnection()
    connection.load_group(chunk(), complete_relations(other_space="nhms_cold" if blocker == "mixed" else None))
    if blocker == "unknown":
        assert isinstance(connection, FreshUnknownConnection)
        connection.transition_to_uncompressed_on_reload("_hyper_1_1_chunk")
    lifecycle_fd = os.open(tmp_path / "lifecycle.lock", os.O_RDWR | os.O_CREAT, 0o600)
    lane_fd = os.open(tmp_path / "lane.lock", os.O_RDWR | os.O_CREAT, 0o600)
    monkeypatch.setattr(runner, "config_from_args", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(runner, "_observe_head", lambda *_args, **_kwargs: ("a" * 40, True, False))
    monkeypatch.setattr(runner, "fetch_display_watermark", lambda *_args, **_kwargs: _NOW)
    monkeypatch.setattr(runner, "acquire_timeseries_lifecycle_lock", lambda *_args, **_kwargs: lifecycle_fd)
    monkeypatch.setattr(runner, "acquire_lock", lambda *_args, **_kwargs: lane_fd)
    monkeypatch.setattr(runner, "release_timeseries_lifecycle_lock", lambda fd: os.close(fd))

    assert runner.main([], now_utc=_NOW, connect=lambda: connection) == 1

    public = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert public["mode"] == "dry-run"
    assert public["outcome"] == "failed"
    assert public["state"] == blocker
    assert public["error"]["class"] == blocker
    assert validate_receipt(public) == public
    assert not any(
        token in sql
        for sql, _params in connection.executed
        for token in ("SET TABLESPACE", "decompress_chunk", "compress_chunk")
    )


def test_durable_public_read_refuses_path_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt_path = tmp_path / "receipt.json"
    publish_receipt(receipt_path, _TERMINAL)
    original = __import__("packages.common.safe_fs", fromlist=["_fsync_and_verify_parent"])._fsync_and_verify_parent

    def swap_then_fsync(parent_fd: int, parent_path: Path, target: Path, *, proof: str) -> None:
        replacement = tmp_path / "replacement.json"
        publish_receipt(replacement, _TERMINAL)
        os.replace(replacement, receipt_path)
        original(parent_fd, parent_path, target, proof=proof)

    monkeypatch.setattr("packages.common.safe_fs._fsync_and_verify_parent", swap_then_fsync)
    from packages.common.compressed_chunk_cold_receipt import read_public_receipt_durable

    with pytest.raises(ColdReceiptError) as raised:
        read_public_receipt_durable(receipt_path)
    assert raised.value.error_class == "publication_indeterminate"


def test_lifecycle_lock_is_acquired_before_lane_lock() -> None:
    source = (_ROOT / "scripts/node27_cold_residency.py").read_text(encoding="utf-8")
    lifecycle = source.index("acquire_timeseries_lifecycle_lock")
    lane = source.index("acquire_lock(config.lock_path)")
    assert lifecycle < lane
    compression = (_ROOT / "scripts/node27_timeseries_compression.py").read_text(encoding="utf-8")
    assert compression.index("acquire_timeseries_lifecycle_lock") < compression.index("acquire_lock(config.lock_path)")
    retention = (_ROOT / "scripts/node27_timeseries_retention.py").read_text(encoding="utf-8")
    assert retention.index("acquire_timeseries_lifecycle_lock") < retention.index("acquire_lock(config.lock_path)")
    replay = (_ROOT / "scripts/node27_timeseries_decompression_replay.py").read_text(encoding="utf-8")
    assert "timeseries_lifecycle_lock" in replay


def test_off_pin_engine_through_runner_publishes_non_success(tmp_path: Path) -> None:
    env = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
        "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
    }
    args = runner._parser().parse_args(["--enforce"])
    config = _ready(runner.config_from_args(args, env))
    connection = FakeConnection()
    connection.server_version = "16.1"
    connection.load_group(chunk(), complete_relations())
    with pytest.raises(Exception, match="PostgreSQL version"):
        runner.run_tick(
            config,
            now_utc=_NOW,
            head_sha="a" * 40,
            connect=lambda: connection,
            fetch_watermark=lambda: _NOW,
        )
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)
    if config.receipt_path.exists():
        public = json.loads(config.receipt_path.read_text(encoding="utf-8"))
        assert public["outcome"] not in {"clean", "no_op"}
