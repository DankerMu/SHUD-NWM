from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from packages.common.object_store import LocalObjectStore
from services.production_closure import object_store_validation, slurm_validation
from services.production_closure.object_store_validation import (
    ProductionObjectStoreConfig,
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
