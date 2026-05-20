from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from packages.common import safe_fs
from packages.common.object_store import LocalObjectStore, ObjectStoreError
from services.production_closure import (
    e2e_validation,
    met_validation,
    object_store_validation,
    ops_validation,
    scale_validation,
    slurm_validation,
)
from services.production_closure.object_store_validation import (
    EvidenceWriter,
    ProductionObjectStoreConfig,
    ProductionObjectStoreValidationError,
    _argparse_main,
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
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/m10")
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
    assert summary["execution_mode"] == "deterministic_fixture"
    assert summary["deterministic_fixture"] is True
    assert summary["live_registry_import"] is False
    assert summary["live_api"] is False
    assert summary["live_api_status"] == "not_executed"
    assert summary["api_contract_source"] == "local_import_source"
    assert summary["final_production_readiness_claimed"] is False
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
    assert consumption["registry"]["status"] == "local_contract_prepared"
    assert consumption["registry"]["db_import_status"] == "not_executed"
    assert consumption["registry"]["live_registry_import"] is False
    assert consumption["registry"]["acceptance_evidence"] == "local_contract_smoke"
    assert consumption["registry"]["implicit_activation"] is False
    assert consumption["api"]["live_api_status"] == "not_executed"
    assert consumption["api"]["live_api"] is False
    assert consumption["api"]["acceptance_evidence"] == "local_contract_smoke"
    assert consumption["api"]["api_contract_source"] == "local_import_source"
    assert consumption["api_contract_source"] == "local_import_source"
    assert consumption["acceptance_evidence"] == "local_contract_smoke"
    assert consumption["live_registry_import"] is False
    assert consumption["live_api"] is False
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


def test_validate_object_store_rejects_unsafe_prefix_before_writing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv(
        "NHMS_PRODUCTION_OBJECT_STORE_PREFIX",
        "s3://AWS_SECRET_ACCESS_KEY:super-secret@nhms-prod/token=secret/m10?X-Amz-Signature=secret",
    )

    try:
        exit_code = slurm_validation.main(
            [
                "validate-object-store",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                "m10_148_raw_manifest",
            ]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    captured = capsys.readouterr()
    lane_dir = tmp_path / "artifacts" / "m10_148_raw_manifest" / "object-store"
    assert exit_code == 1
    assert captured.out == ""
    assert "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE" in captured.err
    assert "Traceback" not in captured.err
    assert not lane_dir.exists()
    assert not (tmp_path / "object-store").exists()


@pytest.mark.parametrize(
    "prefix",
    [
        "s3://user:pass@nhms-prod/m10",
        "s3://nhms-prod/m10?token=secret",
        "s3://nhms-prod/m10#credential=secret",
        "s3://nhms-prod/token=secret/m10",
        "s3://nhms-prod/signature=abc/m10",
        "s3://nhms-prod/access_key=abc/m10",
        "s3://nhms-prod/secret=abc/m10",
        "s3://nhms-prod/password=abc/m10",
        "s3://nhms-prod/credential=abc/m10",
        "s3://nhms-prod/x-amz-signature=abc/m10",
        "s3://nhms-prod/path%2Ftoken=secret/m10",
        "s3://nhms-prod/path%2Fx-amz-signature=abc/m10",
        "s3://nhms-prod/path%3Ftoken=secret/m10",
        "s3://nhms-prod/path%23x-amz-signature=abc/m10",
        "s3://nhms-prod/path%252Ftoken=secret/m10",
        "s3://nhms-prod/path%252Fx-amz-signature=abc/m10",
        "s3://bucket/%2E%2E/prod",
        "s3://bucket/prod%2Fsecret",
        "s3://bucket/prod%5Csecret",
        "s3://bucket/prod\\secret",
        "s3://bucket/%252Ftoken=secret/prod",
        "s3://%2E%2E/prod",
        "s3://bucket.%2E/prod",
        "s3://bucket/path%3Fcredential=abc",
        "s3://bucket/path%23credential=abc",
        "s3://bucket/path%3Bcredential=abc",
        "s3://bucket/path%26credential=abc",
    ],
)
def test_validate_object_store_rejects_sensitive_prefix_shapes_without_lane_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    prefix: str,
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", prefix)

    try:
        exit_code = slurm_validation.main(
            ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_badprefix"]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE" in captured.err
    assert not (tmp_path / "artifacts" / "m10_148_badprefix").exists()
    assert not (tmp_path / "object-store").exists()


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


def test_validate_object_store_cleanup_does_not_touch_existing_model_scoped_failed_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_root = tmp_path / "object-store"
    basins_root = tmp_path / "copied-basins"
    inventory = write_synthetic_basins_fixture(basins_root)
    model_id = next(model["model_id"] for model in inventory["models"] if model["status"] == "valid")
    version = "vexisting-failed-import"
    existing_key = f"models/{model_id}/{version}-failed-import/partial-package.bin"
    existing_content = b"pre-existing model-scoped failed import\n"
    store = LocalObjectStore(object_root, "s3://nhms-prod/m10")
    store.write_bytes_atomic(existing_key, existing_content)
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/m10")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_CLEANUP_POLICY", "delete")
    monkeypatch.setenv("NHMS_PRODUCTION_BASINS_ROOT", str(basins_root))
    monkeypatch.setenv("NHMS_PRODUCTION_BASINS_MODEL_ID", model_id)
    monkeypatch.setenv("NHMS_PRODUCTION_BASINS_VERSION", version)

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_existing"]
    )

    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "m10_148_existing" / "object-store"
    cleanup = json.loads((lane_dir / "cleanup_rollback.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert summary["status"] == "ready"
    assert store.read_bytes(existing_key) == existing_content
    assert cleanup["cleanup_status"] == "deleted"
    assert cleanup["written_object_keys"] == [
        f"runs/m10_148_existing/input/scratch/cleanup-rollback/{model_id}/{version}-failed-import/partial-package.bin"
    ]
    assert cleanup["partial_objects_remaining"] == []


def test_validate_object_store_refuses_existing_cleanup_scratch_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_root = tmp_path / "object-store"
    basins_root = tmp_path / "copied-basins"
    inventory = write_synthetic_basins_fixture(basins_root)
    model_id = next(model["model_id"] for model in inventory["models"] if model["status"] == "valid")
    version = "vexisting-scratch"
    run_id = "m10_148_scratch_exists"
    existing_key = (
        f"runs/{run_id}/input/scratch/cleanup-rollback/"
        f"{model_id}/{version}-failed-import/partial-package.bin"
    )
    store = LocalObjectStore(object_root, "s3://nhms-prod/m10")
    store.write_bytes_atomic(existing_key, b"existing scratch content\n")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/m10")
    monkeypatch.setenv("NHMS_PRODUCTION_BASINS_ROOT", str(basins_root))
    monkeypatch.setenv("NHMS_PRODUCTION_BASINS_MODEL_ID", model_id)
    monkeypatch.setenv("NHMS_PRODUCTION_BASINS_VERSION", version)

    try:
        exit_code = slurm_validation.main(
            ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", run_id]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "PRODUCTION_OBJECT_STORE_VALIDATION_OBJECT_EXISTS" in captured.err
    assert store.read_bytes(existing_key) == b"existing scratch content\n"


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
        consumption["runtime"]["runtime_manifest"]["manifest_uri"],
        consumption["runtime"]["runtime_manifest"]["forcing_uri"],
        consumption["api"]["model_response_fixture"]["model_package_uri"],
        consumption["api"]["model_response_fixture"]["manifest_uri"],
        consumption["registry"]["model_package_uri"],
    ]
    assert all(value.startswith("s3://nhms-prod/runtime-prefix/") for value in values)
    assert all("data/Basins" not in value and "/volume/" not in value for value in values)
    assert consumption["runtime_dev_path_leak"] is False
    assert consumption["runtime"]["scratch_prefix"] == "runs/m10_148_runtime/input/scratch/runtime-staging"
    assert consumption["runtime"]["validation_object_keys"] == [
        "runs/m10_148_runtime/input/scratch/runtime-staging/forcing/gfs/2026051600/basin_v1/"
        f"{consumption['api']['model_response_fixture']['model_id']}/forcing.tsd.forc"
    ]


@pytest.mark.parametrize(
    ("case", "keys", "budget"),
    [
        (
            "file_count",
            {"forcing/one.txt": b"one\n", "forcing/two.txt": b"two\n"},
            {"max_file_count": 1, "max_directory_depth": 8, "max_total_bytes": 1024},
        ),
        (
            "depth",
            {"forcing/a/b/c.txt": b"deep\n"},
            {"max_file_count": 4, "max_directory_depth": 2, "max_total_bytes": 1024},
        ),
        (
            "total_bytes",
            {"forcing/one.txt": b"one\n", "forcing/two.txt": b"two\n"},
            {"max_file_count": 4, "max_directory_depth": 8, "max_total_bytes": 5},
        ),
        (
            "node_count",
            {"forcing/empty-dir/one.txt": b"one\n", "forcing/two.txt": b"two\n"},
            {"max_file_count": 4, "max_directory_depth": 8, "max_total_bytes": 1024, "max_node_count": 2},
        ),
    ],
)
def test_runtime_forcing_prefix_budgets_block_before_staging_excessive_files(
    tmp_path: Path,
    case: str,
    keys: dict[str, bytes],
    budget: dict[str, int],
) -> None:
    config = ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id=f"m10_148_{case}")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True)
    writer.prepare()
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/runtime-prefix")
    for key, content in keys.items():
        store.write_bytes_atomic(f"runs/{config.run_id}/input/scratch/runtime-staging/{key}", content)
    input_dir = config.lane_dir / "runtime-workspace" / "runs" / f"{config.run_id}_runtime_staging" / "input"
    input_dir.mkdir(parents=True)
    staging_budget = object_store_validation.RuntimeStagingBudget(
        max_file_count=budget["max_file_count"],
        max_node_count=budget.get("max_node_count", budget["max_file_count"]),
        max_directory_depth=budget["max_directory_depth"],
        max_total_bytes=budget["max_total_bytes"],
        max_object_bytes=object_store_validation.MAX_RUNTIME_STAGING_OBJECT_BYTES,
    )

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        object_store_validation._collect_runtime_object_or_prefix(
            config,
            store,
            f"runs/{config.run_id}/input/scratch/runtime-staging/forcing/",
            input_dir,
            staging_budget,
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert list(input_dir.rglob("*")) == []


@pytest.mark.parametrize(
    ("case", "keys", "message"),
    [
        ("zero_byte_file", {"forcing/empty.txt": b""}, "must not be empty"),
        ("empty_prefix", {}, "at least one non-empty regular file"),
    ],
)
def test_runtime_forcing_prefix_rejects_zero_byte_or_empty_tree(
    tmp_path: Path,
    case: str,
    keys: dict[str, bytes],
    message: str,
) -> None:
    config = ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id=f"m10_148_{case}")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True)
    writer.prepare()
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/runtime-prefix")
    prefix = Path(store.root) / "runs" / config.run_id / "input" / "scratch" / "runtime-staging" / "forcing"
    prefix.mkdir(parents=True)
    for key, content in keys.items():
        store.write_bytes_atomic(f"runs/{config.run_id}/input/scratch/runtime-staging/{key}", content)
    input_dir = config.lane_dir / "runtime-workspace" / "runs" / f"{config.run_id}_runtime_staging" / "input"
    input_dir.mkdir(parents=True)
    staging_budget = object_store_validation.RuntimeStagingBudget(
        max_file_count=4,
        max_node_count=4,
        max_directory_depth=8,
        max_total_bytes=1024,
        max_object_bytes=object_store_validation.MAX_RUNTIME_STAGING_OBJECT_BYTES,
    )

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        object_store_validation._collect_runtime_object_or_prefix(
            config,
            store,
            f"runs/{config.run_id}/input/scratch/runtime-staging/forcing/",
            input_dir,
            staging_budget,
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert message in exc_info.value.message
    assert list(input_dir.rglob("*")) == []


def test_runtime_forcing_prefix_rejects_fifo_child_without_blocking(
    tmp_path: Path,
) -> None:
    config = ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m10_148_fifo_force")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True)
    writer.prepare()
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/runtime-prefix")
    prefix = Path(store.root) / "runs" / config.run_id / "input" / "scratch" / "runtime-staging" / "forcing"
    prefix.mkdir(parents=True)
    os.mkfifo(prefix / "forcing.tsd.forc")
    input_dir = config.lane_dir / "runtime-workspace" / "runs" / f"{config.run_id}_runtime_staging" / "input"
    input_dir.mkdir(parents=True)
    staging_budget = object_store_validation.RuntimeStagingBudget(
        max_file_count=4,
        max_node_count=4,
        max_directory_depth=8,
        max_total_bytes=1024,
        max_object_bytes=object_store_validation.MAX_RUNTIME_STAGING_OBJECT_BYTES,
    )

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        object_store_validation._collect_runtime_object_or_prefix(
            config,
            store,
            f"runs/{config.run_id}/input/scratch/runtime-staging/forcing/",
            input_dir,
            staging_budget,
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert "regular files or directories" in exc_info.value.message
    assert list(input_dir.rglob("*")) == []


def test_runtime_forcing_prefix_rejects_extra_non_validation_child_before_staging(
    tmp_path: Path,
) -> None:
    config = ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m10_148_extra_force")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True)
    writer.prepare()
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/runtime-prefix")
    current_key = (
        f"runs/{config.run_id}/input/scratch/runtime-staging/forcing/gfs/2026051600/basin_v1/model/"
        "forcing.tsd.forc"
    )
    stale_key = f"runs/{config.run_id}/input/scratch/runtime-staging/forcing/stale/forcing.tsd.forc"
    store.write_bytes_atomic(current_key, b"current\n")
    store.write_bytes_atomic(stale_key, b"stale\n")
    input_dir = config.lane_dir / "runtime-workspace" / "runs" / f"{config.run_id}_runtime_staging" / "input"
    input_dir.mkdir(parents=True)
    staging_budget = object_store_validation.RuntimeStagingBudget(
        max_file_count=4,
        max_node_count=8,
        max_directory_depth=8,
        max_total_bytes=1024,
        max_object_bytes=object_store_validation.MAX_RUNTIME_STAGING_OBJECT_BYTES,
    )

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        object_store_validation._collect_runtime_object_or_prefix(
            config,
            store,
            f"runs/{config.run_id}/input/scratch/runtime-staging/forcing/",
            input_dir,
            staging_budget,
            allowed_keys={current_key},
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert "non-validation object" in exc_info.value.message
    assert stale_key.rsplit("/", maxsplit=1)[0] in exc_info.value.message
    assert list(input_dir.rglob("*")) == []


def test_validate_object_store_blocks_preexisting_extra_runtime_forcing_prefix_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "m10_148_extra_prefix"
    object_root = tmp_path / "object-store"
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/runtime-prefix")
    stale_file = (
        object_root
        / "runs"
        / run_id
        / "input"
        / "scratch"
        / "runtime-staging"
        / "forcing"
        / "gfs"
        / "2026051600"
        / "basin_v1"
        / "basins_basin_a_shud"
        / "stale"
        / "forcing.tsd.forc"
    )
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("stale forcing must not be staged\n", encoding="utf-8")

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id=run_id))

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert "non-validation object" in exc_info.value.message
    lane_dir = tmp_path / "artifacts" / run_id / "object-store"
    input_dir = lane_dir / "runtime-workspace" / "runs" / f"{run_id}_runtime_staging" / "input"
    assert list(input_dir.rglob("*")) == []


def test_runtime_staging_rejects_package_forcing_target_collision_before_writes(
    tmp_path: Path,
) -> None:
    config = ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m10_148_collision")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True)
    writer.prepare()
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/runtime-prefix")
    package_key = "models/collision/alias-a.cfg.para"
    forcing_key = f"runs/{config.run_id}/input/scratch/runtime-staging/forcing/alias-a.cfg.para"
    package_content = b"package cfg\n"
    forcing_content = b"forcing cfg\n"
    package_uri = store.write_bytes_atomic(package_key, package_content)
    store.write_bytes_atomic(forcing_key, forcing_content)
    package_sha = hashlib.sha256(package_content).hexdigest()
    input_dir = config.lane_dir / "runtime-workspace" / "runs" / f"{config.run_id}_runtime_staging" / "input"
    output_dir = config.lane_dir / "runtime-workspace" / "runs" / f"{config.run_id}_runtime_staging" / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    package_manifest = {
        "included_files": [
            {
                "role": "cfg",
                "relative_path": "alias-a.cfg.para",
                "object_uri": package_uri,
                "size_bytes": len(package_content),
                "sha256": package_sha,
            }
        ]
    }
    stored_verification = {
        "entries": [
            {
                "role": "cfg",
                "relative_path": "alias-a.cfg.para",
                "object_uri": package_uri,
                "expected_size_bytes": len(package_content),
                "expected_sha256": package_sha,
                "verified": True,
            }
        ]
    }
    runtime_manifest = {
        "run_id": f"{config.run_id}_runtime_staging",
        "start_time": "2026-05-16T00:00:00Z",
        "end_time": "2026-05-17T00:00:00Z",
        "model": {"model_id": "model", "project_name": "alias-a", "segment_count": 2},
        "forcing": {
            "forcing_uri": store.uri_for_key(
                f"runs/{config.run_id}/input/scratch/runtime-staging/forcing/"
            )
        },
        "runtime": {"output_interval_minutes": 1440},
    }

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        object_store_validation._prepare_runtime_staging_workspace(
            config,
            store,
            runtime_manifest,
            package_manifest,
            stored_verification,
            input_dir,
            output_dir,
            allowed_forcing_keys={forcing_key},
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert "target path collision before write" in exc_info.value.message
    assert list(input_dir.rglob("*")) == []


def test_validate_object_store_blocks_package_object_changed_between_verification_and_runtime_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "m10_148_stage_tamper"
    tampered = False
    original_verify = object_store_validation._verify_stored_objects

    def verify_then_tamper(store: LocalObjectStore, manifest: dict[str, object]) -> dict[str, object]:
        nonlocal tampered
        verification = original_verify(store, manifest)
        package_entry = next(
            entry
            for entry in manifest["included_files"]
            if isinstance(entry, dict) and entry.get("role") != "manifest"
        )
        store.write_bytes_atomic(str(package_entry["object_uri"]), b"tampered runtime package bytes\n")
        tampered = True
        return verification

    monkeypatch.setattr(object_store_validation, "_verify_stored_objects", verify_then_tamper)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id=run_id))

    assert tampered is True
    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert "differ from verified manifest contract" in exc_info.value.message


def test_validate_object_store_runtime_evidence_includes_package_and_forcing_size_sha_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/runtime-prefix")

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_receipts"]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    lane_dir = tmp_path / "artifacts" / "m10_148_receipts" / "object-store"
    consumption = json.loads((lane_dir / "registry_api_runtime_consumption.json").read_text(encoding="utf-8"))
    stored = json.loads((lane_dir / "stored_object_verification.json").read_text(encoding="utf-8"))
    package_receipts = consumption["runtime"]["staged_object_receipts"]["package"]
    forcing_receipts = consumption["runtime"]["staged_object_receipts"]["forcing"]
    forcing_prefix_receipt = consumption["runtime"]["forcing_prefix_receipt"]

    assert package_receipts
    assert forcing_receipts
    assert forcing_prefix_receipt["file_count"] == len(forcing_receipts) == len(forcing_prefix_receipt["objects"])
    for receipt in [*package_receipts, *forcing_receipts]:
        assert receipt["size_bytes"] > 0
        assert len(receipt["sha256"]) == 64
        assert receipt["object_uri"].startswith("s3://nhms-prod/runtime-prefix/")
        assert receipt["relative_path"]
    verified_by_uri = {entry["object_uri"]: entry for entry in stored["entries"] if entry["role"] != "manifest"}
    for receipt in package_receipts:
        verified = verified_by_uri[receipt["object_uri"]]
        assert receipt["verified_manifest_contract"] is True
        assert receipt["relative_path"] == verified["relative_path"]
        assert receipt["role"] == verified["role"]
        assert receipt["size_bytes"] == verified["actual_size_bytes"]
        assert receipt["sha256"] == verified["actual_sha256"]


def test_validate_object_store_rejects_preexisting_runtime_workspace_stale_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "m10_148_stale_workspace"
    evidence_root = tmp_path / "artifacts"
    input_dir = (
        evidence_root
        / run_id
        / "object-store"
        / "runtime-workspace"
        / "runs"
        / f"{run_id}_runtime_staging"
        / "input"
    )
    input_dir.mkdir(parents=True)
    stale_file = input_dir / "stale.tsd.forc"
    stale_file.write_text("stale forcing must not satisfy readiness\n", encoding="utf-8")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/runtime-prefix")

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(ProductionObjectStoreConfig.from_env(evidence_root=evidence_root, run_id=run_id))

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert "must be empty before runtime staging" in exc_info.value.message


def test_validate_object_store_runtime_staged_files_are_receipt_derived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/runtime-prefix")

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_receipt_files"]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    lane_dir = tmp_path / "artifacts" / "m10_148_receipt_files" / "object-store"
    consumption = json.loads((lane_dir / "registry_api_runtime_consumption.json").read_text(encoding="utf-8"))
    runtime = consumption["runtime"]
    input_dir = lane_dir / "runtime-workspace" / "runs" / "m10_148_receipt_files_runtime_staging" / "input"
    runtime_input_relative = Path("runtime-workspace/runs/m10_148_receipt_files_runtime_staging/input")
    receipt_files = {
        Path(receipt["target_relative_path"]).relative_to(runtime_input_relative).as_posix()
        for receipt in [
            *runtime["staged_object_receipts"]["package"],
            *runtime["staged_object_receipts"]["forcing"],
        ]
    }
    generated_cfg = Path(runtime["generated_cfg_path"]).relative_to(input_dir).as_posix()
    assert runtime["staged_files"] == sorted({*receipt_files, generated_cfg})
    assert runtime["staged_file_count"] == len(runtime["staged_files"])


def test_validate_object_store_live_registry_import_opt_in_requires_database_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/runtime-prefix")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_RUN_REGISTRY_IMPORT", "1")
    monkeypatch.delenv("NHMS_PRODUCTION_OBJECT_STORE_REGISTRY_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_import_no_db"]
    )

    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "m10_148_import_no_db" / "object-store"
    consumption = json.loads((lane_dir / "registry_api_runtime_consumption.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert summary["status"] == "blocked"
    assert consumption["status"] == "blocked"
    assert consumption["registry"]["status"] == "blocked"
    assert consumption["registry"]["db_import_status"] == "blocked"
    assert consumption["registry"]["error_code"] == "PRODUCTION_OBJECT_STORE_REGISTRY_DATABASE_URL_MISSING"
    assert consumption["registry"]["live_registry_import"] is False
    assert consumption["acceptance_evidence"] == "live_registry_import_blocked"
    assert consumption["api"]["api_contract_source"] == "local_import_source"


def test_validate_object_store_live_registry_import_opt_in_records_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/runtime-prefix")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_RUN_REGISTRY_IMPORT", "1")
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_REGISTRY_DATABASE_URL", "postgresql://user:pass@db.example/nhms")

    def fake_import_basins_registry(
        *,
        inventory_path: str | Path,
        package_manifest_path: str | Path,
        database_url: str | None = None,
        output_path: str | Path | None = None,
        policy_decision: object | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, object]:
        assert Path(inventory_path).exists()
        manifest = json.loads(Path(package_manifest_path).read_text(encoding="utf-8"))
        assert database_url == "postgresql://user:pass@db.example/nhms"
        assert output_path is None
        assert policy_decision is None
        assert trusted_internal is True
        return {
            "schema_version": "basins.registry_import.v1",
            "status": "imported",
            "model_id": manifest["model_id"],
            "basin_id": "basins_basin_a",
            "basin_version_id": "basin_v1",
            "river_network_version_id": "river_v1",
            "mesh_version_id": "mesh_v1",
            "active": False,
            "segment_count": 2,
            "row_counts": {
                "basin": 1,
                "basin_version": 1,
                "river_network_version": 1,
                "river_segment": 2,
                "mesh_version": 1,
                "model_instance": 1,
            },
            "model_package_uri": manifest["model_package_uri"],
            "manifest_uri": manifest["manifest_uri"],
            "package_checksum": manifest["package_checksum"],
        }

    monkeypatch.setattr(object_store_validation, "import_basins_registry", fake_import_basins_registry)

    exit_code = slurm_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "m10_148_import_live"]
    )

    summary = json.loads(capsys.readouterr().out)
    lane_dir = tmp_path / "artifacts" / "m10_148_import_live" / "object-store"
    consumption = json.loads((lane_dir / "registry_api_runtime_consumption.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert summary["status"] == "ready"
    assert summary["execution_mode"] == "live_registry_import_with_deterministic_api_contract"
    assert summary["deterministic_fixture"] is True
    assert summary["live_registry_import"] is True
    assert summary["live_api"] is False
    assert summary["live_api_status"] == "not_executed"
    assert summary["api_contract_source"] == "live_registry_import"
    assert summary["final_production_readiness_claimed"] is False
    assert consumption["status"] == "ready"
    assert consumption["live_registry_import"] is True
    assert consumption["acceptance_evidence"] == "live_registry_import_contract_smoke"
    assert consumption["api_contract_source"] == "live_registry_import"
    assert consumption["registry"]["status"] == "imported"
    assert consumption["registry"]["db_import_status"] == "imported"
    assert consumption["registry"]["live_registry_import"] is True
    assert consumption["registry"]["inserted_total"] == 7
    assert consumption["registry"]["updated_total"] == 0
    assert consumption["registry"]["idempotent"] is False
    assert consumption["registry"]["implicit_activation"] is False
    assert consumption["registry"]["active"] is False
    assert consumption["api"]["api_contract_source"] == "live_registry_import"
    values = [
        consumption["registry"]["model_package_uri"],
        consumption["registry"]["manifest_uri"],
        consumption["api"]["model_response_fixture"]["model_package_uri"],
        consumption["api"]["model_response_fixture"]["manifest_uri"],
        consumption["runtime"]["runtime_manifest"]["model_package_uri"],
        consumption["runtime"]["runtime_manifest"]["manifest_uri"],
    ]
    assert all(value.startswith("s3://nhms-prod/runtime-prefix/") for value in values)
    assert all("data/Basins" not in value and "/volume/" not in value for value in values)


def test_validate_object_store_runtime_smoke_does_not_clobber_production_like_forcing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_root = tmp_path / "object-store"
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/runtime-prefix")
    production_like_forcing = object_root / "forcing" / "gfs" / "2026051600" / "basin_v1"
    production_like_forcing.mkdir(parents=True)
    existing_file = production_like_forcing / "basins_basin_a_shud" / "forcing.tsd.forc"
    existing_file.parent.mkdir()
    existing_file.write_text("do not clobber\n", encoding="utf-8")

    exit_code = slurm_validation.main(
        [
            "validate-object-store",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "m10_148_no_clobber",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert existing_file.read_text(encoding="utf-8") == "do not clobber\n"
    scratch_root = object_root / "runs" / "m10_148_no_clobber" / "input" / "scratch"
    scratch_forcing = next(scratch_root.rglob("forcing.tsd.forc"))
    assert scratch_forcing.read_text(encoding="utf-8") == "forcing\n"


def test_validate_object_store_refuses_existing_runtime_scratch_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_root = tmp_path / "object-store"
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", "s3://nhms-prod/runtime-prefix")
    existing_file = (
        object_root
        / "runs"
        / "m10_148_scratch_exists"
        / "input"
        / "scratch"
        / "runtime-staging"
        / "forcing"
        / "gfs"
        / "2026051600"
        / "basin_v1"
        / "basins_basin_a_shud"
        / "forcing.tsd.forc"
    )
    existing_file.parent.mkdir(parents=True)
    existing_file.write_text("existing scratch\n", encoding="utf-8")

    try:
        exit_code = slurm_validation.main(
            [
                "validate-object-store",
                "--evidence-root",
                str(tmp_path / "artifacts"),
                "--run-id",
                "m10_148_scratch_exists",
            ]
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "PRODUCTION_OBJECT_STORE_VALIDATION_OBJECT_EXISTS" in captured.err
    assert existing_file.read_text(encoding="utf-8") == "existing scratch\n"


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


def test_validate_object_store_stdout_redacts_summary_like_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_package_uri = "s3://user:pass@bucket/path?token=secret&X-Amz-Signature=abc"

    def fake_validate(config: ProductionObjectStoreConfig) -> dict[str, object]:
        return {
            "schema": "nhms.production_closure.object_store.v1",
            "run_id": config.run_id,
            "status": "ready",
            "evidence_dir": str(config.lane_dir),
            "model_package_uri": secret_package_uri,
            "notes": "path token=secret x-amz-signature=abc credential=hidden",
        }

    monkeypatch.setattr(object_store_validation, "validate_object_store", fake_validate)

    exit_code = object_store_validation.main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts"), "--run-id", "stdout_redact"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "user:pass@" not in captured.out
    assert "?token=secret" not in captured.out
    assert "token=secret" not in captured.out
    assert "x-amz-signature=abc" not in captured.out
    assert "credential=hidden" not in captured.out
    assert json.loads(captured.out)["model_package_uri"] == "s3://bucket/path"

    exit_code = _argparse_main(
        ["validate-object-store", "--evidence-root", str(tmp_path / "artifacts-argparse"), "--run-id", "argparse"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "user:pass@" not in captured.out
    assert "?token=secret" not in captured.out
    assert "token=secret" not in captured.out
    assert "x-amz-signature=abc" not in captured.out
    assert "credential=hidden" not in captured.out
    assert json.loads(captured.out)["model_package_uri"] == "s3://bucket/path"


def test_validate_object_store_standalone_click_usage_errors_exit_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.production_closure.object_store_validation",
            "validate-object-store",
            "--bad-option",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr
    assert "No such option" in result.stderr
    assert "--bad-option" in result.stderr
    assert "Traceback" not in result.stderr


def test_verify_stored_objects_streams_package_entries_without_full_reads(
    tmp_path: Path,
) -> None:
    summary = validate_object_store(
        ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m10_148_streaming")
    )
    manifest_path = tmp_path / "artifacts" / "m10_148_streaming" / "object-store" / "package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stream_calls: list[str] = []

    class RecordingStore(LocalObjectStore):
        def read_bytes(self, key_or_uri: str) -> bytes:
            raise AssertionError(f"full read must not be used during stored object verification: {key_or_uri}")

        def read_bytes_limited(self, key_or_uri: str, *, max_bytes: int) -> bytes:
            assert key_or_uri == summary["manifest_uri"]
            return super().read_bytes_limited(key_or_uri, max_bytes=max_bytes)

        def size_and_checksum(self, key_or_uri: str, *, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
            stream_calls.append(key_or_uri)
            return super().size_and_checksum(key_or_uri, chunk_size=chunk_size)

    store = RecordingStore(
        tmp_path / "artifacts" / "m10_148_streaming" / "object-store" / "local-object-store",
        "s3://nhms-production-like/m10_148_streaming",
    )

    verification = _verify_stored_objects(store, manifest)

    assert verification["status"] == "verified"
    assert stream_calls
    assert any(call != summary["manifest_uri"] for call in stream_calls)


def test_local_object_store_read_bytes_limited_uses_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalObjectStore(tmp_path)
    key = "runs/m10_148_bounded/input/manifest.json"
    store.write_bytes_atomic(key, b"abcdef")
    path = store.resolve_path(key)
    read_sizes: list[int] = []
    original_os_read = safe_fs.os.read

    def fake_os_read(fd: int, size: int) -> bytes:
        read_sizes.append(size)
        return original_os_read(fd, size)

    original_read_bytes = type(path).read_bytes

    def forbidden_read_bytes(self: Path) -> bytes:
        if self != path:
            return original_read_bytes(self)
        raise AssertionError("read_bytes() must not be used for limited reads")

    monkeypatch.setattr(safe_fs.os, "read", fake_os_read)
    monkeypatch.setattr(type(path), "read_bytes", forbidden_read_bytes)

    with pytest.raises(object_store_validation.ObjectStoreError):
        store.read_bytes_limited(key, max_bytes=5)

    assert read_sizes == [6]


@pytest.mark.parametrize("operation", ["read_bytes_limited", "iter_bytes", "size_and_checksum"])
def test_local_object_store_rejects_fifo_entry_without_blocking(
    tmp_path: Path,
    operation: str,
) -> None:
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/m10")
    key = "runs/m10_148_fifo/input/manifest.json"
    fifo_path = store.resolve_path(key)
    fifo_path.parent.mkdir(parents=True)
    os.mkfifo(fifo_path)

    with pytest.raises(ObjectStoreError):
        if operation == "read_bytes_limited":
            store.read_bytes_limited(key, max_bytes=1024)
        elif operation == "iter_bytes":
            next(store.iter_bytes(key))
        else:
            store.size_and_checksum(key)


@pytest.mark.parametrize("helper_name", ["unlink_no_follow", "rmtree_no_follow"])
def test_safe_fs_rejects_parent_traversal_under_containment_root(
    tmp_path: Path,
    helper_name: str,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("external must remain\n", encoding="utf-8")

    if helper_name == "unlink_no_follow":
        with pytest.raises(safe_fs.SafeFilesystemError):
            safe_fs.unlink_no_follow(root / ".." / "outside" / "victim.txt", containment_root=root)
        assert victim.read_text(encoding="utf-8") == "external must remain\n"
    else:
        with pytest.raises(safe_fs.SafeFilesystemError):
            safe_fs.rmtree_no_follow(root / ".." / "outside", containment_root=root)
        assert victim.read_text(encoding="utf-8") == "external must remain\n"


@pytest.mark.parametrize("case", ["parent", "root"])
def test_safe_fs_ensure_directory_no_follow_rejects_uncontained_symlink_components(
    tmp_path: Path,
    case: str,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    symlink = tmp_path / "link"
    symlink.symlink_to(external, target_is_directory=True)
    target = symlink / "child" if case == "parent" else symlink

    with pytest.raises(safe_fs.SafeFilesystemError):
        safe_fs.ensure_directory_no_follow(target)

    assert sorted(path.name for path in external.iterdir()) == []


@pytest.mark.parametrize("case", ["parent", "root"])
def test_local_object_store_rejects_symlinked_root_identity(
    tmp_path: Path,
    case: str,
) -> None:
    external = tmp_path / "external-store"
    external.mkdir()
    symlink = tmp_path / "store-link"
    symlink.symlink_to(external, target_is_directory=True)
    root = symlink / "child" if case == "parent" else symlink

    with pytest.raises(ObjectStoreError):
        LocalObjectStore(root, "s3://nhms-prod/m10")

    assert sorted(path.name for path in external.iterdir()) == []


@pytest.mark.parametrize(
    "operation",
    ["read_bytes_limited", "size_and_checksum", "write_bytes_atomic", "delete"],
)
def test_local_object_store_rejects_symlinked_object_key_parent_identity(
    tmp_path: Path,
    operation: str,
) -> None:
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/m10")
    stale_dir = tmp_path / "object-store" / "runs" / "stale" / "input"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "manifest.json"
    stale_file.write_bytes(b"stale sibling content\n")
    current_parent = tmp_path / "object-store" / "runs" / "current"
    current_parent.parent.mkdir(parents=True, exist_ok=True)
    current_parent.symlink_to(tmp_path / "object-store" / "runs" / "stale", target_is_directory=True)
    key = "runs/current/input/manifest.json"

    with pytest.raises(ObjectStoreError):
        if operation == "read_bytes_limited":
            store.read_bytes_limited(key, max_bytes=1024)
        elif operation == "size_and_checksum":
            store.size_and_checksum(key)
        elif operation == "write_bytes_atomic":
            store.write_bytes_atomic(key, b"new content\n")
        else:
            store.delete(key)

    assert stale_file.read_bytes() == b"stale sibling content\n"


def test_local_object_store_rejects_parent_symlink_swap_before_write_or_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/m10")
    external = tmp_path / "external-store"
    external.mkdir()
    original_verify = safe_fs._verify_fd_matches_path
    swapped = False

    def swap_run_parent(fd: int, path: Path) -> None:
        nonlocal swapped
        if path.name == "input" and path.parent.name == "run_001" and not swapped:
            swapped = True
            run_dir = path.parent
            run_dir.rmdir()
            run_dir.symlink_to(external, target_is_directory=True)
        original_verify(fd, path)

    monkeypatch.setattr(safe_fs, "_verify_fd_matches_path", swap_run_parent)
    with pytest.raises(ObjectStoreError):
        store.write_bytes_atomic("runs/run_001/input/manifest.json", b"manifest\n")
    assert not (external / "input" / "manifest.json").exists()

    monkeypatch.setattr(safe_fs, "_verify_fd_matches_path", original_verify)
    run_dir = tmp_path / "object-store" / "runs" / "run_001"
    if run_dir.is_symlink():
        run_dir.unlink()
    safe_fs.rmtree_no_follow(run_dir, containment_root=tmp_path / "object-store", missing_ok=True)
    (run_dir / "input").mkdir(parents=True)
    target = run_dir / "input" / "manifest.json"
    target.write_text("internal\n", encoding="utf-8")
    external_target = external / "input" / "manifest.json"
    external_target.parent.mkdir(parents=True)
    external_target.write_text("external must remain\n", encoding="utf-8")
    original_open_parent = safe_fs._open_parent_dir
    swapped_for_delete = False

    def swap_delete_parent(path: Path, *, containment_root: Path | None, create: bool):
        nonlocal swapped_for_delete
        if path == target and not swapped_for_delete:
            swapped_for_delete = True
            safe_fs.rmtree_no_follow(run_dir, containment_root=tmp_path / "object-store")
            run_dir.symlink_to(external, target_is_directory=True)
        return original_open_parent(path, containment_root=containment_root, create=create)

    monkeypatch.setattr(safe_fs, "_open_parent_dir", swap_delete_parent)
    with pytest.raises(ObjectStoreError):
        store.delete("runs/run_001/input/manifest.json")
    assert external_target.read_text(encoding="utf-8") == "external must remain\n"


@pytest.mark.parametrize(
    ("lane_name", "module", "error_type", "error_code"),
    [
        (
            "e2e",
            e2e_validation,
            e2e_validation.ProductionE2EValidationError,
            "PRODUCTION_E2E_EVIDENCE_PATH_UNSAFE",
        ),
        (
            "met",
            met_validation,
            met_validation.ProductionMetValidationError,
            "PRODUCTION_MET_EVIDENCE_PATH_UNSAFE",
        ),
        (
            "object-store",
            object_store_validation,
            ProductionObjectStoreValidationError,
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
        ),
        (
            "ops",
            ops_validation,
            ops_validation.ProductionOpsValidationError,
            "PRODUCTION_OPS_EVIDENCE_PATH_UNSAFE",
        ),
        (
            "scale",
            scale_validation,
            scale_validation.ProductionScaleValidationError,
            "PRODUCTION_SCALE_EVIDENCE_PATH_UNSAFE",
        ),
        (
            "slurm",
            slurm_validation,
            slurm_validation.ProductionValidationError,
            "PRODUCTION_SLURM_EVIDENCE_PATH_UNSAFE",
        ),
    ],
)
def test_production_evidence_writers_prepare_reject_lane_parent_symlink_swap_without_external_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane_name: str,
    module: object,
    error_type: type[Exception],
    error_code: str,
) -> None:
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / f"prepare-swap-{lane_name}" / lane_name
    external = tmp_path / f"external-{lane_name}"
    external.mkdir()
    writer = module.EvidenceWriter(evidence_root, lane_dir, force=True)
    original_ensure = module.ensure_directory_no_follow
    swapped = False

    def swap_lane_parent(path: Path, *, containment_root: Path | None = None) -> Path:
        nonlocal swapped
        if Path(path) == lane_dir and not swapped:
            swapped = True
            lane_dir.parent.symlink_to(external, target_is_directory=True)
        return original_ensure(path, containment_root=containment_root)

    monkeypatch.setattr(module, "ensure_directory_no_follow", swap_lane_parent)

    with pytest.raises(error_type) as exc_info:
        writer.prepare()

    assert swapped is True
    assert exc_info.value.error_code == error_code
    assert sorted(path.name for path in external.iterdir()) == []


@pytest.mark.parametrize(
    "child_name",
    [
        "synthetic-basins",
        ".inventory.raw.json",
        ".package_manifest.raw.json",
        ".migration_report.raw.json",
        "runtime-workspace",
        "local-object-store",
    ],
)
def test_validate_object_store_refuses_same_run_lane_child_symlink_without_external_write(
    tmp_path: Path,
    child_name: str,
) -> None:
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / "symlink-child" / "object-store"
    external_dir = tmp_path / "external-target"
    external_dir.mkdir()
    external_file = external_dir / "sentinel.txt"
    external_file.write_text("external must remain\n", encoding="utf-8")
    lane_dir.mkdir(parents=True)
    (lane_dir / child_name).symlink_to(external_dir, target_is_directory=True)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(
            ProductionObjectStoreConfig.from_env(
                evidence_root=evidence_root,
                run_id="symlink-child",
                force=True,
            )
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK"
    assert external_file.read_text(encoding="utf-8") == "external must remain\n"
    assert sorted(path.name for path in external_dir.iterdir()) == ["sentinel.txt"]


def test_object_store_evidence_writer_rejects_lane_parent_symlink_swap_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="swap")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True)
    writer.prepare()
    external = tmp_path / "external"
    external.mkdir()
    original_verify = safe_fs._verify_fd_matches_path
    swapped = False

    def swap_lane_parent(fd: int, path: Path) -> None:
        nonlocal swapped
        if path == config.lane_dir and not swapped:
            swapped = True
            config.lane_dir.rmdir()
            config.lane_dir.symlink_to(external, target_is_directory=True)
        original_verify(fd, path)

    monkeypatch.setattr(safe_fs, "_verify_fd_matches_path", swap_lane_parent)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        writer.write_json(config.lane_dir / "summary.json", {"status": "ready"})

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert not (external / "summary.json").exists()


def test_validate_object_store_refuses_raw_cleanup_parent_symlink_swap_without_external_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_root = tmp_path / "artifacts"
    config = ProductionObjectStoreConfig.from_env(evidence_root=evidence_root, run_id="rawswap", force=True)
    lane_dir = config.lane_dir
    lane_dir.mkdir(parents=True)
    external = tmp_path / "external-raw"
    external.mkdir()
    external_file = external / ".inventory.raw.json"
    external_file.write_text("external must remain\n", encoding="utf-8")
    original_open_parent = safe_fs._open_parent_dir
    swapped = False

    def swap_raw_parent(path: Path, *, containment_root: Path | None, create: bool):
        nonlocal swapped
        if path == lane_dir / ".inventory.raw.json" and not swapped:
            swapped = True
            safe_fs.rmtree_no_follow(lane_dir, containment_root=evidence_root)
            lane_dir.symlink_to(external, target_is_directory=True)
        return original_open_parent(path, containment_root=containment_root, create=create)

    monkeypatch.setattr(safe_fs, "_open_parent_dir", swap_raw_parent)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(config)

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert external_file.read_text(encoding="utf-8") == "external must remain\n"


@pytest.mark.parametrize(
    ("raw_name", "helper_name"),
    [
        (".inventory.raw.json", "write_inventory"),
        (".migration_report.raw.json", "write_basins_migration_report"),
        (".package_manifest.raw.json", "publish_basins_package"),
    ],
)
def test_validate_object_store_raw_lane_write_rejects_symlink_swap_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_name: str,
    helper_name: str,
) -> None:
    evidence_root = tmp_path / "artifacts"
    config = ProductionObjectStoreConfig.from_env(evidence_root=evidence_root, run_id=f"rawswap-{helper_name}")
    lane_dir = config.lane_dir
    external = tmp_path / f"external-{helper_name}"
    external.mkdir()
    external_raw = external / raw_name
    external_raw.write_text("external must remain\n", encoding="utf-8")
    original_helper = getattr(object_store_validation, helper_name)
    original_atomic_write = safe_fs.atomic_write_bytes_no_follow
    swapped = False
    helper_called = False

    def wrapped_helper(*args: object, **kwargs: object) -> object:
        nonlocal helper_called
        output_path = Path(kwargs.get("output_path") or (args[1] if helper_name == "write_inventory" else ""))
        if output_path.name == raw_name:
            helper_called = True
        return original_helper(*args, **kwargs)

    def swap_lane_before_raw_write(
        path: Path,
        content: bytes,
        *,
        containment_root: Path | None = None,
        temp_suffix: str = "tmp",
    ) -> Path:
        nonlocal swapped
        if path == lane_dir / raw_name and containment_root == lane_dir and not swapped:
            swapped = True
            safe_fs.rmtree_no_follow(lane_dir, containment_root=evidence_root)
            lane_dir.symlink_to(external, target_is_directory=True)
        return original_atomic_write(path, content, containment_root=containment_root, temp_suffix=temp_suffix)

    monkeypatch.setattr(object_store_validation, helper_name, wrapped_helper)
    monkeypatch.setattr(safe_fs, "atomic_write_bytes_no_follow", swap_lane_before_raw_write)
    monkeypatch.setattr(object_store_validation, "atomic_write_bytes_no_follow", swap_lane_before_raw_write)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(config)

    assert helper_called is True
    assert swapped is True
    assert exc_info.value.error_code in {
        "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
        "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
    }
    assert external_raw.read_text(encoding="utf-8") == "external must remain\n"
    assert sorted(path.name for path in external.iterdir()) == [raw_name]


def test_validate_object_store_existing_lane_regular_file_raises_stable_error(tmp_path: Path) -> None:
    lane_path = tmp_path / "artifacts" / "file_lane" / "object-store"
    lane_path.parent.mkdir(parents=True)
    lane_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(
            ProductionObjectStoreConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="file_lane")
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"


@pytest.mark.parametrize("suffix", ["new-root", "missing/deep"])
def test_validate_object_store_rejects_primary_evidence_root_under_existing_symlink(
    tmp_path: Path,
    suffix: str,
) -> None:
    target_root = tmp_path / "target-root"
    target_root.mkdir()
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(target_root, target_is_directory=True)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        ProductionObjectStoreConfig.from_env(evidence_root=symlink_root / suffix, run_id="safe")

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK"
    assert not (target_root / suffix).exists()


def test_validate_object_store_refuses_nested_synthetic_basins_symlink_without_external_write(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / "nested-symlink" / "object-store"
    fixture_dir = lane_dir / "synthetic-basins"
    external_dir = tmp_path / "external-nested-target"
    external_dir.mkdir()
    external_file = external_dir / "sentinel.txt"
    external_file.write_text("external must remain\n", encoding="utf-8")
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "basin-a").symlink_to(external_dir, target_is_directory=True)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(
            ProductionObjectStoreConfig.from_env(
                evidence_root=evidence_root,
                run_id="nested-symlink",
                force=True,
            )
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK"
    assert external_file.read_text(encoding="utf-8") == "external must remain\n"
    assert sorted(path.name for path in external_dir.iterdir()) == ["sentinel.txt"]


def test_validate_object_store_synthetic_fixture_refuses_symlink_swap_before_mkdir_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / "fixture-mkdir-swap" / "object-store"
    config = ProductionObjectStoreConfig.from_env(evidence_root=evidence_root, run_id="fixture-mkdir-swap")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True)
    writer.prepare()
    external = tmp_path / "external-fixture-mkdir"
    external.mkdir()
    fixture_root = lane_dir / "synthetic-basins"
    original_ensure = safe_fs.ensure_directory_no_follow
    swapped = False

    def swap_fixture_root_before_dir_create(path: Path, *, containment_root: Path | None = None) -> Path:
        nonlocal swapped
        if path == fixture_root / "basin-a" / "input" / "alias-a" and not swapped:
            swapped = True
            fixture_root.symlink_to(external, target_is_directory=True)
        return original_ensure(path, containment_root=containment_root)

    monkeypatch.setattr(safe_fs, "ensure_directory_no_follow", swap_fixture_root_before_dir_create)
    monkeypatch.setattr(object_store_validation, "ensure_directory_no_follow", swap_fixture_root_before_dir_create)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        write_synthetic_basins_fixture(fixture_root, containment_root=lane_dir)

    assert swapped is True
    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert sorted(path.name for path in external.iterdir()) == []


def test_validate_object_store_synthetic_fixture_refuses_symlink_swap_before_write_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / "fixture-write-swap" / "object-store"
    config = ProductionObjectStoreConfig.from_env(evidence_root=evidence_root, run_id="fixture-write-swap")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True)
    writer.prepare()
    external = tmp_path / "external-fixture-write"
    external.mkdir()
    external_file = external / "alias-a.cfg.para"
    external_file.write_text("external must remain\n", encoding="utf-8")
    fixture_root = lane_dir / "synthetic-basins"
    input_dir = fixture_root / "basin-a" / "input" / "alias-a"
    original_atomic_write = safe_fs.atomic_write_bytes_no_follow
    swapped = False

    def swap_input_dir_before_fixture_write(
        path: Path,
        content: bytes,
        *,
        containment_root: Path | None = None,
        temp_suffix: str = "tmp",
    ) -> Path:
        nonlocal swapped
        if path == input_dir / "alias-a.cfg.para" and not swapped:
            swapped = True
            safe_fs.rmtree_no_follow(input_dir, containment_root=lane_dir)
            input_dir.symlink_to(external, target_is_directory=True)
        return original_atomic_write(path, content, containment_root=containment_root, temp_suffix=temp_suffix)

    monkeypatch.setattr(safe_fs, "atomic_write_bytes_no_follow", swap_input_dir_before_fixture_write)
    monkeypatch.setattr(object_store_validation, "atomic_write_bytes_no_follow", swap_input_dir_before_fixture_write)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        write_synthetic_basins_fixture(fixture_root, containment_root=lane_dir)

    assert swapped is True
    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert external_file.read_text(encoding="utf-8") == "external must remain\n"
    assert sorted(path.name for path in external.iterdir()) == ["alias-a.cfg.para"]


def test_validate_object_store_runtime_staging_refuses_symlink_swap_before_workspace_mkdir_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "runtime-mkdir-swap"
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / run_id / "object-store"
    external = tmp_path / "external-runtime-workspace"
    external.mkdir()
    original_ensure = safe_fs.ensure_directory_no_follow
    swapped = False

    def swap_runtime_run_before_dir_create(path: Path, *, containment_root: Path | None = None) -> Path:
        nonlocal swapped
        if path == lane_dir / "runtime-workspace" / "runs" / f"{run_id}_runtime_staging" / "input" and not swapped:
            swapped = True
            runtime_root = lane_dir / "runtime-workspace"
            runtime_root.symlink_to(external, target_is_directory=True)
        return original_ensure(path, containment_root=containment_root)

    monkeypatch.setattr(safe_fs, "ensure_directory_no_follow", swap_runtime_run_before_dir_create)
    monkeypatch.setattr(object_store_validation, "ensure_directory_no_follow", swap_runtime_run_before_dir_create)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(ProductionObjectStoreConfig.from_env(evidence_root=evidence_root, run_id=run_id))

    assert swapped is True
    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert sorted(path.name for path in external.iterdir()) == []


def test_validate_object_store_runtime_staging_refuses_descendant_symlink_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "runtime-child-symlink"
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / run_id / "object-store"
    external = tmp_path / "external-runtime-child"
    external.mkdir()
    external_cfg = external / "alias-a.cfg.para"
    external_cfg.write_text("external must remain\n", encoding="utf-8")
    original_write = object_store_validation.atomic_write_bytes_no_follow
    swapped = False

    def swap_cfg_before_write(
        path: Path,
        content: bytes,
        *,
        containment_root: Path | None = None,
        temp_suffix: str = "tmp",
    ) -> Path:
        nonlocal swapped
        if (
            path
            == lane_dir
            / "runtime-workspace"
            / "runs"
            / f"{run_id}_runtime_staging"
            / "input"
            / "alias-a.cfg.para"
            and not swapped
        ):
            swapped = True
            if path.exists():
                path.unlink()
            path.symlink_to(external_cfg)
        return original_write(path, content, containment_root=containment_root, temp_suffix=temp_suffix)

    monkeypatch.setattr(object_store_validation, "atomic_write_bytes_no_follow", swap_cfg_before_write)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(ProductionObjectStoreConfig.from_env(evidence_root=evidence_root, run_id=run_id))

    assert swapped is True
    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert external_cfg.read_text(encoding="utf-8") == "external must remain\n"


def test_validate_object_store_runtime_staging_refuses_package_subdir_symlink_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "runtime-subdir-symlink"
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / run_id / "object-store"
    external = tmp_path / "external-runtime-subdir"
    external.mkdir()
    original_write = object_store_validation.atomic_write_bytes_no_follow
    swapped = False

    def swap_gis_before_write(
        path: Path,
        content: bytes,
        *,
        containment_root: Path | None = None,
        temp_suffix: str = "tmp",
    ) -> Path:
        nonlocal swapped
        gis_dir = (
            lane_dir
            / "runtime-workspace"
            / "runs"
            / f"{run_id}_runtime_staging"
            / "input"
            / "gis"
        )
        if path.parent == gis_dir and not swapped:
            swapped = True
            if gis_dir.exists():
                safe_fs.rmtree_no_follow(gis_dir, containment_root=lane_dir)
            gis_dir.symlink_to(external, target_is_directory=True)
        return original_write(path, content, containment_root=containment_root, temp_suffix=temp_suffix)

    monkeypatch.setattr(object_store_validation, "atomic_write_bytes_no_follow", swap_gis_before_write)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(ProductionObjectStoreConfig.from_env(evidence_root=evidence_root, run_id=run_id))

    assert swapped is True
    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
    assert sorted(path.name for path in external.iterdir()) == []


def test_validate_object_store_refuses_external_local_store_descendant_symlink_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_root = tmp_path / "artifacts"
    run_id = "external-store-symlink"
    object_root = tmp_path / "external-object-store"
    target_prefix = object_root / "models" / "existing"
    symlink_parent = object_root / "runs"
    target_prefix.mkdir(parents=True)
    symlink_parent.mkdir()
    sentinel = target_prefix / "sentinel.txt"
    sentinel.write_text("external must remain\n", encoding="utf-8")
    (symlink_parent / run_id).symlink_to(target_prefix, target_is_directory=True)
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", f"s3://nhms-prod/runs/{run_id}")

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(
            ProductionObjectStoreConfig.from_env(
                evidence_root=evidence_root,
                run_id=run_id,
                force=True,
            )
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK"
    assert sentinel.read_text(encoding="utf-8") == "external must remain\n"
    assert sorted(path.name for path in target_prefix.iterdir()) == ["sentinel.txt"]


def test_validate_object_store_does_not_recursively_scan_unrelated_external_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_root = tmp_path / "artifacts"
    run_id = "broad-external-root"
    object_root = tmp_path / "external-object-store"
    unrelated_target = tmp_path / "unrelated-target"
    unrelated_target.mkdir()
    (object_root / "unrelated" / "deep").mkdir(parents=True)
    (object_root / "unrelated" / "deep" / "link").symlink_to(unrelated_target, target_is_directory=True)
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX", f"s3://nhms-prod/runs/{run_id}")

    config = ProductionObjectStoreConfig.from_env(evidence_root=evidence_root, run_id=run_id, force=True)
    object_store_validation._validate_internal_lane_paths(config)

    assert (object_root / "unrelated" / "deep" / "link").is_symlink()
    assert not (object_root / "runs").exists()


def test_validate_object_store_refuses_nested_local_store_symlink_without_external_write(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "artifacts"
    lane_dir = evidence_root / "nested-store-symlink" / "object-store"
    store_dir = lane_dir / "local-object-store"
    external_dir = tmp_path / "external-store-target"
    external_dir.mkdir()
    external_file = external_dir / "sentinel.txt"
    external_file.write_text("external must remain\n", encoding="utf-8")
    store_dir.mkdir(parents=True)
    (store_dir / "runs").symlink_to(external_dir, target_is_directory=True)

    with pytest.raises(ProductionObjectStoreValidationError) as exc_info:
        validate_object_store(
            ProductionObjectStoreConfig.from_env(
                evidence_root=evidence_root,
                run_id="nested-store-symlink",
                force=True,
            )
        )

    assert exc_info.value.error_code == "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK"
    assert external_file.read_text(encoding="utf-8") == "external must remain\n"
    assert sorted(path.name for path in external_dir.iterdir()) == ["sentinel.txt"]
