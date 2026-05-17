from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.production_closure import slurm_validation
from services.production_closure.object_store_validation import write_synthetic_basins_fixture


def _assert_summary_files_match_lane_json(summary: dict[str, object], lane_dir: Path) -> None:
    assert sorted(summary["files"]) == sorted(path.name for path in lane_dir.glob("*.json") if path.is_file())


def test_validate_object_store_synthetic_copied_root_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_root = tmp_path / "object-store"
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://user:pass@nhms-prod/m10?token=secret")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ENDPOINT", "https://user:pass@example.invalid:9000/path?token=secret")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_CREDENTIAL_SOURCE", "env:AWS_SECRET_ACCESS_KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret")

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148"]
    )

    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "m10_148" / "object-store"
    assert exit_code == 0
    assert summary["status"] == "ready"
    assert summary["evidence_dir"] == str(lane_dir)
    assert summary["object_store_prefix"] == "s3://nhms-prod/m10"
    assert "runtime_staging_manifest.json" in summary["files"]
    _assert_summary_files_match_lane_json(summary, lane_dir)

    migration = json.loads((lane_dir / "migration_report.json").read_text(encoding="utf-8"))
    assert migration["production_ready"] is True
    assert migration["source_is_symlink"] is False
    assert migration["file_count"] > 0
    assert migration["byte_count"] > 0
    assert migration["inventory_checksum"]

    package_evidence = json.loads((lane_dir / "package_manifest_evidence.json").read_text(encoding="utf-8"))
    assert package_evidence["manifest_included"] is True
    assert package_evidence["model_package_uri"].startswith("s3://nhms-prod/m10/models/")

    stored = json.loads((lane_dir / "stored_object_verification.json").read_text(encoding="utf-8"))
    assert stored["status"] == "verified"
    assert stored["package_checksum_confirmed_from_stored_manifest"] is True
    assert stored["entry_count"] > 0
    assert all(entry["verified"] is True for entry in stored["entries"])

    consumption = json.loads((lane_dir / "registry_api_runtime_consumption.json").read_text(encoding="utf-8"))
    assert consumption["status"] == "ready"
    assert consumption["uses_object_uri_prefix"] is True
    assert consumption["runtime_dev_path_leak"] is False
    assert consumption["forbidden_runtime_source_fragments"] == []
    assert consumption["registry"]["status"] == "local_sources_prepared"
    assert consumption["registry"]["db_import_status"] == "not_executed"
    assert consumption["registry"]["implicit_activation"] is False
    assert consumption["api"]["live_api_status"] == "not_executed"
    assert consumption["runtime"]["status"] == "prepared"
    assert consumption["runtime"]["execution_status"] == "not_executed"

    cleanup = json.loads((lane_dir / "cleanup_rollback.json").read_text(encoding="utf-8"))
    assert cleanup["status"] == "ready"
    assert cleanup["cleanup_status"] == "quarantined"
    assert cleanup["written_object_keys"]
    assert cleanup["written_db_rows"]
    assert cleanup["implicit_model_activation"] is False
    assert cleanup["active_model_state"] == "unchanged"
    assert cleanup["partial_objects_remaining"] == []

    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in lane_dir.iterdir() if path.is_file())
    assert "super-secret" not in evidence_text
    assert "token=secret" not in evidence_text
    assert "user:pass@" not in evidence_text
    assert "AWS_SECRET_ACCESS_KEY" not in evidence_text
    assert "https://example.invalid:9000/path" in evidence_text
    assert "s3://nhms-prod/m10" in evidence_text


def test_validate_object_store_symlink_root_blocks_before_package_or_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    write_synthetic_basins_fixture(real_root)
    linked_root = tmp_path / "linked-basins"
    os.symlink(real_root, linked_root)

    exit_code = slurm_validation.main(
        [
            "validate-object-store",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "m10_148_symlink",
            "--basins-root",
            str(linked_root),
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "m10_148_symlink" / "object-store"
    assert exit_code == 0
    assert summary["status"] == "blocked"
    assert summary["blockers"][0]["error_code"] == "BASINS_MIGRATION_SYMLINK_TARGET"
    assert (lane_dir / "preflight.json").exists()
    assert (lane_dir / "migration_blocker.json").exists()
    _assert_summary_files_match_lane_json(summary, lane_dir)
    assert not (lane_dir / "package_manifest.json").exists()
    assert not (lane_dir / "stored_object_verification.json").exists()
    assert not (lane_dir / "registry_api_runtime_consumption.json").exists()
    assert not (lane_dir / "cleanup_rollback.json").exists()
    assert not (tmp_path / "artifacts" / "m10_148_symlink" / "object-store" / "local-object-store").exists()


def test_validate_object_store_delete_cleanup_policy_removes_partial_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_root = tmp_path / "object-store"
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/m10")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_CLEANUP_POLICY", "delete")

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_delete"]
    )

    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "m10_148_delete" / "object-store"
    cleanup = json.loads((lane_dir / "cleanup_rollback.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert summary["status"] == "ready"
    assert cleanup["cleanup_policy"] == "delete"
    assert cleanup["cleanup_status"] == "deleted"
    assert cleanup["partial_objects_remaining"] == []
    assert cleanup["implicit_model_activation"] is False
    assert not any(object_root.glob("models/*/vproduction-object-store-local-failed-import/partial-package.bin"))


def test_validate_object_store_runtime_evidence_has_no_development_source_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/runtime-prefix")

    slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_runtime"]
    )
    capsys.readouterr()

    lane_dir = tmp_path / "artifacts" / "m10_148_runtime" / "object-store"
    consumption = json.loads((lane_dir / "registry_api_runtime_consumption.json").read_text(encoding="utf-8"))
    values = [
        consumption["runtime"]["runtime_manifest"]["model_package_uri"],
        consumption["runtime"]["runtime_manifest"]["forcing_uri"],
        consumption["api"]["model_response_fixture"]["model_package_uri"],
        consumption["registry"]["model_package_uri"],
    ]
    assert all(value.startswith("s3://nhms-prod/runtime-prefix/") for value in values)
    assert all("data/Basins" not in value and "/volume/" not in value for value in values)
    assert consumption["runtime_dev_path_leak"] is False


def test_validate_object_store_invalid_target_fails_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_TARGET", "ftp")

    try:
        exit_code = slurm_validation.main(
            ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "badtarget"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code == 1
    assert "PRODUCTION_OBJECT_STORE_TARGET_INVALID" in capsys.readouterr().err
    assert not (tmp_path / "artifacts" / "badtarget").exists()
