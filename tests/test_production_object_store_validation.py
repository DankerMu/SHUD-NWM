from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from packages.common.object_store import LocalObjectStore
from services.production_closure import object_store_validation, slurm_validation
from services.production_closure.object_store_validation import (
    ProductionObjectStoreConfig,
    _verify_stored_objects,
    validate_object_store,
    write_synthetic_basins_fixture,
)


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
    assert (
        stored["package_checksum_source_model_identity_basis"]
        == "documented_148_copied_root_non_symlink_source_suffix"
    )
    assert stored["package_checksum_reconstruction_limitation"] is None
    assert stored["recomputed_package_checksum"] == stored["package_checksum"]
    assert stored["entry_count"] > 0
    assert all(entry["verified"] is True for entry in stored["entries"])
    manifest_entry = next(entry for entry in stored["entries"] if entry["role"] == "manifest")
    assert manifest_entry["actual_sha256"] == manifest_entry["manifest_recorded_sha256"]
    assert manifest_entry["final_manifest_sha256"] != manifest_entry["manifest_recorded_sha256"]

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


def test_validate_object_store_registry_uses_raw_manifest_when_evidence_redacts_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_prepare = object_store_validation.prepare_basins_import_sources
    observed_manifest_paths: list[Path] = []
    observed_raw_prefixes: list[str] = []

    def recording_prepare(*, inventory_path: Path, package_manifest_path: Path) -> object:
        observed_manifest_paths.append(package_manifest_path)
        raw_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
        observed_raw_prefixes.append(str(raw_manifest["model_package_uri"]))
        return original_prepare(inventory_path=inventory_path, package_manifest_path=package_manifest_path)

    monkeypatch.setattr(object_store_validation, "prepare_basins_import_sources", recording_prepare)
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv(
        "NHMS_PRODUCTION_OBJECT_STORE_PREFIX",
        "s3://AWS_SECRET_ACCESS_KEY:super-secret@nhms-prod/token=secret/m10?X-Amz-Signature=secret",
    )

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_raw_manifest"]
    )

    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "m10_148_raw_manifest" / "object-store"
    manifest_evidence = json.loads((lane_dir / "package_manifest.json").read_text(encoding="utf-8"))
    consumption = json.loads((lane_dir / "registry_api_runtime_consumption.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert summary["status"] == "ready"
    assert consumption["registry"]["status"] == "local_sources_prepared"
    assert consumption["status"] == "ready"
    assert observed_manifest_paths == [lane_dir / ".package_manifest.raw.json"]
    assert observed_raw_prefixes[0].startswith("s3://nhms-prod/token=secret/m10/models/")
    assert manifest_evidence["model_package_uri"] == "s3://nhms-prod/token=[redacted]"
    assert manifest_evidence["manifest_uri"] == "s3://nhms-prod/token=[redacted]"
    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in lane_dir.glob("*.json"))
    assert "AWS_SECRET_ACCESS_KEY" not in evidence_text
    assert "super-secret" not in evidence_text
    assert "X-Amz-Signature" not in evidence_text
    assert "token=secret" not in evidence_text


def test_verify_stored_objects_blocks_tampered_manifest_self_entry_checksum(
    tmp_path: Path,
) -> None:
    summary = validate_object_store(
        ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m10_148_manifest_self")
    )
    manifest_uri = str(summary["manifest_uri"])
    manifest_path = tmp_path / "artifacts" / "m10_148_manifest_self" / "object-store" / "package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    store = LocalObjectStore(
        tmp_path / "artifacts" / "m10_148_manifest_self" / "object-store" / "local-object-store",
        "s3://nhms-production-like/m10_148_manifest_self",
    )
    stored_manifest = json.loads(store.read_bytes(manifest_uri).decode("utf-8"))
    manifest_entry = next(entry for entry in stored_manifest["included_files"] if entry["role"] == "manifest")
    manifest_entry["sha256"] = hashlib.sha256(store.read_bytes(manifest_uri)).hexdigest()
    tampered_manifest_bytes = json.dumps(stored_manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    store.write_bytes_atomic(manifest_uri, tampered_manifest_bytes)

    verification = _verify_stored_objects(store, manifest)

    blocked_entry = next(entry for entry in verification["entries"] if entry["role"] == "manifest")
    assert verification["status"] == "blocked"
    assert blocked_entry["verified"] is False
    assert blocked_entry["actual_sha256"] != blocked_entry["manifest_recorded_sha256"]


def test_verify_stored_objects_blocks_limited_package_checksum_when_identity_not_provable(
    tmp_path: Path,
) -> None:
    summary = validate_object_store(
        ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m10_148_limited_checksum")
    )
    manifest_uri = str(summary["manifest_uri"])
    manifest_path = tmp_path / "artifacts" / "m10_148_limited_checksum" / "object-store" / "package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    store = LocalObjectStore(
        tmp_path / "artifacts" / "m10_148_limited_checksum" / "object-store" / "local-object-store",
        "s3://nhms-production-like/m10_148_limited_checksum",
    )
    stored_manifest = json.loads(store.read_bytes(manifest_uri).decode("utf-8"))
    stored_manifest.pop("source_path")
    stored_manifest.pop("resolved_source_path")
    manifest_entry = next(entry for entry in stored_manifest["included_files"] if entry["role"] == "manifest")
    manifest_payload = object_store_validation._stored_manifest_payload_without_self_entry(stored_manifest)
    manifest_entry["sha256"] = hashlib.sha256(
        object_store_validation._deterministic_manifest_bytes(manifest_payload)
    ).hexdigest()
    for _ in range(5):
        manifest_bytes = object_store_validation._deterministic_manifest_bytes(stored_manifest)
        if manifest_entry["size_bytes"] == len(manifest_bytes):
            break
        manifest_entry["size_bytes"] = len(manifest_bytes)
    store.write_bytes_atomic(manifest_uri, object_store_validation._deterministic_manifest_bytes(stored_manifest))

    verification = _verify_stored_objects(store, manifest)

    manifest_verification = next(entry for entry in verification["entries"] if entry["role"] == "manifest")
    assert verification["status"] == "blocked"
    assert verification["package_checksum_matches_manifest"] is True
    assert verification["package_checksum_confirmed_from_stored_manifest"] is False
    assert verification["package_checksum_reconstruction_status"] == "limited"
    assert verification["package_checksum_source_model_identity_basis"] == "unavailable"
    assert (
        verification["package_checksum_reconstruction_limitation"]
        == "stored_manifest_does_not_prove_root_relative_resolved_path"
    )
    assert verification["recomputed_package_checksum"] is None
    assert manifest_verification["verified"] is True
    assert object_store_validation._result_blockers(verification) == [
        {
            "error_code": "PRODUCTION_OBJECT_STORE_VALIDATION_BLOCKED",
            "schema": "nhms.production_closure.object_store.stored_object_verification.v1",
            "status": "blocked",
        }
    ]


def test_validate_object_store_uses_documented_env_names_without_generic_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    basins_root = tmp_path / "copied-basins"
    inventory = write_synthetic_basins_fixture(basins_root)
    model_id = next(model["model_id"] for model in inventory["models"] if model["status"] == "valid")
    monkeypatch.delenv("OBJECT_STORE_ROOT", raising=False)
    monkeypatch.delenv("OBJECT_STORE_PREFIX", raising=False)
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_TARGET", "local-production-like")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "documented-object-store"))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/documented")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_CREDENTIAL_SOURCE", "workload-identity")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_CLEANUP_POLICY", "retain")
    monkeypatch.setenv("NHMS_PRODUCTION_BASINS_ROOT", str(basins_root))
    monkeypatch.setenv("NHMS_PRODUCTION_BASINS_MODEL_ID", model_id)
    monkeypatch.setenv("NHMS_PRODUCTION_BASINS_VERSION", "vdocumented-env")

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_docenv"]
    )

    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "m10_148_docenv" / "object-store"
    preflight = json.loads((lane_dir / "preflight.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert summary["status"] == "ready"
    assert summary["model_id"] == model_id
    assert summary["version"] == "vdocumented-env"
    assert summary["object_store_prefix"] == "s3://nhms-prod/documented"
    assert preflight["target"] == "local-production-like"
    assert preflight["object_store_root"] == str(tmp_path / "documented-object-store")
    assert preflight["object_store_prefix"] == "s3://nhms-prod/documented"
    assert preflight["credential_source"] == "[redacted]"
    assert preflight["cleanup_policy"] == "retain"
    assert preflight["copied_basins_root"] == str(basins_root)
    assert preflight["selected_model"] == model_id
    assert preflight["version"] == "vdocumented-env"


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
