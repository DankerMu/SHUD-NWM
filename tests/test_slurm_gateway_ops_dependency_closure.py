"""Dependency-closure evidence coverage for production-ops validation.

Owns the accepted/proven dependency summary and receipt tests for
``services.production_closure.ops_validation``: real producer shapes,
acceptance receipts, live-proof blockers, and the unproven/claimed
rejections. Shared helpers (``_read_json``, ``_write_dependency_summary``,
``_write_dependency_acceptance_receipt``) live in
``tests/test_production_ops_validation.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.production_closure.ops_validation import (
    ProductionOpsConfig,
    ProductionOpsValidationError,
    validate_ops,
)
from tests.test_production_ops_validation import (
    _read_json,
    _write_dependency_acceptance_receipt,
    _write_dependency_summary,
)


def test_validate_ops_dependency_closure_accepts_real_summaries_but_keeps_live_control_gate(
    tmp_path: Path,
) -> None:
    roots: dict[str, Path] = {}
    for name, issue, schema, status in [
        ("slurm", 147, "nhms.production_closure.slurm.v1", "submitted"),
        ("object_store", 148, "nhms.production_closure.object_store.v1", "ready"),
        ("met", 149, "nhms.production_closure.met.v1", "ready"),
        ("e2e", 150, "nhms.production_closure.e2e.v1", "ready"),
        ("scale", 151, "nhms.production_closure.scale.v1", "ready"),
    ]:
        root = tmp_path / name
        root.mkdir()
        _write_dependency_summary(root / "summary.json", name, issue, schema, status, accepted=True)
        roots[name] = root

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="accepted_deps",
            slurm_evidence_root=roots["slurm"],
            object_store_evidence_root=roots["object_store"],
            met_evidence_root=roots["met"],
            e2e_evidence_root=roots["e2e"],
            scale_evidence_root=roots["scale"],
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "accepted_deps" / "ops" / "dependency_closure.json")
    assert dependency["status"] == "accepted"
    assert {item["status"] for item in dependency["dependencies"]} == {"accepted"}
    assert dependency["blockers"] == []
    assert dependency["final_production_readiness_claimed"] is False
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "accepted"
    assert summary["final_production_readiness_claimed"] is False


def test_validate_ops_dependency_closure_requires_external_acceptance_receipt_for_producer_summary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        extra={
            "execution_mode": "accepted_live_evidence",
            "deterministic_fixture": False,
            "final_production_readiness_claimed": False,
            "live_slurm_executed": True,
            "live_slurm_status": "executed",
        },
    )

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="producer_without_receipt",
            slurm_evidence_root=root,
        )
    )
    dependency = _read_json(tmp_path / "artifacts" / "producer_without_receipt" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_MISSING"
    assert "accepted_dependency_evidence.json" in slurm["reason"]

    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")
    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="producer_with_receipt",
            slurm_evidence_root=root,
        )
    )
    accepted_dependency = _read_json(
        tmp_path / "artifacts" / "producer_with_receipt" / "ops" / "dependency_closure.json"
    )
    accepted_slurm = next(item for item in accepted_dependency["dependencies"] if item["dependency"] == "slurm")
    assert accepted_slurm["status"] == "accepted"
    assert accepted_slurm["accepted_dependency_evidence"]["summary_sha256"] == hashlib.sha256(
        summary_path.read_bytes()
    ).hexdigest()
    assert accepted_slurm["accepted_dependency_evidence"]["receipt_path"] == "[redacted]"
    assert str(root) not in json.dumps(accepted_slurm)


def test_validate_ops_accepts_object_store_fast_summary_with_live_proof_blocker(tmp_path: Path) -> None:
    root = tmp_path / "object_store"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "object_store",
        148,
        "nhms.production_closure.object_store.v1",
        "ready",
        extra={
            "execution_mode": "deterministic_fixture",
            "deterministic_fixture": True,
            "live_registry_import": False,
            "live_api": False,
            "live_api_status": "not_executed",
            "api_contract_source": "local_import_source",
            "final_production_readiness_claimed": False,
        },
    )
    _write_dependency_acceptance_receipt(summary_path, "object_store", 148, "nhms.production_closure.object_store.v1")

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="object_store_fast_with_receipt",
            object_store_evidence_root=root,
        )
    )

    dependency = _read_json(
        tmp_path / "artifacts" / "object_store_fast_with_receipt" / "ops" / "dependency_closure.json"
    )
    object_store = next(item for item in dependency["dependencies"] if item["dependency"] == "object_store")
    assert object_store["status"] == "accepted"
    assert object_store["deterministic_fixture"] is False
    assert object_store["summary_deterministic_fixture"] is True
    assert object_store["accepted_dependency_evidence"]["summary_sha256"] == hashlib.sha256(
        summary_path.read_bytes()
    ).hexdigest()
    assert object_store["release_blockers"][0]["error_code"] == (
        "PRODUCTION_OPS_DEPENDENCY_PRODUCER_LIVE_PROOF_MISSING"
    )
    assert dependency["status"] == "release_blocked"


def test_validate_ops_accepts_object_store_only_with_summary_live_proof_and_receipt(tmp_path: Path) -> None:
    root = tmp_path / "object_store"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "object_store",
        148,
        "nhms.production_closure.object_store.v1",
        "ready",
        extra={
            "execution_mode": "accepted_live_evidence",
            "deterministic_fixture": False,
            "live_registry_import": True,
            "live_api": True,
            "live_api_status": "executed",
            "final_production_readiness_claimed": False,
        },
    )
    _write_dependency_acceptance_receipt(summary_path, "object_store", 148, "nhms.production_closure.object_store.v1")

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="object_store_live_with_receipt",
            object_store_evidence_root=root,
        )
    )

    dependency = _read_json(
        tmp_path / "artifacts" / "object_store_live_with_receipt" / "ops" / "dependency_closure.json"
    )
    object_store = next(item for item in dependency["dependencies"] if item["dependency"] == "object_store")
    assert object_store["status"] == "accepted"
    assert object_store["deterministic_fixture"] is False
    assert object_store["accepted_dependency_evidence"]["summary_sha256"] == hashlib.sha256(
        summary_path.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    ("name", "issue", "schema", "status", "extra"),
    [
        (
            "object_store",
            148,
            "nhms.production_closure.object_store.v1",
            "ready",
            {
                "execution_mode": "deterministic_fixture",
                "deterministic_fixture": True,
                "live_registry_import": False,
                "live_api": False,
                "live_api_status": "not_executed",
                "final_production_readiness_claimed": False,
            },
        ),
        (
            "e2e",
            150,
            "nhms.production_closure.e2e.v1",
            "ready",
            {
                "execution_mode": "deterministic_fixture",
                "deterministic_fixture": True,
                "live_db_executed": False,
                "live_api_executed": False,
                "live_slurm_executed": False,
                "live_frontend_executed": False,
                "final_production_readiness_claimed": False,
            },
        ),
        (
            "scale",
            151,
            "nhms.production_closure.scale.v1",
            "ready",
            {
                "execution_mode": "deterministic_fixture",
                "deterministic_fixture": True,
                "live_db_executed": False,
                "live_api_executed": False,
                "live_frontend_executed": False,
                "final_production_readiness_claimed": False,
            },
        ),
    ],
)
def test_validate_ops_consumes_real_producer_summary_shapes_with_receipt_as_accepted_blocked(
    tmp_path: Path,
    name: str,
    issue: int,
    schema: str,
    status: str,
    extra: dict,
) -> None:
    root = tmp_path / name
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(summary_path, name, issue, schema, status, extra=extra)
    _write_dependency_acceptance_receipt(summary_path, name, issue, schema)

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id=f"{name}_real_shape_with_receipt",
            object_store_evidence_root=root if name == "object_store" else None,
            e2e_evidence_root=root if name == "e2e" else None,
            scale_evidence_root=root if name == "scale" else None,
        )
    )

    dependency = _read_json(
        tmp_path / "artifacts" / f"{name}_real_shape_with_receipt" / "ops" / "dependency_closure.json"
    )
    item = next(item for item in dependency["dependencies"] if item["dependency"] == name)
    assert item["status"] == "accepted"
    assert item["summary_deterministic_fixture"] is True
    assert item["accepted_dependency_evidence"]["summary_sha256"] == hashlib.sha256(
        summary_path.read_bytes()
    ).hexdigest()
    assert item["release_blockers"][0]["error_code"] == "PRODUCTION_OPS_DEPENDENCY_PRODUCER_LIVE_PROOF_MISSING"
    assert dependency["status"] == "release_blocked"


def test_validate_ops_accepts_live_dependency_with_unrelated_false_live_fields(tmp_path: Path) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
        extra={
            "live_alert_sink_delivered": False,
            "live_frontend_executed": False,
            "live_registry_import": False,
            "live_api": False,
            "live_api_status": "not_executed",
        },
    )
    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="accepted_live_with_unrelated_false_fields",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(
        tmp_path
        / "artifacts"
        / "accepted_live_with_unrelated_false_fields"
        / "ops"
        / "dependency_closure.json"
    )
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "accepted"
    assert slurm["deterministic_fixture"] is False


def test_validate_ops_rejects_spoofed_live_field_even_with_receipt(tmp_path: Path) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        extra={
            "execution_mode": "accepted_live_evidence",
            "deterministic_fixture": False,
            "final_production_readiness_claimed": False,
            "live_spoof": True,
        },
    )
    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="spoofed_live_field",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "spoofed_live_field" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "accepted"
    assert slurm["deterministic_fixture"] is False
    assert slurm["release_blockers"][0]["error_code"] == "PRODUCTION_OPS_DEPENDENCY_PRODUCER_LIVE_PROOF_MISSING"


def test_validate_ops_dependency_receipt_uses_bounded_summary_digest_without_second_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        extra={
            "execution_mode": "accepted_live_evidence",
            "deterministic_fixture": False,
            "final_production_readiness_claimed": False,
            "live_slurm_executed": True,
            "live_slurm_status": "executed",
        },
    )
    summary_bytes = summary_path.read_bytes()
    expected_digest = hashlib.sha256(summary_bytes).hexdigest()
    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")

    original_read_bytes = Path.read_bytes

    def fail_summary_read_bytes(path: Path) -> bytes:
        if path == summary_path.resolve():
            raise AssertionError("summary.json must not be re-read for receipt checksum validation")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_summary_read_bytes)

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="producer_receipt_single_read_digest",
            slurm_evidence_root=root,
        )
    )
    dependency = _read_json(
        tmp_path / "artifacts" / "producer_receipt_single_read_digest" / "ops" / "dependency_closure.json"
    )
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "accepted"
    assert slurm["accepted_dependency_evidence"]["summary_sha256"] == expected_digest


@pytest.mark.parametrize(
    ("summary_fields", "expected_status"),
    [
        ({"deterministic_fixture": True}, "skipped"),
        ({"execution_mode": "deterministic_fixture"}, "skipped"),
        ({"live_slurm_executed": False}, "skipped"),
        ({}, "blocked"),
        (
            {
                "accepted_dependency_evidence": {
                    "accepted": True,
                    "receipt_id": "missing-fields",
                    "execution_mode": "accepted_live_evidence",
                    "deterministic_fixture": False,
                    "final_production_readiness_claimed": False,
                }
            },
            "blocked",
        ),
    ],
)
def test_validate_ops_dependency_closure_rejects_unproven_ready_summaries(
    tmp_path: Path,
    summary_fields: dict,
    expected_status: str,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    _write_dependency_summary(
        root / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        extra=summary_fields,
    )

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="unproven_deps",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "unproven_deps" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == expected_status
    assert slurm["final_production_readiness_claimed"] is False
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_MISSING"
    assert dependency["status"] == "release_blocked"
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"


def test_validate_ops_dependency_closure_rejects_final_readiness_claimed_summary(tmp_path: Path) -> None:
    root = tmp_path / "object_store"
    root.mkdir()
    _write_dependency_summary(
        root / "summary.json",
        "object_store",
        148,
        "nhms.production_closure.object_store.v1",
        "ready",
        accepted=True,
        extra={"final_production_readiness_claimed": True},
    )

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="final_claimed_dep",
            object_store_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "final_claimed_dep" / "ops" / "dependency_closure.json")
    object_store = next(item for item in dependency["dependencies"] if item["dependency"] == "object_store")
    assert object_store["status"] == "blocked"
    assert object_store["summary_final_production_readiness_claimed"] is True
    assert object_store["final_production_readiness_claimed"] is False
    assert object_store["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_MISSING"
    assert summary["status"] == "release_blocked"


def test_validate_ops_dependency_statuses_record_skipped_blocked_and_not_executed(tmp_path: Path) -> None:
    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_statuses",
            dependency_statuses="slurm=skipped,object_store=skipped,met=blocked,e2e=not_executed,scale=blocked",
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "dep_statuses" / "ops" / "dependency_closure.json")
    statuses = {item["dependency"]: item["status"] for item in dependency["dependencies"]}
    assert statuses == {
        "slurm": "skipped",
        "object_store": "skipped",
        "met": "blocked",
        "e2e": "not_executed",
        "scale": "blocked",
    }
    assert dependency["deterministic_fixture"] is True
    assert summary["status"] == "release_blocked"


def test_validate_ops_rejects_explicit_accepted_dependency_status(tmp_path: Path) -> None:
    with pytest.raises(ProductionOpsValidationError) as exc_info:
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="accepted_status",
            dependency_statuses="slurm=accepted",
        )

    assert exc_info.value.error_code == "PRODUCTION_OPS_DEPENDENCY_STATUS_INVALID"
