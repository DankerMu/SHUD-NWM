"""Public-boundary refusals for Bringup-C4 freeze/bind/verify.

These cases live beside ``tests/test_node27_c4_production_acceptance.py`` so
each module stays under the 1,000-line structural limit. Helpers and the
labeled synthetic fixture are reused from that suite; this file does not
reimplement them.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.production_closure.c4_production_acceptance import bind, freeze, verify
from services.production_closure.c4_production_acceptance_io import (
    FILE_MODE,
    MAX_RECORD_BYTES,
    C4AcceptanceError,
    read_private_json,
)
from tests.test_node27_c4_production_acceptance import (
    API_ORIGIN,
    BASIN_ID,
    CMD_END,
    CMD_START,
    FRONTEND_ORIGIN,
    REVIEWED_SHA,
    SEGMENT_ID,
    _freeze_ok,
    _pass_runner,
    _private_dir,
    _rewrite_private_json,
    _write_private_json,
    _write_receipt,
)
from tests.test_node27_c4_production_acceptance import (
    paths as paths,
)

UPPERCASE_SHA = "0123456789ABCDEF0123456789ABCDEF01234567"
QUERY_ORIGIN = "https://display.example.test/?lane=c4"
OVERSIZE_NOTE = "x" * (MAX_RECORD_BYTES + 1)


def _bind_ok(paths: dict[str, Path]) -> None:
    bind(
        freeze_path=paths["freeze"],
        reviewed_sha=REVIEWED_SHA,
        cmd_start=CMD_START,
        cmd_end=CMD_END,
        output=paths["binding"],
        command_runner=_pass_runner,
    )


def _replace_same_bytes_new_inode(path: Path) -> None:
    """Delete and recreate identical bytes on a new inode.

    The replacement file is created first so the original inode cannot be
    reused after unlink.
    """

    original = path.read_bytes()
    sibling = path.with_name(f".{path.name}.replacement")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(sibling, flags, FILE_MODE)
    try:
        os.write(fd, original)
        os.fsync(fd)
        os.fchmod(fd, FILE_MODE)
    finally:
        os.close(fd)
    os.unlink(path)
    os.rename(sibling, path)
    os.chmod(path, FILE_MODE)


def test_public_boundary_parameter_batch_refuses_without_publication(tmp_path: Path, paths: dict[str, Path]) -> None:
    cases = (
        (
            "C4_SHA_INVALID",
            lambda: freeze(
                approved_record=paths["approved"],
                reviewed_sha=UPPERCASE_SHA,
                receipt=paths["receipt"],
                frontend_origin=FRONTEND_ORIGIN,
                api_origin=API_ORIGIN,
                basin_id=BASIN_ID,
                segment_id=SEGMENT_ID,
                output=paths["freeze"],
            ),
        ),
        (
            "C4_ORIGIN_INVALID",
            lambda: freeze(
                approved_record=paths["approved"],
                reviewed_sha=REVIEWED_SHA,
                receipt=paths["receipt"],
                frontend_origin=QUERY_ORIGIN,
                api_origin=API_ORIGIN,
                basin_id=BASIN_ID,
                segment_id=SEGMENT_ID,
                output=paths["freeze"],
            ),
        ),
        (
            "C4_BASIN_INVALID",
            lambda: freeze(
                approved_record=paths["approved"],
                reviewed_sha=REVIEWED_SHA,
                receipt=paths["receipt"],
                frontend_origin=FRONTEND_ORIGIN,
                api_origin=API_ORIGIN,
                basin_id="",
                segment_id=SEGMENT_ID,
                output=paths["freeze"],
            ),
        ),
        (
            "C4_SEGMENT_INVALID",
            lambda: freeze(
                approved_record=paths["approved"],
                reviewed_sha=REVIEWED_SHA,
                receipt=paths["receipt"],
                frontend_origin=FRONTEND_ORIGIN,
                api_origin=API_ORIGIN,
                basin_id=BASIN_ID,
                segment_id="",
                output=paths["freeze"],
            ),
        ),
        (
            "C4_BRACKET_INVALID",
            lambda: (
                _freeze_ok(paths),
                _write_receipt(paths),
                bind(
                    freeze_path=paths["freeze"],
                    reviewed_sha=REVIEWED_SHA,
                    cmd_start=CMD_END,
                    cmd_end=CMD_START,
                    output=paths["binding"],
                    command_runner=_pass_runner,
                ),
            )[-1],
        ),
        (
            "C4_PATH_INVALID",
            lambda: freeze(
                approved_record=paths["approved"],
                reviewed_sha=REVIEWED_SHA,
                receipt="c4-receipt.json",
                frontend_origin=FRONTEND_ORIGIN,
                api_origin=API_ORIGIN,
                basin_id=BASIN_ID,
                segment_id=SEGMENT_ID,
                output=paths["freeze"],
            ),
        ),
        (
            "C4_TOO_LARGE",
            lambda: freeze(
                approved_record=_write_oversize_approved(tmp_path),
                reviewed_sha=REVIEWED_SHA,
                receipt=paths["receipt"],
                frontend_origin=FRONTEND_ORIGIN,
                api_origin=API_ORIGIN,
                basin_id=BASIN_ID,
                segment_id=SEGMENT_ID,
                output=_private_dir(tmp_path / "oversize-out") / "freeze.json",
            ),
        ),
    )
    freeze_rows = cases[:4] + cases[5:]
    for code, action in freeze_rows:
        with pytest.raises(C4AcceptanceError) as error:
            action()
        assert error.value.code == code
        assert not paths["freeze"].exists()
        assert not paths["binding"].exists()
        assert not paths["acceptance"].exists()
    bracket_code, bracket_action = cases[4]
    with pytest.raises(C4AcceptanceError) as error:
        bracket_action()
    assert error.value.code == bracket_code
    assert not paths["binding"].exists()
    assert not paths["acceptance"].exists()


def _write_oversize_approved(tmp_path: Path) -> Path:
    root = _private_dir(tmp_path / "oversize")
    approved = root / "approved.json"
    _write_private_json(
        approved,
        {
            "status": "PASS",
            "head_sha": REVIEWED_SHA,
            "reviewed_sha": REVIEWED_SHA,
            "note": OVERSIZE_NOTE,
        },
    )
    return approved


def test_verify_refuses_same_byte_inode_replacement_after_bind(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    _bind_ok(paths)
    original_binding = paths["binding"].read_bytes()
    original_freeze = paths["freeze"].read_bytes()
    original_ino = paths["receipt"].stat().st_ino
    _replace_same_bytes_new_inode(paths["receipt"])
    assert paths["receipt"].stat().st_ino != original_ino
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
    assert paths["freeze"].read_bytes() == original_freeze


def test_verify_refuses_utime_only_receipt_drift_after_bind(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    _bind_ok(paths)
    original_binding = paths["binding"].read_bytes()
    original_bytes = paths["receipt"].read_bytes()
    later = int(datetime(2025, 9, 4, 2, 0, 0, tzinfo=UTC).timestamp())
    os.utime(paths["receipt"], (later, later), follow_symlinks=False)
    assert paths["receipt"].read_bytes() == original_bytes
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


def test_bind_refuses_absent_freeze_without_binding(paths: dict[str, Path]) -> None:
    _write_receipt(paths)
    assert not paths["freeze"].exists()
    with pytest.raises(C4AcceptanceError) as error:
        bind(
            freeze_path=paths["freeze"],
            reviewed_sha=REVIEWED_SHA,
            cmd_start=CMD_START,
            cmd_end=CMD_END,
            output=paths["binding"],
            command_runner=_pass_runner,
        )
    assert error.value.code == "C4_MISSING"
    assert not paths["binding"].exists()


def test_verify_refuses_valid_cross_freeze_binding(tmp_path: Path, paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    _bind_ok(paths)
    other_root = _private_dir(tmp_path / "other")
    other = {
        "root": other_root,
        "approved": other_root / "approved.json",
        "receipt": other_root / "c4-receipt.json",
        "freeze": other_root / "freeze.json",
        "binding": other_root / "binding.json",
        "acceptance": other_root / "acceptance.json",
    }
    _write_private_json(
        other["approved"],
        {
            "status": "PASS",
            "head_sha": REVIEWED_SHA,
            "reviewed_sha": REVIEWED_SHA,
            "note": "second operator-supplied delivery record",
        },
    )
    freeze(
        approved_record=other["approved"],
        reviewed_sha=REVIEWED_SHA,
        receipt=other["receipt"],
        frontend_origin=FRONTEND_ORIGIN,
        api_origin=API_ORIGIN,
        basin_id=BASIN_ID,
        segment_id=SEGMENT_ID,
        output=other["freeze"],
        now=lambda: datetime(2025, 9, 4, 0, 59, 58, tzinfo=UTC),
    )
    original_binding = paths["binding"].read_bytes()
    with pytest.raises(C4AcceptanceError) as error:
        verify(
            freeze_path=other["freeze"],
            binding_path=paths["binding"],
            reviewed_sha=REVIEWED_SHA,
            output=paths["acceptance"],
            command_runner=_pass_runner,
        )
    assert error.value.code == "C4_FREEZE_MISMATCH"
    assert not paths["acceptance"].exists()
    assert paths["binding"].read_bytes() == original_binding


def test_verify_refuses_edited_valid_binding_input(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    _bind_ok(paths)
    _raw, binding, _identity, _parent = read_private_json(paths["binding"])
    mutated: dict[str, Any] = dict(binding)
    mutated["c4_inputs"] = dict(binding["c4_inputs"])
    mutated["c4_inputs"]["basin_id"] = "basins_other"
    _rewrite_private_json(paths["binding"], mutated)
    with pytest.raises(C4AcceptanceError) as error:
        verify(
            freeze_path=paths["freeze"],
            binding_path=paths["binding"],
            reviewed_sha=REVIEWED_SHA,
            output=paths["acceptance"],
            command_runner=_pass_runner,
        )
    assert error.value.code == "C4_INPUT_MISMATCH"
    assert not paths["acceptance"].exists()


def test_verify_refuses_extra_key_freeze(paths: dict[str, Path]) -> None:
    _freeze_ok(paths)
    _write_receipt(paths)
    _bind_ok(paths)
    _raw, freeze_document, _identity, _parent = read_private_json(paths["freeze"])
    mutated = dict(freeze_document)
    mutated["extra"] = "not-in-schema"
    _rewrite_private_json(paths["freeze"], mutated)
    with pytest.raises(C4AcceptanceError) as error:
        verify(
            freeze_path=paths["freeze"],
            binding_path=paths["binding"],
            reviewed_sha=REVIEWED_SHA,
            output=paths["acceptance"],
            command_runner=_pass_runner,
        )
    assert error.value.code == "C4_FREEZE_SCHEMA"
    assert not paths["acceptance"].exists()


def test_cli_closed_stdout_after_success_keeps_published_artifact(
    paths: dict[str, Path],
) -> None:
    """Successful freeze with a closed stdout reader is owner-coded, not rolled back."""

    argv = [
        sys.executable,
        str(Path("scripts/node27_c4_production_acceptance.py").resolve()),
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
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not paths["freeze"].exists():
        if process.poll() is not None:
            break
        time.sleep(0.05)
    ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
    if ready:
        process.stdout.read(1)
    process.stdout.close()
    stderr = process.stderr.read()
    rc = process.wait(timeout=10)
    assert rc == 1
    assert "C4_STDOUT_CLOSED:" in stderr
    assert "BrokenPipeError" not in stderr
    assert "Exception ignored" not in stderr
    assert paths["freeze"].exists()
    _raw, document, _identity, _parent = read_private_json(paths["freeze"])
    assert document["stage"] == "freeze"
    assert document["reviewed_sha"] == REVIEWED_SHA
    assert not paths["binding"].exists()
