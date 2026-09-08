"""C3 current-publication receipt contracts. Helpers imported from the C1/C2 core suite."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from packages.common.node27_issue1895_display_runtime import validate_c1_receipt
from packages.common.node27_issue1895_lanes import EXACT_IDENTITY_SQL
from packages.common.node27_issue1895_private_receipt import publish_private_receipt
from packages.common.node27_issue1895_publication_current import (
    CANONICAL_SCHEDULER_REGISTRY_MANIFEST,
    COMPLETE_RUN_SQL,
    _validate_c4_matches_bound_identity,
    bind_c3_receipt,
    observe_current_publication,
    validate_c3_receipt,
)
from packages.common.node27_issue1895_readonly_accept import validate_c2_receipt
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from services.orchestrator.scheduler_file_providers import MAX_FILE_PROVIDER_JSON_NODES, MAX_REGISTRY_MANIFEST_BYTES
from tests.test_issue1895_readiness_c1_c2_c3 import (
    INVALID_SHA,
    NOW,
    PAST_BRACKET,
    REGISTRY_SCHEMA_VERSION,
    ROOT,
    SHA,
    WRONG_SHA,
    FakeOpener,
    FakeResponse,
    _current_bracket,
    _private,
)


class FakeC3Cursor:
    def __init__(self, connection: "FakeC3Connection") -> None:
        self.connection = connection
        self.description: list[tuple[str]] = []
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeC3Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.connection.executed.append((sql, params))
        if sql.startswith("SET LOCAL"):
            return
        if sql == "SELECT current_setting('transaction_read_only')":
            self._rows = [{"transaction_read_only": "on"}]
        elif sql == "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user":
            self._rows = [{"current_user": "nhms_display_ro", "rolsuper": False}]
        elif sql == EXACT_IDENTITY_SQL:
            source = str(params[-1])  # type: ignore[index]
            self._rows = [self.connection.identities[source]]
        elif sql == COMPLETE_RUN_SQL:
            source = str(params[0])  # type: ignore[index]
            self._rows = list(self.connection.complete_rows[source])
        else:
            raise AssertionError(f"unexpected SQL: {sql}")
        self.description = [(key,) for key in self._rows[0]] if self._rows else []

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class FakeC3Connection:
    def __init__(self, identities: dict[str, dict[str, Any]], complete_rows: dict[str, list[dict[str, Any]]]) -> None:
        self.identities = identities
        self.complete_rows = complete_rows
        self.executed: list[tuple[str, object]] = []
        self.set_session_calls: list[dict[str, bool]] = []
        self.rollback_called = False
        self.closed = False

    def set_session(self, **kwargs: bool) -> None:
        self.set_session_calls.append(kwargs)

    def cursor(self) -> FakeC3Cursor:
        return FakeC3Cursor(self)

    def rollback(self) -> None:
        self.rollback_called = True

    def close(self) -> None:
        self.closed = True


def _registry_payload(*, generated_at: str, models: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "generated_at": generated_at,
        "models": models,
    }
    payload["checksum"] = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return payload


def _write_registry_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.chmod(path, 0o600)


def _frontend_c4_builder() -> Path | None:
    candidate = ROOT / "apps" / "frontend" / "node_modules" / ".bin" / "vitest"
    return candidate if candidate.is_file() and shutil.which("pnpm") is not None else None


def _c3_identity(source: str, *, run_id: str, cycle_time: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "model_id": "model",
        "basin_id": "basin",
        "basin_version_id": "bv",
        "river_network_version_id": "rv",
        "source_id": source,
        "cycle_time": cycle_time,
        "status": "published",
    }


def _c4_pass_product(identity: dict[str, Any], scenario: str) -> dict[str, Any]:
    return {
        **identity,
        "cycle_time": str(identity["cycle_time"]).replace("Z", ".000Z"),
        "scenario": scenario,
    }


def _c3_complete_row(identity: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "source_id": source,
        "cycle_time": identity["cycle_time"],
        "run_status": "published",
        "model_id": "model",
        "basin_id": "basin",
    }


def _identity_response(identity: dict[str, Any]) -> FakeResponse:
    return FakeResponse(
        json.dumps(
            {"status": "ok", "data": {**identity, "status": "ready", "availability": {"ready": True}}}
        ).encode()
    )


def _c4_pass_document(gfs: dict[str, Any], ifs: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact": "nhms-frontend-c4-live-evidence",
        "schema_version": "1.0",
        "status": "PASS",
        "generated_at": "2026-09-06T12:00:00.000Z",
        "started_at": "2026-09-06T12:00:00.000Z",
        "ended_at": "2026-09-06T12:00:00.000Z",
        "origins": {"frontend": "https://test.nwm.ac.cn", "api": "https://test.nwm.ac.cn"},
        "requested_pins": {"basin_id": "basin", "river_segment_id": "segment"},
        "gfs": _c4_pass_product(gfs, "forecast_gfs_deterministic"),
        "ifs": _c4_pass_product(ifs, "forecast_ifs_deterministic"),
        "home": {
            "path": "/",
            "map_surface_visible": True,
            "runtime_config_status": 200,
            "runtime_service_role": "display_readonly",
            "current_read_observed": True,
            "current_read_path": "/api/v1/mvp/qhh/latest-product",
        },
        "ops": {
            "gfs": {
                "source_id": "GFS", "path": "/ops", "heading_observed": True, "permission_denied": True,
                "runtime_unavailable": True, "status_status": 200, "stages_status": 200, "jobs_status": 200,
                "job_id": "gfs-job", "logs_status": 200, "role_selector_count": 0,
                "retry_cancel_control_count": 0, "slurm_request_count": 0,
            },
            "ifs": {
                "source_id": "IFS", "path": "/ops", "heading_observed": True, "permission_denied": True,
                "runtime_unavailable": True, "status_status": 200, "stages_status": 200, "jobs_status": 200,
                "job_id": "ifs-job", "logs_status": 200, "role_selector_count": 0,
                "retry_cancel_control_count": 0, "slurm_request_count": 0,
            },
        },
        "source_switch": {"available": True, "selected_source": "IFS"},
        "no_control": {"slurm_request_count": 0, "non_get_control_count": 0},
        "failure": None,
    }


@pytest.mark.skipif(_frontend_c4_builder() is None, reason="frontend C4 builder dependencies are unavailable")
def test_c3_accepts_the_shipping_frontend_c4_builder_output(tmp_path: Path) -> None:
    fixture = tmp_path / "c4-builder.json"
    try:
        completed = subprocess.run(
            [
                "pnpm",
                "test",
                "--",
                "src/lib/c4DisplayEvidence/__tests__/receipt.test.ts",
            ],
            cwd=ROOT / "apps" / "frontend",
            env={**os.environ, "C4_ISSUE1895_BRIDGE_FIXTURE": str(fixture)},
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"shipping C4 builder bridge timed out: {error}")
    assert completed.returncode == 0, completed.stderr
    c4 = json.loads(fixture.read_text(encoding="utf-8"))
    assert c4["gfs"]["cycle_time"] == "2026-09-04T00:00:00.000Z"
    assert c4["ifs"]["cycle_time"] == "2026-09-04T06:00:00.000Z"
    bound = {
        "GFS": {
            "run_id": "qhh_20260904_gfs",
            "model_id": "shud-gfs",
            "basin_id": "basins_qhh",
            "basin_version_id": "bv-2026-09",
            "river_network_version_id": "rn-2026-09",
            "source_id": "GFS",
            "cycle_time": "2026-09-04T00:00:00Z",
            "scenario": "forecast_gfs_deterministic",
        },
        "IFS": {
            "run_id": "qhh_20260904_ifs",
            "model_id": "shud-ifs",
            "basin_id": "basins_qhh",
            "basin_version_id": "bv-2026-09",
            "river_network_version_id": "rn-2026-09",
            "source_id": "IFS",
            "cycle_time": "2026-09-04T06:00:00Z",
            "scenario": "forecast_ifs_deterministic",
        },
    }
    _validate_c4_matches_bound_identity({"products": {"GFS": c4["gfs"], "IFS": c4["ifs"]}}, bound=bound)


def test_c3_c4_identity_compares_only_cycle_time_as_a_calendar_valid_instant() -> None:
    bound = {
        "GFS": {
            "run_id": "gfs-run",
            "model_id": "model",
            "basin_id": "basin",
            "basin_version_id": "bv",
            "river_network_version_id": "rv",
            "source_id": "GFS",
            "cycle_time": "2026-09-06T00:00:00Z",
            "scenario": "forecast_gfs_deterministic",
        },
        "IFS": {
            "run_id": "ifs-run",
            "model_id": "model",
            "basin_id": "basin",
            "basin_version_id": "bv",
            "river_network_version_id": "rv",
            "source_id": "IFS",
            "cycle_time": "2026-09-06T06:00:00Z",
            "scenario": "forecast_ifs_deterministic",
        },
    }
    c4 = {
        "products": {
            "GFS": {**bound["GFS"], "cycle_time": "2026-09-06T00:00:00.000Z"},
            "IFS": {**bound["IFS"], "cycle_time": "2026-09-06T06:00:00.000Z"},
        }
    }
    _validate_c4_matches_bound_identity(c4, bound=bound)
    for bad_cycle in ("2026-02-30T00:00:00Z", "2026-09-06T00:00:00+01:00"):
        invalid = {"products": {**c4["products"], "GFS": {**c4["products"]["GFS"], "cycle_time": bad_cycle}}}
        with pytest.raises(Issue1895ReadinessError) as rejected:
            _validate_c4_matches_bound_identity(invalid, bound=bound)
        assert rejected.value.code == "C3_C4_IDENTITY"
    changed_model = {"products": {**c4["products"], "GFS": {**c4["products"]["GFS"], "model_id": "other"}}}
    with pytest.raises(Issue1895ReadinessError) as exact:
        _validate_c4_matches_bound_identity(changed_model, bound=bound)
    assert exact.value.code == "C3_C4_IDENTITY"


def test_c3_owner_binds_current_display_api_db_registry_frontier_and_c4(tmp_path: Path) -> None:
    private = tmp_path / "private"
    _private(private)
    display_env = private / "display.env"
    display_env.write_text("NHMS_DISPLAY_API_PORT=18080\n", encoding="utf-8")
    os.chmod(display_env, 0o600)
    gfs = _c3_identity("GFS", run_id="gfs-run", cycle_time="2026-09-06T00:00:00Z")
    ifs = _c3_identity("IFS", run_id="ifs-run", cycle_time="2026-09-06T06:00:00Z")
    registry = private / "manifest-last.json"
    registry.write_text(
        json.dumps(
            _registry_payload(
                generated_at="2026-09-06T11:00:00Z",
                models=[{"model_id": "model", "basin_id": "basin"}],
            )
        ),
        encoding="utf-8",
    )
    baseline = private / "valid-times-baseline.json"
    baseline.write_text(json.dumps({"valid_times": ["2026-09-06T00:00:00Z"]}), encoding="utf-8")
    c4 = private / "c4.json"
    c4.write_text(json.dumps(_c4_pass_document(gfs, ifs)), encoding="utf-8")
    for path in (registry, baseline, c4):
        os.chmod(path, 0o600)
    connection = FakeC3Connection(
        {"GFS": gfs, "IFS": ifs},
        {
            "gfs": [_c3_complete_row(gfs, "gfs")],
            "IFS": [_c3_complete_row(ifs, "IFS")],
        },
    )
    origin = "http://127.0.0.1:18080"
    gfs_url = f"{origin}/api/v1/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=basin"
    ifs_url = f"{origin}/api/v1/mvp/qhh/latest-product?source=IFS&identity_only=true&basin_id=basin"
    opener = FakeOpener(
        {
            gfs_url: _identity_response(gfs),
            ifs_url: _identity_response(ifs),
            f"{origin}/api/v1/layers/discharge/valid-times": FakeResponse(
                json.dumps({"valid_times": ["2026-09-06T06:00:00Z"]}).encode()
            ),
        }
    )
    receipt = private / "c3.json"
    document = observe_current_publication(
        display_env=display_env,
        registry=registry,
        canonical_registry_path=registry,
        allow_test_registry_override=True,
        expected_display_env=display_env,
        allow_test_display_env_override=True,
        basin_id="basin",
        baseline_valid_times=baseline,
        c4_receipt=c4,
        receipt_path=receipt,
        head_sha=SHA,
        reviewed_sha=SHA,
        dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
        opener=opener,
        connect=lambda _dsn: connection,
        now=lambda: "2026-09-06T12:00:00Z",
    )
    assert document["checks"] == {
        "readonly_session": True,
        "api_db_exact_identity": True,
        "registry_complete_cycle": True,
        "valid_times_nonempty_nonregressed": True,
        "c4_pass_control_zero": True,
        "c4_api_db_identity": True,
        "c4_ops_logs": True,
    }
    assert connection.set_session_calls == [{"readonly": True, "autocommit": False}]
    assert connection.rollback_called and connection.closed
    assert any(sql == EXACT_IDENTITY_SQL and isinstance(params, tuple) for sql, params in connection.executed)
    assert any(sql == COMPLETE_RUN_SQL and isinstance(params, tuple) for sql, params in connection.executed)
    assert all(request.get_method() == "GET" for request in opener.requests)


def _c3_owner_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, FakeC3Connection, FakeOpener]:
    private = tmp_path / "private"
    _private(private)
    display_env = private / "display.env"
    display_env.write_text("NHMS_DISPLAY_API_PORT=18080\n", encoding="utf-8")
    os.chmod(display_env, 0o600)
    gfs = _c3_identity("GFS", run_id="gfs-run", cycle_time="2026-09-06T00:00:00Z")
    ifs = _c3_identity("IFS", run_id="ifs-run", cycle_time="2026-09-06T06:00:00Z")
    registry = private / "manifest-last.json"
    registry.write_text(
        json.dumps(
            _registry_payload(
                generated_at="2026-09-06T11:00:00Z",
                models=[{"model_id": "model", "basin_id": "basin"}],
            )
        ),
        encoding="utf-8",
    )
    baseline = private / "valid-times-baseline.json"
    baseline.write_text(json.dumps({"valid_times": ["2026-09-06T00:00:00Z"]}), encoding="utf-8")
    c4 = private / "c4.json"
    c4.write_text(json.dumps(_c4_pass_document(gfs, ifs)), encoding="utf-8")
    for path in (registry, baseline, c4):
        os.chmod(path, 0o600)
    connection = FakeC3Connection(
        {"GFS": gfs, "IFS": ifs},
        {
            "gfs": [_c3_complete_row(gfs, "gfs")],
            "IFS": [_c3_complete_row(ifs, "IFS")],
        },
    )
    origin = "http://127.0.0.1:18080"
    opener = FakeOpener(
        {
            f"{origin}/api/v1/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=basin": (
                _identity_response(gfs)
            ),
            f"{origin}/api/v1/mvp/qhh/latest-product?source=IFS&identity_only=true&basin_id=basin": (
                _identity_response(ifs)
            ),
            f"{origin}/api/v1/layers/discharge/valid-times": FakeResponse(
                json.dumps({"valid_times": ["2026-09-06T06:00:00Z"]}).encode()
            ),
        }
    )
    return display_env, registry, baseline, c4, private / "c3.json", connection, opener


def test_c3_owner_refuses_invalid_or_mismatched_sha_without_publishing(tmp_path: Path) -> None:
    display_env, registry, baseline, c4, receipt, connection, opener = _c3_owner_inputs(tmp_path)
    with pytest.raises(Issue1895ReadinessError) as invalid:
        observe_current_publication(
            display_env=display_env,
            registry=registry,
            canonical_registry_path=registry,
            allow_test_registry_override=True,
            expected_display_env=display_env,
            allow_test_display_env_override=True,
            basin_id="basin",
            baseline_valid_times=baseline,
            c4_receipt=c4,
            receipt_path=receipt,
            head_sha=INVALID_SHA,
            reviewed_sha=INVALID_SHA,
            dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
            opener=opener,
            connect=lambda _dsn: connection,
            now=lambda: "2026-09-06T12:00:00Z",
        )
    assert invalid.value.code == "READINESS_SHA_INVALID"
    assert not receipt.exists()
    with pytest.raises(Issue1895ReadinessError) as mismatch:
        observe_current_publication(
            display_env=display_env,
            registry=registry,
            canonical_registry_path=registry,
            allow_test_registry_override=True,
            expected_display_env=display_env,
            allow_test_display_env_override=True,
            basin_id="basin",
            baseline_valid_times=baseline,
            c4_receipt=c4,
            receipt_path=receipt,
            head_sha=SHA,
            reviewed_sha=WRONG_SHA,
            dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
            opener=opener,
            connect=lambda _dsn: connection,
            now=lambda: "2026-09-06T12:00:00Z",
        )
    assert mismatch.value.code == "READINESS_SHA_MISMATCH"
    assert not receipt.exists()


@pytest.mark.parametrize(
    ("status", "failure"),
    (("FAIL", None), ("BLOCKED", {"reason": "ops"}), ("PASS", {"reason": "ops"})),
)
def test_c3_owner_refuses_non_pass_c4_without_publishing(
    tmp_path: Path, status: str, failure: dict[str, str] | None
) -> None:
    display_env, registry, baseline, c4, receipt, connection, opener = _c3_owner_inputs(tmp_path)
    payload = json.loads(c4.read_text(encoding="utf-8"))
    payload["status"] = status
    payload["failure"] = failure
    c4.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(c4, 0o600)
    with pytest.raises(Issue1895ReadinessError) as refused:
        observe_current_publication(
            display_env=display_env,
            registry=registry,
            canonical_registry_path=registry,
            allow_test_registry_override=True,
            expected_display_env=display_env,
            allow_test_display_env_override=True,
            basin_id="basin",
            baseline_valid_times=baseline,
            c4_receipt=c4,
            receipt_path=receipt,
            head_sha=SHA,
            reviewed_sha=SHA,
            dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
            opener=opener,
            connect=lambda _dsn: connection,
            now=lambda: "2026-09-06T12:00:00Z",
        )
    assert refused.value.code == "C3_C4_STATUS"
    assert not receipt.exists()


def test_c3_binder_refuses_wrong_invalid_sha_and_out_of_bracket(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    _private(parent)
    c4 = parent / "c4.json"
    c4.write_text("{}", encoding="utf-8")
    registry = parent / "manifest-last.json"
    registry.write_text(
        json.dumps(
            _registry_payload(
                generated_at="2026-09-06T11:00:00Z",
                models=[{"model_id": "model", "basin_id": "basin"}],
            )
        ),
        encoding="utf-8",
    )
    for path in (c4, registry):
        os.chmod(path, 0o600)
    receipt = parent / "c3.json"
    document = _c3_document(c4)
    document["registry_facts"] = _registry_document(registry)
    document["registry_sha256"] = document["registry_facts"]["sha256"]
    document["registry_generated_at"] = "2026-09-06T11:00:00Z"
    publish_private_receipt(receipt, document, code_prefix="C3_RECEIPT", stage="c3")
    original = receipt.read_bytes()
    cmd_start, cmd_end = _current_bracket()
    with pytest.raises(Issue1895ReadinessError) as wrong:
        bind_c3_receipt(
            receipt,
            reviewed_sha=WRONG_SHA,
            basin_id="basin",
            c4_receipt=c4,
            registry=registry,
            canonical_registry_path=registry,
            allow_test_registry_override=True,
            cmd_start=cmd_start,
            cmd_end=cmd_end,
        )
    assert wrong.value.code == "C3_BIND_SHA"
    assert receipt.read_bytes() == original
    with pytest.raises(Issue1895ReadinessError) as invalid:
        bind_c3_receipt(
            receipt,
            reviewed_sha=INVALID_SHA,
            basin_id="basin",
            c4_receipt=c4,
            registry=registry,
            canonical_registry_path=registry,
            allow_test_registry_override=True,
            cmd_start=cmd_start,
            cmd_end=cmd_end,
        )
    assert invalid.value.code == "READINESS_SHA_INVALID"
    assert receipt.read_bytes() == original
    with pytest.raises(Issue1895ReadinessError) as bracket:
        bind_c3_receipt(
            receipt,
            reviewed_sha=SHA,
            basin_id="basin",
            c4_receipt=c4,
            registry=registry,
            canonical_registry_path=registry,
            allow_test_registry_override=True,
            cmd_start=PAST_BRACKET[0],
            cmd_end=PAST_BRACKET[1],
        )
    assert bracket.value.code == "READINESS_BRACKET"
    assert receipt.read_bytes() == original


@pytest.mark.parametrize(
    "fault",
    ("api_mismatch", "registry_missing", "registry_invalid", "frontier_regressed", "c4_tamper"),
)
def test_c3_owner_refuses_broken_current_chain(tmp_path: Path, fault: str) -> None:
    private = tmp_path / "private"
    _private(private)
    display_env = private / "display.env"
    display_env.write_text("NHMS_DISPLAY_API_PORT=18080\n", encoding="utf-8")
    os.chmod(display_env, 0o600)
    gfs = _c3_identity("GFS", run_id="gfs-run", cycle_time="2026-09-06T00:00:00Z")
    ifs = _c3_identity("IFS", run_id="ifs-run", cycle_time="2026-09-06T06:00:00Z")
    registry = private / "manifest-last.json"
    models: list[dict[str, Any]] = [{"model_id": "model", "basin_id": "basin"}]
    if fault == "registry_missing":
        models.clear()
    if fault == "registry_invalid":
        models = [{"model_id": "", "basin_id": "basin"}]
    registry.write_text(
        json.dumps(_registry_payload(generated_at="2026-09-06T11:00:00Z", models=models)),
        encoding="utf-8",
    )
    baseline = private / "valid-times-baseline.json"
    baseline.write_text(json.dumps({"valid_times": ["2026-09-06T12:00:00Z"]}), encoding="utf-8")
    c4_payload = _c4_pass_document(gfs, ifs)
    if fault == "c4_tamper":
        c4_payload["gfs"]["run_id"] = "other"
    c4 = private / "c4.json"
    c4.write_text(json.dumps(c4_payload), encoding="utf-8")
    for path in (registry, baseline, c4):
        os.chmod(path, 0o600)
    api_gfs = dict(gfs)
    if fault == "api_mismatch":
        api_gfs["run_id"] = "other"
    frontier = ["2026-09-06T00:00:00Z"] if fault == "frontier_regressed" else ["2026-09-06T18:00:00Z"]
    connection = FakeC3Connection(
        {"GFS": gfs, "IFS": ifs},
        {
            "gfs": [_c3_complete_row(gfs, "gfs")],
            "IFS": [_c3_complete_row(ifs, "IFS")],
        },
    )
    origin = "http://127.0.0.1:18080"
    gfs_url = f"{origin}/api/v1/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=basin"
    ifs_url = f"{origin}/api/v1/mvp/qhh/latest-product?source=IFS&identity_only=true&basin_id=basin"
    opener = FakeOpener(
        {
            gfs_url: _identity_response(api_gfs),
            ifs_url: _identity_response(ifs),
            f"{origin}/api/v1/layers/discharge/valid-times": FakeResponse(
                json.dumps({"valid_times": frontier}).encode()
            ),
        }
    )
    with pytest.raises(Issue1895ReadinessError):
        observe_current_publication(
            display_env=display_env,
            registry=registry,
            canonical_registry_path=registry,
            allow_test_registry_override=True,
            expected_display_env=display_env,
            allow_test_display_env_override=True,
            basin_id="basin",
            baseline_valid_times=baseline,
            c4_receipt=c4,
            receipt_path=private / "c3.json",
            head_sha=SHA,
            reviewed_sha=SHA,
            dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
            opener=opener,
            connect=lambda _dsn: connection,
            now=lambda: "2026-09-06T12:00:00Z",
        )
    if fault != "registry_missing":
        assert connection.rollback_called and connection.closed


def test_c3_rejects_noncanonical_display_env_override_without_test_seam(tmp_path: Path) -> None:
    display_env = tmp_path / "display.env"
    display_env.write_text("NHMS_DISPLAY_API_PORT=18080\n", encoding="utf-8")
    os.chmod(display_env, 0o600)
    with pytest.raises(Issue1895ReadinessError) as rejected:
        observe_current_publication(
            display_env=display_env,
            registry=CANONICAL_SCHEDULER_REGISTRY_MANIFEST,
            basin_id="basin",
            baseline_valid_times=tmp_path / "baseline.json",
            c4_receipt=tmp_path / "c4.json",
            receipt_path=tmp_path / "c3.json",
            head_sha=SHA,
            reviewed_sha=SHA,
            dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
        )
    assert rejected.value.code == "C1_DISPLAY_ENV_PATH"
    with pytest.raises(Issue1895ReadinessError) as seam_rejected:
        observe_current_publication(
            display_env=display_env,
            registry=CANONICAL_SCHEDULER_REGISTRY_MANIFEST,
            basin_id="basin",
            baseline_valid_times=tmp_path / "baseline.json",
            c4_receipt=tmp_path / "c4.json",
            receipt_path=tmp_path / "c3.json",
            head_sha=SHA,
            reviewed_sha=SHA,
            dsn="postgresql://nhms_display_ro@127.0.0.1/nhms",
            expected_display_env=display_env,
        )
    assert seam_rejected.value.code == "C3_DISPLAY_ENV_PATH"


def test_c3_rejects_noncanonical_registry_override_without_test_seam(tmp_path: Path) -> None:
    registry = tmp_path / "manifest-last.json"
    registry.write_text(
        json.dumps(
            _registry_payload(
                generated_at="2026-09-06T00:00:00Z",
                models=[{"model_id": "m", "basin_id": "b"}],
            )
        )
    )
    with pytest.raises(Issue1895ReadinessError) as rejected:
        from packages.common.node27_issue1895_publication_current import _registry_models

        _registry_models(registry, canonical_path=registry)
    assert rejected.value.code == "C3_REGISTRY_PATH"
    registry_payload = json.loads(registry.read_text())
    registry_payload["schema_version"] = "wrong"
    registry.write_text(json.dumps(registry_payload), encoding="utf-8")
    with pytest.raises(Issue1895ReadinessError) as unsupported_schema:
        _registry_models(registry, canonical_path=registry, allow_test_override=True)
    assert unsupported_schema.value.code == "C3_REGISTRY_INVALID"
    registry_payload = _registry_payload(
        generated_at="2026-09-06T00:00:00Z",
        models=[{"model_id": "m", "basin_id": "b"}],
    )
    registry_payload["checksum"] = "sha256:" + "0" * 64
    registry.write_text(json.dumps(registry_payload), encoding="utf-8")
    with pytest.raises(Issue1895ReadinessError) as invalid_checksum:
        _registry_models(registry, canonical_path=registry, allow_test_override=True)
    assert invalid_checksum.value.code == "C3_REGISTRY_INVALID"


def test_c3_registry_reader_matches_shipping_size_and_node_bounds(tmp_path: Path) -> None:
    from packages.common import node27_issue1895_publication_current as c3
    from packages.common.node27_issue1895_publication_current import _registry_models

    assert c3.MAX_REGISTRY_MANIFEST_BYTES == MAX_REGISTRY_MANIFEST_BYTES
    assert c3.MAX_FILE_PROVIDER_JSON_NODES == MAX_FILE_PROVIDER_JSON_NODES
    large = tmp_path / "large-manifest.json"
    payload = _registry_payload(
        generated_at="2026-09-06T00:00:00Z",
        models=[{"model_id": "m", "basin_id": "b", "shipping_padding": "x" * (1_100_000)}],
    )
    _write_registry_payload(large, payload)
    assert large.stat().st_size > 1_048_576
    models, _digest, _facts, generated_at = _registry_models(
        large,
        canonical_path=large,
        allow_test_override=True,
    )
    assert models[0]["model_id"] == "m"
    assert generated_at == "2026-09-06T00:00:00Z"
    oversized = tmp_path / "oversized-manifest.json"
    oversized.write_bytes(b"x" * (MAX_REGISTRY_MANIFEST_BYTES + 1))
    os.chmod(oversized, 0o600)
    with pytest.raises(Issue1895ReadinessError) as too_large:
        _registry_models(oversized, canonical_path=oversized, allow_test_override=True)
    assert too_large.value.code == "C3_INPUT_INVALID"
    too_many_nodes = tmp_path / "too-many-nodes.json"
    payload = _registry_payload(
        generated_at="2026-09-06T00:00:00Z",
        models=[{"model_id": "m", "basin_id": "b", "nodes": [0] * MAX_FILE_PROVIDER_JSON_NODES}],
    )
    _write_registry_payload(too_many_nodes, payload)
    with pytest.raises(Issue1895ReadinessError) as node_limited:
        _registry_models(too_many_nodes, canonical_path=too_many_nodes, allow_test_override=True)
    assert node_limited.value.code == "C3_INPUT_INVALID"


def test_c3_registry_test_seam_refuses_symlinked_manifest(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps(
            _registry_payload(
                generated_at="2026-09-06T00:00:00Z",
                models=[{"model_id": "m", "basin_id": "b"}],
            )
        ),
        encoding="utf-8",
    )
    registry = tmp_path / "manifest-last.json"
    registry.symlink_to(target)
    from packages.common.node27_issue1895_publication_current import _registry_models

    with pytest.raises(Issue1895ReadinessError) as rejected:
        _registry_models(registry, canonical_path=registry, allow_test_override=True)
    assert rejected.value.code == "C3_REGISTRY_PATH"


def _c3_document(c4: Path) -> dict[str, Any]:
    generated = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    digest = hashlib.sha256(c4.read_bytes()).hexdigest()
    facts = c4.stat()
    identity = {
        "run_id": "run-gfs",
        "model_id": "model",
        "basin_id": "basin",
        "basin_version_id": "bv",
        "river_network_version_id": "rv",
        "source_id": "GFS",
        "cycle_time": NOW,
        "scenario": "forecast_gfs_deterministic",
    }
    ifs = {**identity, "run_id": "run-ifs", "source_id": "IFS", "scenario": "forecast_ifs_deterministic"}

    def source(value: dict[str, str], job: str) -> dict[str, Any]:
        return {"identity": value, "expected_count": 1, "observed_count": 1, "c4_job_id": job, "c4_logs_status": 200}

    return {
        "artifact": "nhms-issue1895-c3-current-publication-display",
        "schema_version": "1.0",
        "status": "PASS",
        "head_sha": SHA,
        "reviewed_sha": SHA,
        "started_at": generated,
        "ended_at": generated,
        "generated_at": generated,
        "origin": "http://127.0.0.1:18080",
        "basin_id": "basin",
        "registry_sha256": "b" * 64,
        "registry_facts": {
            "st_dev": 1,
            "st_ino": 3,
            "st_uid": os.geteuid(),
            "st_mode": 0o600,
            "st_nlink": 1,
            "st_size": 1,
            "sha256": "b" * 64,
        },
        "registry_generated_at": "2026-09-06T11:00:00Z",
        "baseline_sha256": "c" * 64,
        "frontier": {"status": 200, "current_count": 2, "baseline_count": 1, "non_regressed": True},
        "sources": {"GFS": source(identity, "job-gfs"), "IFS": source(ifs, "job-ifs")},
        "c4_receipt": {
            "st_dev": facts.st_dev,
            "st_ino": facts.st_ino,
            "st_uid": os.geteuid(),
            "st_mode": 0o600,
            "st_nlink": 1,
            "st_size": facts.st_size,
            "sha256": digest,
        },
        "checks": {
            "readonly_session": True,
            "api_db_exact_identity": True,
            "registry_complete_cycle": True,
            "valid_times_nonempty_nonregressed": True,
            "c4_pass_control_zero": True,
            "c4_api_db_identity": True,
            "c4_ops_logs": True,
        },
    }


def _registry_document(path: Path) -> dict[str, Any]:
    info = path.stat()
    raw = path.read_bytes()
    return {
        "st_dev": info.st_dev,
        "st_ino": info.st_ino,
        "st_uid": info.st_uid,
        "st_mode": info.st_mode & 0o777,
        "st_nlink": info.st_nlink,
        "st_size": info.st_size,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def test_c3_binder_rejects_c4_tamper_source_collapse_and_partial_counts(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    _private(parent)
    c4 = parent / "c4.json"
    c4.write_text("{}", encoding="utf-8")
    os.chmod(c4, 0o600)
    receipt = parent / "c3.json"
    document = _c3_document(c4)
    validate_c3_receipt(document)
    publish_private_receipt(receipt, document, code_prefix="C3_RECEIPT", stage="c3")
    cmd_start, cmd_end = _current_bracket()
    c4.write_text('{"changed":true}', encoding="utf-8")
    os.chmod(c4, 0o600)
    with pytest.raises(Issue1895ReadinessError) as tamper:
        bind_c3_receipt(
            receipt,
            reviewed_sha=SHA,
            basin_id="basin",
            c4_receipt=c4,
            registry=CANONICAL_SCHEDULER_REGISTRY_MANIFEST,
            cmd_start=cmd_start,
            cmd_end=cmd_end,
        )
    assert tamper.value.code == "C3_BIND_C4_TAMPER"
    collapsed = _c3_document(c4)
    collapsed["sources"]["IFS"]["identity"] = dict(collapsed["sources"]["GFS"]["identity"])
    with pytest.raises(Issue1895ReadinessError):
        validate_c3_receipt(collapsed)
    partial = _c3_document(c4)
    partial["sources"]["GFS"]["observed_count"] = 0
    with pytest.raises(Issue1895ReadinessError):
        validate_c3_receipt(partial)
    mismatched_scenario = _c3_document(c4)
    mismatched_scenario["sources"]["IFS"]["identity"]["scenario"] = "forecast_gfs_deterministic"
    with pytest.raises(Issue1895ReadinessError):
        validate_c3_receipt(mismatched_scenario)


def test_c3_binder_rereads_canonical_registry_facts_and_generated_at(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    _private(parent)
    c4 = parent / "c4.json"
    c4.write_text("{}", encoding="utf-8")
    registry = parent / "manifest-last.json"
    registry.write_text(
        json.dumps(
            _registry_payload(
                generated_at="2026-09-06T11:00:00Z",
                models=[{"model_id": "model", "basin_id": "basin"}],
            )
        ),
        encoding="utf-8",
    )
    for path in (c4, registry):
        os.chmod(path, 0o600)
    receipt = parent / "c3.json"
    document = _c3_document(c4)
    document["registry_facts"] = _registry_document(registry)
    document["registry_sha256"] = document["registry_facts"]["sha256"]
    document["registry_generated_at"] = "2026-09-06T11:00:00Z"
    validate_c3_receipt(document)
    publish_private_receipt(receipt, document, code_prefix="C3_RECEIPT", stage="c3")
    cmd_start, cmd_end = _current_bracket()
    bound = bind_c3_receipt(
        receipt,
        reviewed_sha=SHA,
        basin_id="basin",
        c4_receipt=c4,
        registry=registry,
        canonical_registry_path=registry,
        allow_test_registry_override=True,
        cmd_start=cmd_start,
        cmd_end=cmd_end,
    )
    assert bound["registry_facts"] == document["registry_facts"]
    registry.write_text(
        json.dumps(
            _registry_payload(
                generated_at="2026-09-06T12:00:00Z",
                models=[{"model_id": "model", "basin_id": "basin"}],
            )
        ),
        encoding="utf-8",
    )
    os.chmod(registry, 0o600)
    with pytest.raises(Issue1895ReadinessError) as changed:
        bind_c3_receipt(
            receipt,
            reviewed_sha=SHA,
            basin_id="basin",
            c4_receipt=c4,
            registry=registry,
            canonical_registry_path=registry,
            allow_test_registry_override=True,
            cmd_start=cmd_start,
            cmd_end=cmd_end,
        )
    assert changed.value.code == "C3_BIND_REGISTRY_TAMPER"
    invalid_generated = _c3_document(c4)
    invalid_generated["registry_generated_at"] = "2026-02-30T11:00:00Z"
    with pytest.raises(Issue1895ReadinessError) as invalid_timestamp:
        validate_c3_receipt(invalid_generated)
    assert invalid_timestamp.value.code == "C3_RECEIPT_REGISTRY"


@pytest.mark.parametrize(
    "schema_name,example_name,validator",
    [
        (
            "node27_issue1895_c1_display_runtime_receipt.schema.json",
            "node27_issue1895_c1_display_runtime_receipt.example.json",
            validate_c1_receipt,
        ),
        (
            "node27_issue1895_c2_readonly_boundary_receipt.schema.json",
            "node27_issue1895_c2_readonly_boundary_receipt.example.json",
            validate_c2_receipt,
        ),
        (
            "node27_issue1895_c3_current_publication_display_receipt.schema.json",
            "node27_issue1895_c3_current_publication_display_receipt.example.json",
            validate_c3_receipt,
        ),
    ],
)
def test_new_receipt_schemas_accept_pass_examples_and_reject_negative_documents(
    schema_name: str,
    example_name: str,
    validator: Any,
) -> None:
    schema = json.loads((ROOT / "schemas" / schema_name).read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    example = json.loads((ROOT / "schemas" / "examples" / example_name).read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(example)
    # Semantic validators intentionally require the actual euid on private input
    # facts, so schema-valid C2/C3 fixtures do not stand in for a live accepted receipt.
    if schema_name == "node27_issue1895_c1_display_runtime_receipt.schema.json":
        assert validator(example)["status"] == "PASS"
    if schema_name == "node27_issue1895_c2_readonly_boundary_receipt.schema.json":
        legacy_boolean = json.loads(json.dumps(example))
        legacy_boolean["checks"]["summary_full_gfs_ifs_scope"] = True
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(legacy_boolean)
    negative = dict(example)
    negative["artifact"] = "wrong"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(negative)
