"""Dependency-evidence hardening coverage for production-ops validation.

Owns the path/size/symlink/fd-safety proofs for dependency summary and
receipt ingestion in ``services.production_closure.ops_validation``:
TOCTOU swap guards, bound-parent-fd opens, UTF-8/depth/width limits, and
recursion-error containment. Shared helpers live in
``tests/test_production_ops_validation.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from services.production_closure import ops_validation as ops_validation_module
from services.production_closure.ops_validation import (
    MAX_EVIDENCE_PAYLOAD_BYTES,
    ProductionOpsConfig,
    validate_ops,
)
from tests.test_production_ops_validation import (
    _read_json,
    _write_dependency_acceptance_receipt,
    _write_dependency_summary,
)


def test_validate_ops_hardens_dependency_summary_paths_and_sizes(tmp_path: Path) -> None:
    valid_root = tmp_path / "valid"
    valid_root.mkdir()
    _write_dependency_summary(
        valid_root / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
    )
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(valid_root, target_is_directory=True)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_symlink_root",
            slurm_evidence_root=symlink_root,
        )
    )
    dependency = _read_json(tmp_path / "artifacts" / "dep_symlink_root" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"

    file_link_root = tmp_path / "file-link-root"
    file_link_root.mkdir()
    outside = tmp_path / "outside-summary.json"
    _write_dependency_summary(outside, "met", 149, "nhms.production_closure.met.v1", "ready")
    (file_link_root / "summary.json").symlink_to(outside)
    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_symlink_file",
            met_evidence_root=file_link_root,
        )
    )
    met = next(
        item
        for item in _read_json(tmp_path / "artifacts" / "dep_symlink_file" / "ops" / "dependency_closure.json")[
            "dependencies"
        ]
        if item["dependency"] == "met"
    )
    assert met["status"] == "blocked"
    assert met["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"

    oversized_root = tmp_path / "oversized-root"
    oversized_root.mkdir()
    (oversized_root / "summary.json").write_text(
        json.dumps(
            {
                "schema": "nhms.production_closure.scale.v1",
                "issue": 151,
                "run_id": "scale-run",
                "status": "ready",
                "payload": "x" * MAX_EVIDENCE_PAYLOAD_BYTES,
            }
        ),
        encoding="utf-8",
    )
    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_oversized",
            scale_evidence_root=oversized_root,
        )
    )
    scale = next(
        item
        for item in _read_json(tmp_path / "artifacts" / "dep_oversized" / "ops" / "dependency_closure.json")[
            "dependencies"
        ]
        if item["dependency"] == "scale"
    )
    assert scale["status"] == "blocked"
    assert scale["error_code"] == "PRODUCTION_OPS_DEPENDENCY_SUMMARY_TOO_LARGE"


def test_validate_ops_blocks_dependency_summary_swap_to_symlink_before_fd_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    symlink_target = tmp_path / "symlink-target-summary.json"
    _write_dependency_summary(
        symlink_target,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    target_bytes = symlink_target.read_bytes()
    swapped = False
    original_open = os.open

    def swap_summary_before_open(path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if dir_fd is not None and path == summary_path.name and not swapped:
            swapped = True
            summary_path.unlink()
            summary_path.symlink_to(symlink_target)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ops_validation_module.os, "open", swap_summary_before_open)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="summary_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "summary_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"
    assert "accepted_dependency_evidence" not in slurm
    assert hashlib.sha256(target_bytes).hexdigest() not in json.dumps(slurm)


def test_validate_ops_blocks_dependency_summary_swap_to_same_root_symlink_before_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    sibling = root / "sibling-summary.json"
    _write_dependency_summary(
        sibling,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    original_stat = ops_validation_module.os.stat
    swapped = False

    def swap_summary_before_no_follow_stat(path, *args, **kwargs):
        nonlocal swapped
        if Path(path) == summary_path and kwargs.get("follow_symlinks") is False and not swapped:
            swapped = True
            summary_path.unlink()
            summary_path.symlink_to(sibling)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(ops_validation_module.os, "stat", swap_summary_before_no_follow_stat)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="summary_same_root_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "summary_same_root_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"
    assert "accepted_dependency_evidence" not in slurm
    assert "sibling-summary" not in json.dumps(slurm)


def test_validate_ops_opens_dependency_summary_and_receipt_by_bound_parent_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    receipt_path = root / "accepted_dependency_evidence.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    full_path_open_attempts: list[Path] = []
    basename_opens: list[str] = []
    original_open = os.open

    def guarded_open(path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if dir_fd is None and Path(path) in {summary_path.resolve(), receipt_path.resolve()}:
            full_path_open_attempts.append(Path(path))
            raise AssertionError("dependency evidence file must be opened relative to the bound parent fd")
        if dir_fd is not None and path in {summary_path.name, receipt_path.name}:
            basename_opens.append(str(path))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ops_validation_module.os, "open", guarded_open)

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="bound_parent_fd",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "bound_parent_fd" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "accepted"
    assert full_path_open_attempts == []
    assert basename_opens == ["summary.json", "accepted_dependency_evidence.json"]


def test_validate_ops_blocks_dependency_root_swap_to_symlink_before_summary_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    outside = tmp_path / "outside-slurm"
    outside.mkdir()
    _write_dependency_summary(
        outside / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    swapped = False
    original_verify = ops_validation_module._verify_bound_directory_identity

    def swap_root_before_verify(path, expected, path_unsafe_code):
        nonlocal swapped
        if path == root.resolve() and not swapped:
            swapped = True
            summary_path.unlink()
            root.rmdir()
            root.symlink_to(outside, target_is_directory=True)
        return original_verify(path, expected, path_unsafe_code)

    monkeypatch.setattr(ops_validation_module, "_verify_bound_directory_identity", swap_root_before_verify)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="root_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "root_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE"
    assert "accepted_dependency_evidence" not in slurm


def test_validate_ops_blocks_dependency_parent_swap_to_symlink_before_summary_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "deps"
    summary_parent = root / "slurm"
    summary_parent.mkdir(parents=True)
    summary_path = summary_parent / "summary.json"
    _write_dependency_summary(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    _write_dependency_summary(
        outside / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    swapped = False
    original_verify = ops_validation_module._verify_bound_directory_identity

    def swap_parent_before_verify(path, expected, path_unsafe_code):
        nonlocal swapped
        if path == summary_parent.resolve() and not swapped:
            swapped = True
            summary_path.unlink()
            summary_parent.rmdir()
            summary_parent.symlink_to(outside, target_is_directory=True)
        return original_verify(path, expected, path_unsafe_code)

    monkeypatch.setattr(ops_validation_module, "_verify_bound_directory_identity", swap_parent_before_verify)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="parent_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "parent_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE"
    assert "accepted_dependency_evidence" not in slurm


def test_validate_ops_blocks_dependency_receipt_swap_to_symlink_before_fd_open(
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
        accepted=True,
    )
    receipt_path = root / "accepted_dependency_evidence.json"
    symlink_target = tmp_path / "symlink-target-accepted_dependency_evidence.json"
    symlink_target.write_bytes(receipt_path.read_bytes())
    target_bytes = symlink_target.read_bytes()
    swapped = False
    original_open = os.open

    def swap_receipt_before_open(path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if dir_fd is not None and path == receipt_path.name and not swapped:
            swapped = True
            receipt_path.unlink()
            receipt_path.symlink_to(symlink_target)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ops_validation_module.os, "open", swap_receipt_before_open)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="receipt_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "receipt_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"
    assert "accepted_dependency_evidence" not in slurm
    assert hashlib.sha256(target_bytes).hexdigest() not in json.dumps(slurm)


def test_validate_ops_blocks_dependency_receipt_swap_to_same_root_symlink_before_bind(
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
        accepted=True,
    )
    receipt_path = root / "accepted_dependency_evidence.json"
    sibling_receipt = root / "sibling-accepted_dependency_evidence.json"
    sibling_receipt.write_bytes(receipt_path.read_bytes())
    original_stat = ops_validation_module.os.stat
    swapped = False

    def swap_receipt_before_no_follow_stat(path, *args, **kwargs):
        nonlocal swapped
        if Path(path) == receipt_path and kwargs.get("follow_symlinks") is False and not swapped:
            swapped = True
            receipt_path.unlink()
            receipt_path.symlink_to(sibling_receipt)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(ops_validation_module.os, "stat", swap_receipt_before_no_follow_stat)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="receipt_same_root_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "receipt_same_root_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"
    assert "accepted_dependency_evidence" not in slurm
    assert "sibling-accepted" not in json.dumps(slurm)


def test_validate_ops_blocks_dependency_root_swap_to_symlink_before_receipt_open(
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
        accepted=True,
    )
    outside = tmp_path / "outside-slurm"
    outside.mkdir()
    _write_dependency_summary(outside / "summary.json", "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    _write_dependency_acceptance_receipt(outside / "summary.json", "slurm", 147, "nhms.production_closure.slurm.v1")
    swapped = False
    verify_count = 0
    original_verify = ops_validation_module._verify_bound_directory_identity

    def swap_root_before_receipt_verify(path, expected, path_unsafe_code):
        nonlocal swapped, verify_count
        if path == root.resolve():
            verify_count += 1
            if verify_count > 1 and not swapped:
                swapped = True
                for child in root.iterdir():
                    child.unlink()
                root.rmdir()
                root.symlink_to(outside, target_is_directory=True)
        return original_verify(path, expected, path_unsafe_code)

    monkeypatch.setattr(ops_validation_module, "_verify_bound_directory_identity", swap_root_before_receipt_verify)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="receipt_root_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "receipt_root_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE"
    assert "accepted_dependency_evidence" not in slurm


def test_validate_ops_blocks_dependency_root_swap_to_symlink_after_summary_read_before_receipt_lookup(
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
        accepted=False,
    )
    outside = tmp_path / "outside-slurm"
    outside.mkdir()
    outside_summary_path = outside / "summary.json"
    _write_dependency_summary(
        outside_summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    marker_receipt_digest = hashlib.sha256((outside / "accepted_dependency_evidence.json").read_bytes()).hexdigest()
    swapped = False
    original_read_summary = ops_validation_module._read_dependency_summary_json

    def swap_root_after_summary_read(summary_evidence):
        nonlocal swapped
        summary, digest = original_read_summary(summary_evidence)
        if not swapped:
            swapped = True
            summary_path.unlink()
            root.rmdir()
            root.symlink_to(outside, target_is_directory=True)
        return summary, digest

    monkeypatch.setattr(ops_validation_module, "_read_dependency_summary_json", swap_root_after_summary_read)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="root_swap_after_summary",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "root_swap_after_summary" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE"
    assert "accepted_dependency_evidence" not in slurm
    assert marker_receipt_digest not in json.dumps(slurm)


def test_validate_ops_blocks_invalid_utf8_dependency_summary_and_writes_lane(tmp_path: Path) -> None:
    invalid_root = tmp_path / "invalid-summary"
    invalid_root.mkdir()
    (invalid_root / "summary.json").write_bytes(b'{"schema": "\xff"}')

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="invalid_utf8_summary",
            slurm_evidence_root=invalid_root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "invalid_utf8_summary" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_SUMMARY_INVALID"


def test_validate_ops_blocks_too_deep_dependency_summary_and_writes_lane(tmp_path: Path) -> None:
    root = tmp_path / "deep-summary"
    root.mkdir()
    summary_path = root / "summary.json"
    nested: object = "leaf"
    for _ in range(150):
        nested = [nested]
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
        extra={"bounded_nested_payload": nested},
    )
    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="too_deep_summary",
            slurm_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "too_deep_summary" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_SUMMARY_INVALID"
    assert "nesting limit" in slurm["reason"]


def test_validate_ops_blocks_invalid_utf8_dependency_receipt_and_writes_lane(tmp_path: Path) -> None:
    invalid_root = tmp_path / "invalid-receipt"
    invalid_root.mkdir()
    summary_path = invalid_root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    (invalid_root / "accepted_dependency_evidence.json").write_bytes(b'{"schema": "\xff"}')

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="invalid_utf8_receipt",
            slurm_evidence_root=invalid_root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "invalid_utf8_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"


def test_validate_ops_blocks_malformed_dependency_receipt_and_writes_lane(tmp_path: Path) -> None:
    invalid_root = tmp_path / "malformed-receipt"
    invalid_root.mkdir()
    summary_path = invalid_root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    (invalid_root / "accepted_dependency_evidence.json").write_text('{"schema": ', encoding="utf-8")

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="malformed_receipt",
            slurm_evidence_root=invalid_root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "malformed_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"


def test_validate_ops_blocks_too_deep_dependency_receipt_and_writes_lane(tmp_path: Path) -> None:
    root = tmp_path / "deep-receipt"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    receipt_path = root / "accepted_dependency_evidence.json"
    receipt = _read_json(receipt_path)
    nested: object = "leaf"
    for _ in range(150):
        nested = [nested]
    receipt["bounded_nested_payload"] = nested
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="too_deep_receipt",
            slurm_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "too_deep_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"
    assert "nesting limit" in slurm["reason"]


def test_validate_ops_blocks_too_wide_dependency_receipt_and_writes_lane(tmp_path: Path) -> None:
    root = tmp_path / "wide-receipt"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    receipt_path = root / "accepted_dependency_evidence.json"
    receipt = _read_json(receipt_path)
    receipt["wide_nodes"] = [0] * 10_050
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="too_wide_receipt",
            slurm_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "too_wide_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"
    assert "complexity limit" in slurm["reason"]


def test_validate_ops_blocks_dependency_receipt_recursion_error_and_writes_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "recursive-receipt"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )

    def raise_recursion_error(receipt_path: object) -> object:
        del receipt_path
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ops_validation_module, "_read_dependency_receipt_json", raise_recursion_error)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="recursive_receipt",
            slurm_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "recursive_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"


def test_validate_ops_resolves_dependency_summary_shapes_in_rollback_references(tmp_path: Path) -> None:
    run_root = tmp_path / "dependencies"
    slurm_root = run_root / "slurm"
    object_store_root = run_root / "object-store"
    slurm_root.mkdir(parents=True)
    object_store_root.mkdir()
    _write_dependency_summary(
        slurm_root / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
    )
    _write_dependency_summary(
        object_store_root / "summary.json",
        "object_store",
        148,
        "nhms.production_closure.object_store.v1",
        "ready",
    )

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_shapes",
            slurm_evidence_root=run_root,
            object_store_evidence_root=run_root,
        )
    )

    rollback = _read_json(tmp_path / "artifacts" / "dep_shapes" / "ops" / "rollback_drills.json")
    first_drill_refs = rollback["drills"][0]["dependency_artifact_references"]
    assert {"dependency": "slurm", "drill": "bad_model_activation", "summary": "[redacted]"} in (
        first_drill_refs
    )
    assert {
        "dependency": "object_store",
        "drill": "bad_model_activation",
        "summary": "[redacted]",
    } in first_drill_refs
    assert str(run_root) not in json.dumps(first_drill_refs)


def test_validate_ops_live_drill_scope_remains_release_blocked_without_receipts(tmp_path: Path) -> None:
    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="live_scope",
            rollback_scope="live_drill",
        )
    )

    rollback = _read_json(tmp_path / "artifacts" / "live_scope" / "ops" / "rollback_drills.json")
    assert summary["status"] == "release_blocked"
    assert summary["live_rollback_executed"] is False
    assert rollback["status"] == "release_blocked"
    assert rollback["requested_scope"] == "live_drill"
    assert rollback["live_rollback_executed"] is False
    assert {drill["execution_mode"] for drill in rollback["drills"]} == {"simulated_drill"}
    assert {drill["live_rollback_executed"] for drill in rollback["drills"]} == {False}


def test_validate_ops_live_ready_config_knobs_do_not_claim_live_execution(tmp_path: Path) -> None:
    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="live_knobs",
            auth_mode="backend_route_executed",
            alert_target="https://alerts.example/ops",
        )
    )

    lane_dir = tmp_path / "artifacts" / "live_knobs" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    alerts = _read_json(lane_dir / "monitoring_alerts.json")
    assert summary["live_backend_auth_executed"] is False
    assert summary["live_alert_sink_delivered"] is False
    assert auth["model_activation_boundary"]["backend_enforcement_available"] is True
    assert auth["model_activation_boundary"]["requested_auth_mode"] == "backend_route_executed"
    assert alerts["status"] == "release_blocked"
    assert {alert["execution_mode"] for alert in alerts["alerts"]} == {"not_executed"}
    assert {alert["sink"] for alert in alerts["alerts"]} == {"https://alerts.example/[redacted-alert-path]"}
