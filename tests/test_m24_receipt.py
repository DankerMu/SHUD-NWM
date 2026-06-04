"""Requirement-driven contract tests for the shared M24 receipt schema."""

from __future__ import annotations

import json
import os
import stat

import pytest

from services.m24_live.receipt import (
    CONTRACT_ID,
    RECEIPT_SECTIONS,
    SCHEMA_VERSION,
    ReceiptValidationError,
    receipt_path,
    validate_receipt,
    write_receipt,
)


def _base_receipt(**overrides):
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "section": "baseline",
        "run_id": "m24-test-001",
        "node": "node-22",
        "command": "uv run python scripts/m24_emit_baseline.py --run-id m24-test-001",
        "timestamp": "2026-06-04T00:00:00+00:00",
        "status": "PASS",
        "execution_mode": "deterministic",
        "live_proof_accepted": False,
        "dependency_blocker": None,
        "redaction": {"db_dsn_redacted": True, "bounds": {}},
        "artifact_refs": [{"kind": "log", "uri": "file:///tmp/x.log"}],
        "identity": {
            "source": "multi",
            "cycle_time": "2026-06-04T00:00:00Z",
            "model_id": None,
            "basin_id": None,
            "basin_version_id": None,
            "river_network_version_id": None,
        },
        "stages": [{"stage": "db_identity", "status": "PASS", "counts": {}}],
        "slurm": {
            "job_id": None,
            "array_task_id": None,
            "original_task_id": None,
            "accounting": None,
            "log_uri": None,
        },
        "published_uri": None,
        "warm_start_quality": None,
    }
    receipt.update(overrides)
    return receipt


# --- happy paths ----------------------------------------------------------------


def test_valid_pass_baseline_receipt():
    validate_receipt(_base_receipt())


def test_valid_blocked_receipt():
    receipt = _base_receipt(
        status="BLOCKED",
        dependency_blocker="DATABASE_URL missing",
        live_proof_accepted=False,
    )
    validate_receipt(receipt)


def test_valid_live_proof_receipt():
    receipt = _base_receipt(
        execution_mode="live_proof",
        live_proof_accepted=True,
        warm_start_quality="fresh",
        published_uri="s3://bucket/out.nc",
        slurm={
            "job_id": "12345",
            "array_task_id": 3,
            "original_task_id": 1,
            "accounting": {"state": "COMPLETED"},
            "log_uri": "file:///scratch/log/12345.out",
        },
    )
    validate_receipt(receipt)


# --- missing required fields -----------------------------------------------------


@pytest.mark.parametrize(
    "missing_key",
    [
        "schema_version",
        "contract_id",
        "run_id",
        "node",
        "command",
        "timestamp",
        "status",
        "execution_mode",
        "live_proof_accepted",
        "dependency_blocker",
        "redaction",
        "artifact_refs",
        "identity",
        "stages",
        "slurm",
        "published_uri",
        "warm_start_quality",
    ],
)
def test_missing_required_top_key_raises(missing_key):
    receipt = _base_receipt()
    del receipt[missing_key]
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


# --- enum violations -------------------------------------------------------------


def test_invalid_status_enum_raises():
    with pytest.raises(ReceiptValidationError):
        validate_receipt(_base_receipt(status="OK"))


def test_invalid_execution_mode_enum_raises():
    with pytest.raises(ReceiptValidationError):
        validate_receipt(_base_receipt(execution_mode="batch"))


def test_invalid_warm_start_quality_enum_raises():
    with pytest.raises(ReceiptValidationError):
        validate_receipt(_base_receipt(warm_start_quality="warm"))


# --- hard BLOCKED rules ----------------------------------------------------------


def test_blocked_without_dependency_blocker_raises():
    receipt = _base_receipt(status="BLOCKED", dependency_blocker=None)
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


def test_blocked_with_empty_dependency_blocker_raises():
    receipt = _base_receipt(status="BLOCKED", dependency_blocker="   ")
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


def test_blocked_with_live_proof_accepted_true_raises():
    receipt = _base_receipt(
        status="BLOCKED",
        dependency_blocker="dep down",
        live_proof_accepted=True,
    )
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


# --- nested structure violations -------------------------------------------------


def test_identity_missing_subkey_raises():
    receipt = _base_receipt()
    del receipt["identity"]["model_id"]
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


def test_redaction_missing_subkey_raises():
    receipt = _base_receipt()
    del receipt["redaction"]["bounds"]
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


def test_redaction_db_dsn_redacted_not_bool_raises():
    receipt = _base_receipt(redaction={"db_dsn_redacted": "yes", "bounds": {}})
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


def test_slurm_missing_subkey_raises():
    receipt = _base_receipt()
    del receipt["slurm"]["accounting"]
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


def test_artifact_refs_element_missing_uri_raises():
    receipt = _base_receipt(artifact_refs=[{"kind": "log"}])
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


def test_artifact_refs_element_not_object_raises():
    receipt = _base_receipt(artifact_refs=["not-an-object"])
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


def test_stages_element_missing_counts_raises():
    receipt = _base_receipt(stages=[{"stage": "db", "status": "PASS"}])
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


def test_stages_element_invalid_status_raises():
    receipt = _base_receipt(stages=[{"stage": "db", "status": "WAT", "counts": {}}])
    with pytest.raises(ReceiptValidationError):
        validate_receipt(receipt)


# --- timestamp -------------------------------------------------------------------


def test_non_iso_timestamp_raises():
    with pytest.raises(ReceiptValidationError):
        validate_receipt(_base_receipt(timestamp="June 4th 2026"))


# --- receipt_path / write_receipt ------------------------------------------------


def test_receipt_path_layout_and_unknown_section(tmp_path):
    path = receipt_path("run-9", "baseline", root=tmp_path)
    assert path == tmp_path / "run-9" / "baseline.json"
    assert (tmp_path / "run-9").is_dir()
    with pytest.raises(ReceiptValidationError):
        receipt_path("run-9", "bogus_section", root=tmp_path)


def test_receipt_path_rejects_unsafe_run_id(tmp_path):
    with pytest.raises(ReceiptValidationError):
        receipt_path("../escape", "baseline", root=tmp_path)


def test_all_known_sections_resolve(tmp_path):
    for section in RECEIPT_SECTIONS:
        path = receipt_path("run-sections", section, root=tmp_path)
        assert path.name == f"{section}.json"


def test_write_receipt_round_trip_perms_and_validate(tmp_path):
    receipt = _base_receipt()
    path = write_receipt(receipt, root=tmp_path)
    assert path == tmp_path / receipt["run_id"] / "baseline.json"

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    reloaded = json.loads(path.read_text())
    validate_receipt(reloaded)
    assert reloaded["run_id"] == receipt["run_id"]


def test_write_receipt_requires_known_section(tmp_path):
    receipt = _base_receipt()
    del receipt["section"]
    with pytest.raises(ReceiptValidationError):
        write_receipt(receipt, root=tmp_path)


def test_write_receipt_redacts_dsn(tmp_path):
    # A DSN-like value embedded anywhere must not land on disk verbatim.
    receipt = _base_receipt(
        artifact_refs=[{"kind": "dsn", "uri": "postgresql://user:secretpw@host:5432/nhms"}]
    )
    path = write_receipt(receipt, root=tmp_path)
    raw = path.read_text()
    assert "secretpw" not in raw
