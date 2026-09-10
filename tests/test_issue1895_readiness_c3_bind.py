"""C3 PASS binder nested-C4 semantic contracts. Helpers imported from the C3 suite."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from packages.common.node27_issue1895_private_receipt import publish_private_receipt
from packages.common.node27_issue1895_publication_current import bind_c3_receipt
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from tests.test_issue1895_readiness_c1_c2_c3 import SHA, WRONG_SHA, _current_bracket, _private
from tests.test_issue1895_readiness_c3 import (
    _c3_document,
    _c4_pass_document,
    _registry_document,
    _registry_payload,
)


def _write_private_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def _shipping_c4() -> dict[str, Any]:
    gfs = {
        "run_id": "run-gfs",
        "model_id": "model",
        "basin_id": "basin",
        "basin_version_id": "bv",
        "river_network_version_id": "rv",
        "source_id": "GFS",
        "cycle_time": "2026-09-06T12:00:00Z",
        "status": "published",
    }
    ifs = {**gfs, "run_id": "run-ifs", "source_id": "IFS"}
    return _c4_pass_document(gfs, ifs)


def _align_ops(document: dict[str, Any], c4_payload: Mapping[str, Any]) -> dict[str, Any]:
    document["sources"]["GFS"]["c4_job_id"] = c4_payload["ops"]["gfs"]["job_id"]
    document["sources"]["IFS"]["c4_job_id"] = c4_payload["ops"]["ifs"]["job_id"]
    document["sources"]["GFS"]["c4_logs_status"] = c4_payload["ops"]["gfs"]["logs_status"]
    document["sources"]["IFS"]["c4_logs_status"] = c4_payload["ops"]["ifs"]["logs_status"]
    return document


def _published_bind_inputs(
    tmp_path: Path,
    c4_payload: dict[str, Any],
    *,
    align_ops: bool = False,
    mutate_document: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[Path, Path, Path, bytes]:
    parent = tmp_path / "private"
    _private(parent)
    c4 = parent / "c4.json"
    _write_private_json(c4, c4_payload)
    registry = parent / "manifest-last.json"
    _write_private_json(
        registry,
        _registry_payload(
            generated_at="2026-09-06T11:00:00Z",
            models=[{"model_id": "model", "basin_id": "basin"}],
        ),
    )
    document = _c3_document(c4)
    document["registry_facts"] = _registry_document(registry)
    document["registry_sha256"] = document["registry_facts"]["sha256"]
    document["registry_generated_at"] = "2026-09-06T11:00:00Z"
    if align_ops:
        document = _align_ops(document, c4_payload)
    if mutate_document is not None:
        document = mutate_document(document)
    receipt = parent / "c3.json"
    publish_private_receipt(receipt, document, code_prefix="C3_RECEIPT", stage="c3")
    return receipt, c4, registry, receipt.read_bytes()


def _bind(
    receipt: Path,
    c4: Path,
    registry: Path,
    *,
    reviewed_sha: str = SHA,
    basin_id: str = "basin",
    cmd_start: str | None = None,
    cmd_end: str | None = None,
) -> dict[str, Any]:
    if cmd_start is None or cmd_end is None:
        cmd_start, cmd_end = _current_bracket()
    return bind_c3_receipt(
        receipt,
        reviewed_sha=reviewed_sha,
        basin_id=basin_id,
        c4_receipt=c4,
        registry=registry,
        canonical_registry_path=registry,
        allow_test_registry_override=True,
        cmd_start=cmd_start,
        cmd_end=cmd_end,
    )


def _assert_bind_refuses(
    *,
    receipt: Path,
    c4: Path,
    registry: Path,
    original: bytes,
    code: str,
    reviewed_sha: str = SHA,
    basin_id: str = "basin",
    cmd_start: str | None = None,
    cmd_end: str | None = None,
) -> None:
    with pytest.raises(Issue1895ReadinessError) as refused:
        _bind(
            receipt,
            c4,
            registry,
            reviewed_sha=reviewed_sha,
            basin_id=basin_id,
            cmd_start=cmd_start,
            cmd_end=cmd_end,
        )
    assert refused.value.code == code
    assert receipt.read_bytes() == original


def test_c3_binder_accepts_shipping_shape_c4_pass(tmp_path: Path) -> None:
    payload = _shipping_c4()
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload, align_ops=True)
    bound = _bind(receipt, c4, registry)
    assert bound["status"] == "PASS"
    assert bound["sources"]["GFS"]["c4_job_id"] == "gfs-job"
    assert bound["sources"]["IFS"]["c4_job_id"] == "ifs-job"
    assert receipt.read_bytes() == original


def test_c3_binder_refuses_empty_c4_object(tmp_path: Path) -> None:
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, {})
    _assert_bind_refuses(receipt=receipt, c4=c4, registry=registry, original=original, code="C3_C4_SCHEMA")


@pytest.mark.parametrize(
    ("status", "failure"),
    (("FAIL", None), ("BLOCKED", {"reason": "ops"}), ("PASS", {"reason": "ops"})),
)
def test_c3_binder_refuses_non_pass_or_failed_c4(
    tmp_path: Path, status: str, failure: dict[str, str] | None
) -> None:
    payload = _shipping_c4()
    payload["status"] = status
    payload["failure"] = failure
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload, align_ops=True)
    _assert_bind_refuses(receipt=receipt, c4=c4, registry=registry, original=original, code="C3_C4_STATUS")


@pytest.mark.parametrize("fault", ("extra", "missing"))
def test_c3_binder_refuses_c4_closed_key_violations(tmp_path: Path, fault: str) -> None:
    payload = _shipping_c4()
    if fault == "extra":
        payload["extra"] = True
    else:
        del payload["home"]
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload, align_ops=True)
    _assert_bind_refuses(receipt=receipt, c4=c4, registry=registry, original=original, code="C3_C4_SCHEMA")


def test_c3_binder_refuses_c4_wrong_basin(tmp_path: Path) -> None:
    payload = _shipping_c4()
    payload["requested_pins"]["basin_id"] = "other"
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload, align_ops=True)
    _assert_bind_refuses(receipt=receipt, c4=c4, registry=registry, original=original, code="C3_C4_PIN")


def test_c3_binder_refuses_c4_nonzero_control(tmp_path: Path) -> None:
    payload = _shipping_c4()
    payload["no_control"] = {"slurm_request_count": 1, "non_get_control_count": 0}
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload, align_ops=True)
    _assert_bind_refuses(receipt=receipt, c4=c4, registry=registry, original=original, code="C3_C4_CONTROL")


@pytest.mark.parametrize("fault", ("invalid_job", "collapsed"))
def test_c3_binder_refuses_invalid_or_collapsed_c4_ops(tmp_path: Path, fault: str) -> None:
    payload = _shipping_c4()
    if fault == "invalid_job":
        payload["ops"]["gfs"]["job_id"] = ""
    else:
        payload["ops"]["ifs"]["job_id"] = payload["ops"]["gfs"]["job_id"]
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload)
    _assert_bind_refuses(receipt=receipt, c4=c4, registry=registry, original=original, code="C3_C4_OPS")


def test_c3_binder_refuses_c4_product_identity_mismatch(tmp_path: Path) -> None:
    payload = _shipping_c4()
    payload["gfs"]["run_id"] = "other-run"
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload, align_ops=True)
    _assert_bind_refuses(receipt=receipt, c4=c4, registry=registry, original=original, code="C3_C4_IDENTITY")


@pytest.mark.parametrize("fault", ("job_id", "logs_status"))
def test_c3_binder_refuses_c4_ops_mismatch_against_outer_sources(tmp_path: Path, fault: str) -> None:
    payload = _shipping_c4()
    receipt, c4, registry, original = _published_bind_inputs(
        tmp_path,
        payload,
        align_ops=True,
        mutate_document=lambda document: _mismatch_ops(document, fault),
    )
    _assert_bind_refuses(receipt=receipt, c4=c4, registry=registry, original=original, code="C3_BIND_C4_OPS")


def _mismatch_ops(document: dict[str, Any], fault: str) -> dict[str, Any]:
    if fault == "job_id":
        document["sources"]["GFS"]["c4_job_id"] = "other-job"
    else:
        document["sources"]["IFS"]["c4_logs_status"] = 201
    return document


def test_c3_binder_refuses_valid_c4_digest_tamper(tmp_path: Path) -> None:
    payload = _shipping_c4()
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload, align_ops=True)
    tampered = _shipping_c4()
    tampered["generated_at"] = "2026-09-06T12:00:01.000Z"
    _write_private_json(c4, tampered)
    _assert_bind_refuses(receipt=receipt, c4=c4, registry=registry, original=original, code="C3_BIND_C4_TAMPER")


def test_c3_binder_refuses_registry_tamper_after_valid_c4_bind(tmp_path: Path) -> None:
    payload = _shipping_c4()
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload, align_ops=True)
    bound = _bind(receipt, c4, registry)
    assert bound["registry_facts"]["sha256"] == json.loads(original.decode())["registry_sha256"]
    _write_private_json(
        registry,
        _registry_payload(
            generated_at="2026-09-06T12:00:00Z",
            models=[{"model_id": "model", "basin_id": "basin"}],
        ),
    )
    _assert_bind_refuses(
        receipt=receipt, c4=c4, registry=registry, original=original, code="C3_BIND_REGISTRY_TAMPER"
    )


def test_c3_binder_refuses_wrong_sha_without_rewriting_receipt(tmp_path: Path) -> None:
    payload = _shipping_c4()
    receipt, c4, registry, original = _published_bind_inputs(tmp_path, payload, align_ops=True)
    _assert_bind_refuses(
        receipt=receipt,
        c4=c4,
        registry=registry,
        original=original,
        code="C3_BIND_SHA",
        reviewed_sha=WRONG_SHA,
    )
