"""Tests for the M24 baseline emitter (no real DB / HTTP required)."""

from __future__ import annotations

import json
import stat

from scripts import m24_emit_baseline as emit
from services.m24_live.receipt import validate_receipt

EXPECTED_STAGES = {
    "db_identity",
    "active_models",
    "hydro_run_gfs",
    "hydro_run_ifs",
    "state_snapshot",
    "gateway_health",
    "provenance_claim",
}


class _FakeCursor:
    def __init__(self, responses):
        self._responses = responses
        self._last = None

    def execute(self, sql, params=None):
        text = " ".join(sql.split()).lower()
        if "core.model_instance" in text:
            self._last = ("scalar", 7)
        elif "hydro.hydro_run" in text:
            source = (params or ["?"])[0]
            self._last = ("rows", self._responses["hydro_run"].get(source.lower(), []))
        elif "hydro.state_snapshot" in text:
            self._last = ("scalar", 0)
        else:
            self._last = ("scalar", 0)

    def fetchone(self):
        kind, value = self._last
        assert kind == "scalar"
        return (value,)

    def fetchall(self):
        kind, value = self._last
        assert kind == "rows"
        return value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, responses):
        self._responses = responses
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._responses)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _fake_connect_factory(responses):
    def _connect(database_url):
        return _FakeConnection(responses)

    return _connect


def _fake_http_get_ok(url):
    return 200, {"healthy": True, "binaries": ["sbatch", "squeue", "sacct", "scancel"]}


def _responses():
    return {
        "hydro_run": {
            "gfs": [("published", 3), ("parsed", 1)],
            "ifs": [("published", 2)],
        }
    }


def test_baseline_receipt_validates_and_has_all_stages():
    receipt = emit.build_baseline_receipt(
        "m24-base-001",
        database_url="postgresql://u:pw@db:5432/nhms",
        gateway_url="http://127.0.0.1:8081",
        connect=_fake_connect_factory(_responses()),
        http_get=_fake_http_get_ok,
    )
    validate_receipt(receipt)
    assert receipt["status"] == "PASS"
    assert receipt["execution_mode"] == "deterministic"
    assert receipt["live_proof_accepted"] is False
    stage_names = {stage["stage"] for stage in receipt["stages"]}
    assert stage_names == EXPECTED_STAGES


def test_baseline_counts_are_collected():
    receipt = emit.build_baseline_receipt(
        "m24-base-002",
        database_url="postgresql://u:pw@db:5432/nhms",
        gateway_url="http://127.0.0.1:8081",
        connect=_fake_connect_factory(_responses()),
        http_get=_fake_http_get_ok,
    )
    by_stage = {stage["stage"]: stage for stage in receipt["stages"]}
    assert by_stage["active_models"]["counts"]["active_model_count"] == 7
    assert by_stage["hydro_run_gfs"]["counts"]["by_status"] == {"published": 3, "parsed": 1}
    assert by_stage["hydro_run_gfs"]["counts"]["total"] == 4
    assert by_stage["hydro_run_ifs"]["counts"]["by_status"] == {"published": 2}
    assert by_stage["state_snapshot"]["counts"]["state_snapshot_count"] == 0
    assert by_stage["gateway_health"]["status"] == "PASS"
    assert by_stage["gateway_health"]["counts"]["status_code"] == 200


def test_db_dsn_password_never_in_identity_stage():
    receipt = emit.build_baseline_receipt(
        "m24-base-003",
        database_url="postgresql://alice:topsecret@db.example:5432/nhms",
        gateway_url="http://127.0.0.1:8081",
        connect=_fake_connect_factory(_responses()),
        http_get=_fake_http_get_ok,
    )
    serialized = json.dumps(receipt)
    assert "topsecret" not in serialized
    by_stage = {stage["stage"]: stage for stage in receipt["stages"]}
    redacted = by_stage["db_identity"]["counts"]["db_dsn_redacted"]
    assert redacted["host"] == "db.example"
    assert redacted["user"] == "alice"
    assert redacted["database"] == "nhms"
    assert "password" not in redacted


def test_missing_database_url_blocks_whole_receipt():
    receipt = emit.build_baseline_receipt(
        "m24-base-004",
        database_url=None,
        gateway_url="http://127.0.0.1:8081",
        connect=_fake_connect_factory(_responses()),
        http_get=_fake_http_get_ok,
    )
    validate_receipt(receipt)
    assert receipt["status"] == "BLOCKED"
    assert receipt["dependency_blocker"]
    assert receipt["live_proof_accepted"] is False


def test_gateway_unreachable_stage_blocked_but_receipt_pass():
    def _boom(url):
        raise ConnectionError("connection refused")

    receipt = emit.build_baseline_receipt(
        "m24-base-005",
        database_url="postgresql://u:pw@db:5432/nhms",
        gateway_url="http://127.0.0.1:8081",
        connect=_fake_connect_factory(_responses()),
        http_get=_boom,
    )
    validate_receipt(receipt)
    assert receipt["status"] == "PASS"
    by_stage = {stage["stage"]: stage for stage in receipt["stages"]}
    assert by_stage["gateway_health"]["status"] == "BLOCKED"
    assert "error" in by_stage["gateway_health"]["counts"]


def test_db_unreachable_marks_db_stages_blocked_receipt_pass():
    def _connect_fail(database_url):
        raise OSError("could not connect to server")

    receipt = emit.build_baseline_receipt(
        "m24-base-006",
        database_url="postgresql://u:pw@db:5432/nhms",
        gateway_url="http://127.0.0.1:8081",
        connect=_connect_fail,
        http_get=_fake_http_get_ok,
    )
    validate_receipt(receipt)
    assert receipt["status"] == "PASS"
    by_stage = {stage["stage"]: stage for stage in receipt["stages"]}
    for name in ("active_models", "hydro_run_gfs", "hydro_run_ifs", "state_snapshot"):
        assert by_stage[name]["status"] == "BLOCKED"


def test_main_writes_receipt_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@db:5432/nhms")
    monkeypatch.setenv("SLURM_GATEWAY_URL", "http://127.0.0.1:8081")

    real_build = emit.build_baseline_receipt

    def _patched_build(run_id, *, database_url, gateway_url, **_kwargs):
        return real_build(
            run_id,
            database_url=database_url,
            gateway_url=gateway_url,
            connect=_fake_connect_factory(_responses()),
            http_get=_fake_http_get_ok,
        )

    monkeypatch.setattr(emit, "build_baseline_receipt", _patched_build)

    rc = emit.main(["--run-id", "m24-main-001", "--root", str(tmp_path)])
    assert rc == 0

    path = tmp_path / "m24-main-001" / "baseline.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    validate_receipt(json.loads(path.read_text()))
