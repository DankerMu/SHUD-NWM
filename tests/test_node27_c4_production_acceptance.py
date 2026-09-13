"""Public freeze/bind/verify and filesystem refusal for Bringup-C4 acceptance."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.node27_c4_production_acceptance import main as cli_main
from services.production_closure import c4_production_acceptance as owner
from services.production_closure.c4_production_acceptance import bind, freeze, verify
from services.production_closure.c4_production_acceptance_io import (
    FILE_MODE,
    PARENT_MODE,
    C4AcceptanceError,
    parse_closed_json,
    read_private_json,
)

REVIEWED_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FRONTEND_ORIGIN = "https://display.example.test"
API_ORIGIN = "https://api.example.test"
BASIN_ID = "basins_qhh"
SEGMENT_ID = "basins_qhh_shud_reach_000001"
CMD_START_DT = datetime(2025, 9, 4, 1, 0, 0, tzinfo=UTC)
CMD_END_DT = datetime(2025, 9, 4, 1, 2, 3, tzinfo=UTC)
CMD_START = str(int(CMD_START_DT.timestamp()))
CMD_END = str(int(CMD_END_DT.timestamp()))
FREEZE_AT = datetime(2025, 9, 4, 0, 59, 59, tzinfo=UTC)
LATE_FREEZE_AT = datetime(2025, 9, 4, 1, 0, 0, 1, tzinfo=UTC)

C4_FIXTURE_DOCUMENT: dict[str, Any] = {
    "artifact": "nhms-frontend-c4-live-evidence",
    "schema_version": "1.0",
    "status": "PASS",
    "generated_at": "2026-09-04T01:02:03Z",
    "started_at": "2026-09-04T01:00:00Z",
    "ended_at": "2026-09-04T01:02:03Z",
    "origins": {"frontend": FRONTEND_ORIGIN, "api": API_ORIGIN},
    "requested_pins": {"basin_id": BASIN_ID, "river_segment_id": SEGMENT_ID},
    "gfs": {
        "source_id": "GFS",
        "basin_id": BASIN_ID,
        "basin_version_id": "bv-2026-09",
        "river_network_version_id": "rn-2026-09",
        "run_id": "qhh_20260904_gfs",
        "model_id": "shud-gfs",
        "cycle_time": "2026-09-04T00:00:00.000Z",
        "scenario": "forecast_gfs_deterministic",
    },
    "ifs": {
        "source_id": "IFS",
        "basin_id": BASIN_ID,
        "basin_version_id": "bv-2026-09",
        "river_network_version_id": "rn-2026-09",
        "run_id": "qhh_20260904_ifs",
        "model_id": "shud-ifs",
        "cycle_time": "2026-09-04T06:00:00.000Z",
        "scenario": "forecast_ifs_deterministic",
    },
    "home": {
        "path": "/",
        "map_surface_visible": True,
        "runtime_config_status": 200,
        "runtime_service_role": "display_readonly",
        "current_read_observed": True,
        "current_read_path": "/api/v1/basins",
    },
    "ops": {
        "gfs": {
            "source_id": "GFS",
            "path": "/ops",
            "heading_observed": True,
            "permission_denied": False,
            "runtime_unavailable": False,
            "status_status": 200,
            "stages_status": 200,
            "jobs_status": 200,
            "job_id": "job-gfs-001",
            "logs_status": 200,
            "role_selector_count": 0,
            "retry_cancel_control_count": 0,
            "slurm_request_count": 0,
            "non_get_control_count": 0,
            "queue_readonly_visible": True,
            "operator_recovery_visible": True,
        },
        "ifs": {
            "source_id": "IFS",
            "path": "/ops",
            "heading_observed": True,
            "permission_denied": False,
            "runtime_unavailable": False,
            "status_status": 200,
            "stages_status": 200,
            "jobs_status": 200,
            "job_id": "job-ifs-001",
            "logs_status": 200,
            "role_selector_count": 0,
            "retry_cancel_control_count": 0,
            "slurm_request_count": 0,
            "non_get_control_count": 0,
            "queue_readonly_visible": True,
            "operator_recovery_visible": True,
        },
    },
    "source_switch": {
        "both_sources_completed": True,
        "identities_distinct_or_source_bound": True,
    },
    "no_control": {"slurm_request_count": 0, "non_get_control_count": 0},
    "failure": None,
}


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    os.chmod(path, PARENT_MODE)
    return path


def _write_private_json(path: Path, document: dict[str, Any]) -> None:
    encoded = json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, FILE_MODE)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
        os.fchmod(fd, FILE_MODE)
    finally:
        os.close(fd)


def _rewrite_private_json(path: Path, document: dict[str, Any]) -> None:
    encoded = json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
    os.chmod(path, 0o600)
    path.write_bytes(encoded)
    os.chmod(path, FILE_MODE)


def write_closed_c4_fixture(path: Path, *, cmd_start: int, cmd_end: int) -> None:
    """Write a labeled synthetic closed C4 receipt. Fixture proof, not live PASS."""

    started = datetime.fromtimestamp(cmd_start, UTC).replace(microsecond=0)
    ended = datetime.fromtimestamp(cmd_end, UTC).replace(microsecond=0)
    if ended < started:
        ended = started
    started_text = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    ended_text = ended.strftime("%Y-%m-%dT%H:%M:%SZ")
    document = dict(C4_FIXTURE_DOCUMENT)
    document["started_at"] = started_text
    document["ended_at"] = ended_text
    document["generated_at"] = ended_text
    _write_private_json(path, document)
    mtime = cmd_start if cmd_start == cmd_end else cmd_start + min(1, cmd_end - cmd_start)
    os.utime(path, (mtime, mtime), follow_symlinks=False)


def _approved_record() -> dict[str, Any]:
    return {
        "status": "PASS",
        "head_sha": REVIEWED_SHA,
        "reviewed_sha": REVIEWED_SHA,
        "note": "operator-supplied delivery record",
    }


def _workspace(tmp_path: Path) -> dict[str, Path]:
    root = _private_dir(tmp_path.resolve() / "private")
    return {
        "root": root,
        "approved": root / "approved.json",
        "receipt": root / "c4-receipt.json",
        "freeze": root / "freeze.json",
        "binding": root / "binding.json",
        "acceptance": root / "acceptance.json",
    }


def _pass_runner(_argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(_argv, 0, owner.BINDER_PASS, "")


def _fail_runner(_argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(_argv, 1, "", "BINDER: status must be PASS\n")


def _timeout_runner(_argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    raise subprocess.TimeoutExpired(cmd=_argv, timeout=10)


def _seed_approved(paths: dict[str, Path]) -> None:
    _write_private_json(paths["approved"], _approved_record())


def _freeze_ok(paths: dict[str, Path]) -> None:
    freeze(
        approved_record=paths["approved"],
        reviewed_sha=REVIEWED_SHA,
        receipt=paths["receipt"],
        frontend_origin=FRONTEND_ORIGIN,
        api_origin=API_ORIGIN,
        basin_id=BASIN_ID,
        segment_id=SEGMENT_ID,
        output=paths["freeze"],
        now=lambda: FREEZE_AT,
    )


def _write_receipt(paths: dict[str, Path]) -> None:
    write_closed_c4_fixture(paths["receipt"], cmd_start=int(CMD_START), cmd_end=int(CMD_END))


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    workspace = _workspace(tmp_path)
    _seed_approved(workspace)
    return workspace


def test_freeze_binds_original_source_and_five_inputs(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _raw, document, identity, parent = read_private_json(paths["freeze"])
    assert document["c4_inputs"]["receipt"] == str(paths["receipt"])
    assert document["frozen_at"] == "2025-09-04T00:59:59Z"
    assert identity.st_mode == FILE_MODE
    assert identity.st_nlink == 1
    assert parent.st_mode == PARENT_MODE
    assert not paths["receipt"].exists()
    assert not paths["binding"].exists()


def test_freeze_preserves_subsecond_timestamp_without_flooring(paths: dict[str, Path]) -> None:
    instant = datetime(2025, 9, 4, 0, 59, 59, 123456, tzinfo=UTC)
    freeze(
        approved_record=paths["approved"],
        reviewed_sha=REVIEWED_SHA,
        receipt=paths["receipt"],
        frontend_origin=FRONTEND_ORIGIN,
        api_origin=API_ORIGIN,
        basin_id=BASIN_ID,
        segment_id=SEGMENT_ID,
        output=paths["freeze"],
        now=lambda: instant,
    )
    _raw, document, _identity, _parent = read_private_json(paths["freeze"])
    assert document["frozen_at"] == "2025-09-04T00:59:59.123456Z"


def test_freeze_refuses_mismatched_sha_without_output(paths: dict[str, Path]) -> None:
    with pytest.raises(C4AcceptanceError) as error:
        freeze(
            approved_record=paths["approved"],
            reviewed_sha=OTHER_SHA,
            receipt=paths["receipt"],
            frontend_origin=FRONTEND_ORIGIN,
            api_origin=API_ORIGIN,
            basin_id=BASIN_ID,
            segment_id=SEGMENT_ID,
            output=paths["freeze"],
            now=lambda: FREEZE_AT,
        )
    assert error.value.code == "C4_SHA_MISMATCH"
    assert not paths["freeze"].exists()


def test_freeze_refuses_non_pass_status_without_self_approval(paths: dict[str, Path]) -> None:
    os.unlink(paths["approved"])
    _write_private_json(paths["approved"], {**_approved_record(), "status": "FAIL"})
    with pytest.raises(C4AcceptanceError) as error:
        freeze(
            approved_record=paths["approved"],
            reviewed_sha=REVIEWED_SHA,
            receipt=paths["receipt"],
            frontend_origin=FRONTEND_ORIGIN,
            api_origin=API_ORIGIN,
            basin_id=BASIN_ID,
            segment_id=SEGMENT_ID,
            output=paths["freeze"],
            now=lambda: FREEZE_AT,
        )
    assert error.value.code == "C4_APPROVAL_STATUS"
    assert not paths["freeze"].exists()


def test_freeze_refuses_existing_receipt_and_missing_parent(tmp_path: Path, paths: dict[str, Path]) -> None:
    _write_receipt(paths)
    with pytest.raises(C4AcceptanceError) as error:
        freeze(
            approved_record=paths["approved"],
            reviewed_sha=REVIEWED_SHA,
            receipt=paths["receipt"],
            frontend_origin=FRONTEND_ORIGIN,
            api_origin=API_ORIGIN,
            basin_id=BASIN_ID,
            segment_id=SEGMENT_ID,
            output=paths["freeze"],
            now=lambda: FREEZE_AT,
        )
    assert error.value.code == "C4_EXISTS"
    missing_parent = tmp_path / "absent" / "receipt.json"
    with pytest.raises(C4AcceptanceError) as missing:
        freeze(
            approved_record=paths["approved"],
            reviewed_sha=REVIEWED_SHA,
            receipt=missing_parent,
            frontend_origin=FRONTEND_ORIGIN,
            api_origin=API_ORIGIN,
            basin_id=BASIN_ID,
            segment_id=SEGMENT_ID,
            output=paths["freeze"],
            now=lambda: FREEZE_AT,
        )
    assert missing.value.code == "C4_PARENT_OPEN"


def test_bind_and_verify_succeed_with_injected_pass_binder(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    bind(
        freeze_path=paths["freeze"],
        reviewed_sha=REVIEWED_SHA,
        cmd_start=CMD_START,
        cmd_end=CMD_END,
        output=paths["binding"],
        command_runner=_pass_runner,
    )
    _raw, binding, _identity, _parent = read_private_json(paths["binding"])
    verify(
        freeze_path=paths["freeze"],
        binding_path=paths["binding"],
        reviewed_sha=REVIEWED_SHA,
        output=paths["acceptance"],
        command_runner=_pass_runner,
    )
    _raw, acceptance, _identity, _parent = read_private_json(paths["acceptance"])
    assert acceptance["status"] == "PASS"
    assert acceptance["c4"]["identity"] == binding["c4"]["identity"]
    assert acceptance["freeze"]["identity"] == binding["freeze"]["identity"]


def test_bind_refuses_nonzero_binder_and_keeps_inputs(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    before = paths["receipt"].read_bytes()
    with pytest.raises(C4AcceptanceError) as error:
        bind(
            freeze_path=paths["freeze"],
            reviewed_sha=REVIEWED_SHA,
            cmd_start=CMD_START,
            cmd_end=CMD_END,
            output=paths["binding"],
            command_runner=_fail_runner,
        )
    assert error.value.code == "C4_BINDER_FAILED"
    assert not paths["binding"].exists()
    assert paths["receipt"].read_bytes() == before
    assert paths["freeze"].exists()


def test_bind_refuses_exit_zero_without_pass_terminal(paths: dict[str, Path]) -> None:
    def fake(_argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(_argv, 0, "ok\n", "")

    _freeze_ok(paths)
    _write_receipt(paths)
    with pytest.raises(C4AcceptanceError) as error:
        bind(
            freeze_path=paths["freeze"],
            reviewed_sha=REVIEWED_SHA,
            cmd_start=CMD_START,
            cmd_end=CMD_END,
            output=paths["binding"],
            command_runner=fake,
        )
    assert error.value.code == "C4_BINDER_NOT_PASS"
    assert not paths["binding"].exists()


def test_bind_refuses_timeout_with_static_code(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    with pytest.raises(C4AcceptanceError) as error:
        bind(
            freeze_path=paths["freeze"],
            reviewed_sha=REVIEWED_SHA,
            cmd_start=CMD_START,
            cmd_end=CMD_END,
            output=paths["binding"],
            command_runner=_timeout_runner,
        )
    assert error.value.code == "C4_BINDER_TIMEOUT"
    assert "http" not in str(error.value)
    assert not paths["binding"].exists()


def test_bind_refuses_freeze_after_command_start_without_flooring(
    paths: dict[str, Path],
) -> None:
    freeze(
        approved_record=paths["approved"],
        reviewed_sha=REVIEWED_SHA,
        receipt=paths["receipt"],
        frontend_origin=FRONTEND_ORIGIN,
        api_origin=API_ORIGIN,
        basin_id=BASIN_ID,
        segment_id=SEGMENT_ID,
        output=paths["freeze"],
        now=lambda: LATE_FREEZE_AT,
    )
    _write_receipt(paths)
    with pytest.raises(C4AcceptanceError) as error:
        bind(
            freeze_path=paths["freeze"],
            reviewed_sha=REVIEWED_SHA,
            cmd_start=CMD_START,
            cmd_end=CMD_END,
            output=paths["binding"],
            command_runner=_pass_runner,
        )
    assert error.value.code == "C4_FREEZE_AFTER_START"
    assert not paths["binding"].exists()


def test_bind_refuses_c4_byte_change_across_binder(paths: dict[str, Path]) -> None:
    def mutating(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(paths["receipt"].read_text())
        payload["home"]["runtime_config_status"] = 201
        os.chmod(paths["receipt"], 0o600)
        paths["receipt"].write_bytes(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        os.chmod(paths["receipt"], FILE_MODE)
        return _pass_runner(argv, **kwargs)

    _freeze_ok(paths)
    _write_receipt(paths)
    with pytest.raises(C4AcceptanceError) as error:
        bind(
            freeze_path=paths["freeze"],
            reviewed_sha=REVIEWED_SHA,
            cmd_start=CMD_START,
            cmd_end=CMD_END,
            output=paths["binding"],
            command_runner=mutating,
        )
    assert error.value.code == "C4_IDENTITY_DRIFT"
    assert not paths["binding"].exists()


def test_verify_refuses_to_refresh_binding_when_c4_bytes_change(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    bind(
        freeze_path=paths["freeze"],
        reviewed_sha=REVIEWED_SHA,
        cmd_start=CMD_START,
        cmd_end=CMD_END,
        output=paths["binding"],
        command_runner=_pass_runner,
    )
    original_binding = paths["binding"].read_bytes()
    payload = json.loads(paths["receipt"].read_text())
    payload["ops"]["gfs"]["status_status"] = 201
    os.chmod(paths["receipt"], 0o600)
    paths["receipt"].write_bytes(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    os.chmod(paths["receipt"], FILE_MODE)
    with pytest.raises(C4AcceptanceError) as error:
        verify(
            freeze_path=paths["freeze"],
            binding_path=paths["binding"],
            reviewed_sha=REVIEWED_SHA,
            output=paths["acceptance"],
            command_runner=_pass_runner,
        )
    assert error.value.code == "C4_IDENTITY_DRIFT"
    assert not paths["acceptance"].exists()
    assert paths["binding"].read_bytes() == original_binding


def test_verify_refuses_when_approved_record_mutates_across_binder(
    paths: dict[str, Path],
) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    bind(
        freeze_path=paths["freeze"],
        reviewed_sha=REVIEWED_SHA,
        cmd_start=CMD_START,
        cmd_end=CMD_END,
        output=paths["binding"],
        command_runner=_pass_runner,
    )
    original_binding = paths["binding"].read_bytes()
    original_freeze = paths["freeze"].read_bytes()

    def mutate_approved(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(paths["approved"].read_text())
        payload["note"] = "mutated-after-freeze"
        _rewrite_private_json(paths["approved"], payload)
        return _pass_runner(argv, **kwargs)

    with pytest.raises(C4AcceptanceError) as error:
        verify(
            freeze_path=paths["freeze"],
            binding_path=paths["binding"],
            reviewed_sha=REVIEWED_SHA,
            output=paths["acceptance"],
            command_runner=mutate_approved,
        )
    assert error.value.code == "C4_SOURCE_DRIFT"
    assert not paths["acceptance"].exists()
    assert paths["binding"].read_bytes() == original_binding
    assert paths["freeze"].read_bytes() == original_freeze


def test_verify_refuses_when_freeze_mutates_across_binder(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    bind(
        freeze_path=paths["freeze"],
        reviewed_sha=REVIEWED_SHA,
        cmd_start=CMD_START,
        cmd_end=CMD_END,
        output=paths["binding"],
        command_runner=_pass_runner,
    )
    original_binding = paths["binding"].read_bytes()

    def mutate_freeze(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(paths["freeze"].read_text())
        payload["frozen_at"] = "2025-09-04T00:59:58.000001Z"
        _rewrite_private_json(paths["freeze"], payload)
        return _pass_runner(argv, **kwargs)

    with pytest.raises(C4AcceptanceError) as error:
        verify(
            freeze_path=paths["freeze"],
            binding_path=paths["binding"],
            reviewed_sha=REVIEWED_SHA,
            output=paths["acceptance"],
            command_runner=mutate_freeze,
        )
    assert error.value.code == "C4_SOURCE_DRIFT"
    assert not paths["acceptance"].exists()
    assert paths["binding"].read_bytes() == original_binding


def test_verify_refuses_when_binding_mutates_across_binder(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    bind(
        freeze_path=paths["freeze"],
        reviewed_sha=REVIEWED_SHA,
        cmd_start=CMD_START,
        cmd_end=CMD_END,
        output=paths["binding"],
        command_runner=_pass_runner,
    )

    def mutate_binding(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload = json.loads(paths["binding"].read_text())
        payload["bound_at"] = "2025-09-04T01:02:04.000001Z"
        _rewrite_private_json(paths["binding"], payload)
        return _pass_runner(argv, **kwargs)

    with pytest.raises(C4AcceptanceError) as error:
        verify(
            freeze_path=paths["freeze"],
            binding_path=paths["binding"],
            reviewed_sha=REVIEWED_SHA,
            output=paths["acceptance"],
            command_runner=mutate_binding,
        )
    assert error.value.code == "C4_SOURCE_DRIFT"
    assert not paths["acceptance"].exists()
    mutated = json.loads(paths["binding"].read_bytes())
    assert mutated["bound_at"] == "2025-09-04T01:02:04.000001Z"
    assert mutated["stage"] == "bind"


def test_freeze_refuses_parent_replaced_during_temp_creation(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.production_closure import c4_production_acceptance_io as io

    original_write = io.write_bytes_no_follow_exclusive
    replacement = paths["root"].parent / "replacement"
    _private_dir(replacement)

    def swap_parent(path, content, **kwargs):
        result = original_write(path, content, **kwargs)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        copy_fd = os.open(replacement / path.name, flags, FILE_MODE)
        try:
            os.write(copy_fd, path.read_bytes())
            os.fsync(copy_fd)
            os.fchmod(copy_fd, FILE_MODE)
        finally:
            os.close(copy_fd)
        os.rename(paths["root"], paths["root"].parent / "displaced")
        os.rename(replacement, paths["root"])
        return result

    monkeypatch.setattr(io, "write_bytes_no_follow_exclusive", swap_parent)
    with pytest.raises(C4AcceptanceError) as error:
        freeze(
            approved_record=paths["approved"],
            reviewed_sha=REVIEWED_SHA,
            receipt=paths["receipt"],
            frontend_origin=FRONTEND_ORIGIN,
            api_origin=API_ORIGIN,
            basin_id=BASIN_ID,
            segment_id=SEGMENT_ID,
            output=paths["freeze"],
            now=lambda: FREEZE_AT,
        )
    assert error.value.code == "C4_PARENT_DRIFT"
    assert not (paths["root"] / "freeze.json").exists()
    displaced = paths["root"].parent / "displaced"
    assert (displaced / "approved.json").exists()
    assert not (displaced / "freeze.json").exists()


def test_publish_refuses_existing_output_and_does_not_clobber(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    original = paths["freeze"].read_bytes()
    with pytest.raises(C4AcceptanceError) as error:
        freeze(
            approved_record=paths["approved"],
            reviewed_sha=REVIEWED_SHA,
            receipt=paths["receipt"],
            frontend_origin=FRONTEND_ORIGIN,
            api_origin=API_ORIGIN,
            basin_id=BASIN_ID,
            segment_id=SEGMENT_ID,
            output=paths["freeze"],
            now=lambda: FREEZE_AT,
        )
    assert error.value.code == "C4_EXISTS"
    assert paths["freeze"].read_bytes() == original


def test_publish_succeeds_without_clobbering_unrelated_sibling(paths: dict[str, Path]) -> None:
    stale = paths["root"] / ".freeze.json.tmp"
    _write_private_json(stale, {"stale": True})
    original_stale = stale.read_bytes()
    _freeze_ok(paths)
    assert paths["freeze"].exists()
    assert stale.exists()
    assert stale.read_bytes() == original_stale


def test_publish_refuses_duplicate_json_and_nonfinite_numbers() -> None:
    with pytest.raises(C4AcceptanceError) as duplicate:
        parse_closed_json(b'{"a":1,"a":2}', label="c4-acceptance")
    assert duplicate.value.code == "C4_JSON_DUPLICATE"
    with pytest.raises(C4AcceptanceError) as nonfinite:
        parse_closed_json(b'{"a":NaN}', label="c4-acceptance")
    assert nonfinite.value.code == "C4_JSON_INVALID"


def test_cli_freeze_bind_verify_with_injected_process_boundary(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner, "utc_now", lambda: FREEZE_AT)
    monkeypatch.setattr(owner, "_assert_trusted_node", lambda: None)
    monkeypatch.setattr(owner, "run_bounded_command", lambda argv, **kwargs: _pass_runner(argv, **kwargs))
    freeze_rc = cli_main(
        [
            "freeze",
            "--approved-record",
            str(paths["approved"]),
            "--reviewed-sha",
            REVIEWED_SHA,
            "--receipt",
            str(paths["receipt"]),
            "--frontend-origin",
            FRONTEND_ORIGIN,
            "--api-origin",
            API_ORIGIN,
            "--basin-id",
            BASIN_ID,
            "--segment-id",
            SEGMENT_ID,
            "--output",
            str(paths["freeze"]),
        ]
    )
    assert freeze_rc == 0
    _write_receipt(paths)
    bind_rc = cli_main(
        [
            "bind",
            "--freeze",
            str(paths["freeze"]),
            "--reviewed-sha",
            REVIEWED_SHA,
            "--cmd-start",
            CMD_START,
            "--cmd-end",
            CMD_END,
            "--output",
            str(paths["binding"]),
        ]
    )
    assert bind_rc == 0
    verify_rc = cli_main(
        [
            "verify",
            "--freeze",
            str(paths["freeze"]),
            "--binding",
            str(paths["binding"]),
            "--reviewed-sha",
            REVIEWED_SHA,
            "--output",
            str(paths["acceptance"]),
        ]
    )
    assert verify_rc == 0
    _raw, acceptance, _identity, _parent = read_private_json(paths["acceptance"])
    assert acceptance["status"] == "PASS"


def test_cli_usage_errors_are_static(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli_main(["freeze"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == "C4_USAGE: invalid arguments\n"
    assert "approved-record" not in captured.err


def test_cli_refuses_without_leaking_urls(paths: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(
        [
            "freeze",
            "--approved-record",
            str(paths["approved"]),
            "--reviewed-sha",
            OTHER_SHA,
            "--receipt",
            str(paths["receipt"]),
            "--frontend-origin",
            FRONTEND_ORIGIN,
            "--api-origin",
            API_ORIGIN,
            "--basin-id",
            BASIN_ID,
            "--segment-id",
            SEGMENT_ID,
            "--output",
            str(paths["freeze"]),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "C4_SHA_MISMATCH:" in captured.err
    assert FRONTEND_ORIGIN not in captured.err
    assert str(paths["approved"]) not in captured.err


def test_symlink_output_parent_is_refused(tmp_path: Path, paths: dict[str, Path]) -> None:
    real = _private_dir(tmp_path / "real-parent")
    link = tmp_path / "link-parent"
    link.symlink_to(real)
    output = link / "freeze.json"
    with pytest.raises(C4AcceptanceError) as error:
        freeze(
            approved_record=paths["approved"],
            reviewed_sha=REVIEWED_SHA,
            receipt=paths["receipt"],
            frontend_origin=FRONTEND_ORIGIN,
            api_origin=API_ORIGIN,
            basin_id=BASIN_ID,
            segment_id=SEGMENT_ID,
            output=output,
            now=lambda: FREEZE_AT,
        )
    assert error.value.code in {"C4_PARENT_OPEN", "C4_PARENT_INVALID"}
    assert not (real / "freeze.json").exists()


def test_complex_json_is_refused() -> None:
    nested: Any = {"k": 1}
    for _ in range(20):
        nested = {"k": nested}
    with pytest.raises(C4AcceptanceError) as error:
        parse_closed_json(json.dumps(nested).encode("utf-8"), label="c4-acceptance")
    assert error.value.code == "C4_JSON_COMPLEX"


def test_alias_between_output_and_approved_is_refused(paths: dict[str, Path]) -> None:
    with pytest.raises(C4AcceptanceError) as error:
        freeze(
            approved_record=paths["approved"],
            reviewed_sha=REVIEWED_SHA,
            receipt=paths["receipt"],
            frontend_origin=FRONTEND_ORIGIN,
            api_origin=API_ORIGIN,
            basin_id=BASIN_ID,
            segment_id=SEGMENT_ID,
            output=paths["approved"],
            now=lambda: FREEZE_AT,
        )
    assert error.value.code == "C4_ALIAS"


def test_real_node_binder_accepts_labeled_synthetic_fixture(paths: dict[str, Path]) -> None:
    if not Path("/usr/bin/node").is_file():
        pytest.skip("trusted Node runtime is absent")
    _freeze_ok(paths)
    _write_receipt(paths)
    bind(
        freeze_path=paths["freeze"],
        reviewed_sha=REVIEWED_SHA,
        cmd_start=CMD_START,
        cmd_end=CMD_END,
        output=paths["binding"],
    )
    _raw, binding, _identity, _parent = read_private_json(paths["binding"])
    assert binding["artifact"] == owner.BINDING_ARTIFACT
    verify(
        freeze_path=paths["freeze"],
        binding_path=paths["binding"],
        reviewed_sha=REVIEWED_SHA,
        output=paths["acceptance"],
    )
    _raw, acceptance, _identity, _parent = read_private_json(paths["acceptance"])
    assert acceptance["status"] == "PASS"


def test_real_node_binder_refuses_sha_mismatch_without_rewriting_freeze(
    paths: dict[str, Path],
) -> None:
    if not Path("/usr/bin/node").is_file():
        pytest.skip("trusted Node runtime is absent")
    _freeze_ok(paths)
    _write_receipt(paths)
    original = paths["freeze"].read_bytes()
    with pytest.raises(C4AcceptanceError) as error:
        bind(
            freeze_path=paths["freeze"],
            reviewed_sha=OTHER_SHA,
            cmd_start=CMD_START,
            cmd_end=CMD_END,
            output=paths["binding"],
        )
    assert error.value.code == "C4_SHA_MISMATCH"
    assert not paths["binding"].exists()
    assert paths["freeze"].read_bytes() == original
