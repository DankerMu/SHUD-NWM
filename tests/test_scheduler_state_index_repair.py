from __future__ import annotations

import json
import os
import stat
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from packages.common import provider_atomic as provider_atomic_module
from packages.common import state_manager as state_manager_module
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.provider_atomic import ProviderAtomicError
from packages.common.safe_fs import SafeFilesystemError
from packages.common.state_manager import (
    FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
    merge_state_snapshot_index_copyback,
    publish_state_snapshot_index,
)
from scripts import scheduler_state_index_repair as repair
from tests.provider_mode_helpers import write_provider_destination
from tests.test_state_manager import _LockReleaseSeam

PREFIX = "s3://nhms"
SHARED_RUN = "fcst_gfs_2026072000_model_a"
PRIVATE_RUN = "fcst_gfs_2026071900_model_a"
DEST_ONLY_RUN = "fcst_gfs_2026070500_model_a"


def test_checksum_invalid_destination_dry_run_changes_no_filesystem_bytes(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture.corrupt_destination_checksum()
    _apply_env(monkeypatch, fixture)
    before = _fs_fingerprint(fixture.root)

    exit_code = repair.main(["recompute-checksum", "--lane", "destination"])

    captured = capsys.readouterr()
    preview = json.loads(captured.out.strip().splitlines()[-1])
    assert exit_code == 0
    assert preview["mode"] == "dry_run"
    assert preview["lanes"]["destination"]["checksum_valid"] is False
    assert preview["lanes"]["destination"]["action"] == "recompute-checksum"
    assert preview["lanes"]["reference"]["action"] == "skip"
    assert preview["lanes"]["reference"]["untouched_reason"] == "validated_read_only_sibling"
    assert preview["lanes"]["destination"]["entry_count_before"] == 2
    assert preview["lanes"]["destination"]["entry_count_after"] == 2
    assert _fs_fingerprint(fixture.root) == before
    assert not (fixture.archive_root / "latest.json").exists()
    assert not (fixture.receipt_root / "latest.json").exists()


def test_enforce_checksum_only_archives_target_and_leaves_sibling_bytes_unchanged(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_destination = json.loads(fixture.destination_index.read_text(encoding="utf-8"))
    original_entries = original_destination["entries"]
    reference_before = fixture.reference_index.read_bytes()
    fixture.corrupt_destination_checksum()
    destination_before = fixture.destination_index.read_bytes()
    _apply_env(monkeypatch, fixture)

    exit_code = repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    repaired = json.loads(fixture.destination_index.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert receipt["lanes"]["destination"]["action"] == "recompute-checksum"
    assert receipt["lanes"]["reference"]["action"] == "skip"
    assert receipt["lanes"]["reference"]["untouched_reason"] == "validated_read_only_sibling"
    assert repaired["entries"] == original_entries
    assert repaired["checksum"] == f"sha256:{_independent_payload_checksum(repaired)}"
    assert fixture.reference_index.read_bytes() == reference_before
    archives = list(fixture.archive_root.glob("*-destination.json"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == destination_before
    assert stat.S_IMODE(archives[0].stat().st_mode) == 0o600
    assert list(fixture.archive_root.glob("*-reference.json")) == []
    _assert_production_index_readable(fixture.destination_index, fixture.destination_root, "shared-state")


@pytest.mark.parametrize(
    "argv",
    [
        ["remove-entry", "--state-id", "shared-state"],
        ["remove-entry", "--run-id", SHARED_RUN],
        [
            "remove-entry",
            "--model-id",
            "model_a",
            "--source-id",
            "gfs",
            "--valid-time",
            "2026-07-20T12:00:00Z",
        ],
    ],
    ids=["state_id", "run_id", "base_key"],
)
def test_unique_removal_preserves_unrelated_entries_independently_in_each_lane(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    reference_before = json.loads(fixture.reference_index.read_text(encoding="utf-8"))["entries"]
    destination_before = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    reference_bytes = fixture.reference_index.read_bytes()
    destination_bytes = fixture.destination_index.read_bytes()
    _apply_env(monkeypatch, fixture)

    exit_code = repair.main([*argv, "--enforce"])

    reference_after = json.loads(fixture.reference_index.read_text(encoding="utf-8"))["entries"]
    destination_after = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert [entry["state_id"] for entry in reference_after] == ["private-state"]
    assert [entry["state_id"] for entry in destination_after] == ["destination-state"]
    assert reference_after == [entry for entry in reference_before if entry["state_id"] != "shared-state"]
    assert destination_after == [entry for entry in destination_before if entry["state_id"] != "shared-state"]
    assert receipt["lanes"]["reference"]["action"] == "remove-entry"
    assert receipt["lanes"]["destination"]["action"] == "remove-entry"
    assert receipt["lanes"]["reference"]["state_id"] == "shared-state"
    assert receipt["lanes"]["destination"]["state_id"] == "shared-state"
    archives = {
        path.name.split("-")[-1]: path
        for path in fixture.archive_root.glob("*.json")
        if path.name != "latest.json"
    }
    assert archives["reference.json"].read_bytes() == reference_bytes
    assert archives["destination.json"].read_bytes() == destination_bytes
    _assert_production_index_readable(fixture.reference_index, fixture.reference_root, "private-state")
    _assert_production_index_readable(fixture.destination_index, fixture.destination_root, "destination-state")


def test_zero_multiple_and_cross_lane_mismatch_refuse_without_archive_or_index_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    cases = [
        (["remove-entry", "--state-id", "missing-state"], "repair_selector_absent"),
        (["remove-entry", "--run-id", SHARED_RUN], "repair_selector_ambiguous"),
        (["remove-entry", "--state-id", "shared-state"], "repair_selector_identity_mismatch"),
    ]
    for argv, expected in cases:
        local = RepairFixture(tmp_path / expected)
        if expected == "repair_selector_ambiguous":
            extra_same_run = _entry(
                state_id="shared-duplicate",
                run_id=SHARED_RUN,
                state_uri=local.shared_entry["state_uri"],
                content=local.shared_content,
                valid_time="2026-07-21T12:00:00Z",
                created_at="2026-07-21T13:00:00Z",
                cycle_id="gfs_2026072100",
            )
            _republish(
                local,
                reference_entries=[local.private_entry, local.shared_entry, extra_same_run],
                destination_entries=[local.destination_only_entry, local.shared_entry, extra_same_run],
            )
        if expected == "repair_selector_identity_mismatch":
            mismatched = dict(local.shared_entry)
            mismatched["state_id"] = "other-shared-state"
            _republish(local, destination_entries=[local.destination_only_entry, mismatched])
            argv = ["remove-entry", "--run-id", SHARED_RUN]
        _apply_env(monkeypatch, local)
        reference_before = local.reference_index.read_bytes()
        destination_before = local.destination_index.read_bytes()
        exit_code = repair.main([*argv, "--enforce"])
        error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert exit_code == 2
        assert error["status"] == "refused"
        assert error["reason"] == expected
        assert local.reference_index.read_bytes() == reference_before
        assert local.destination_index.read_bytes() == destination_before
        assert list(local.archive_root.iterdir()) == []
        assert not (local.receipt_root / "latest.json").exists()


@pytest.mark.parametrize(
    ("mutator", "expected_reason"),
    [
        (
            lambda path: write_provider_destination(path, "{not-json"),
            "state_snapshot_index_malformed_json",
        ),
        (
            lambda path: _mutate_payload(path, lambda payload: payload.__setitem__("schema_version", "v0")),
            "state_snapshot_index_schema_unsupported",
        ),
        (
            lambda path: _mutate_payload(
                path,
                lambda payload: payload["entries"][0].__setitem__(
                    "state_uri",
                    payload["entries"][0]["state_uri"] + "?x=1",
                ),
            ),
            "state_snapshot_index_object_unsafe_uri",
        ),
    ],
    ids=["malformed", "schema", "unsafe_uri"],
)
def test_structural_corruption_refuses_before_archive_or_mutation(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutator: Any,
    expected_reason: str,
) -> None:
    mutator(fixture.destination_index)
    _apply_env(monkeypatch, fixture)
    reference_before = fixture.reference_index.read_bytes()
    destination_before = fixture.destination_index.read_bytes()

    exit_code = repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])

    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == expected_reason
    assert fixture.reference_index.read_bytes() == reference_before
    assert fixture.destination_index.read_bytes() == destination_before
    assert list(fixture.archive_root.iterdir()) == []
    assert not (fixture.receipt_root / "latest.json").exists()


def test_entry_limit_and_non_unique_base_key_refuse_without_writes(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    same_base = _entry(
        state_id="shared-other-lead",
        run_id="fcst_gfs_2026072000_model_a_alt",
        state_uri=fixture.shared_entry["state_uri"],
        content=fixture.shared_content,
        valid_time="2026-07-20T12:00:00Z",
        created_at="2026-07-20T14:00:00Z",
        cycle_id="gfs_2026072006",
        lead_hours=6,
    )
    _republish(
        fixture,
        reference_entries=[fixture.private_entry, fixture.shared_entry, same_base],
        destination_entries=[fixture.destination_only_entry, fixture.shared_entry, same_base],
    )
    _apply_env(monkeypatch, fixture)
    reference_before = fixture.reference_index.read_bytes()
    destination_before = fixture.destination_index.read_bytes()

    exit_code = repair.main(
        [
            "remove-entry",
            "--model-id",
            "model_a",
            "--source-id",
            "gfs",
            "--valid-time",
            "2026-07-20T12:00:00Z",
            "--enforce",
        ]
    )
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == "repair_selector_ambiguous"
    assert fixture.reference_index.read_bytes() == reference_before
    assert fixture.destination_index.read_bytes() == destination_before

    monkeypatch.setattr(state_manager_module, "MAX_STATE_SNAPSHOT_INDEX_ENTRIES", 0)
    exit_code = repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == "state_snapshot_index_entry_limit_exceeded"
    assert fixture.reference_index.read_bytes() == reference_before
    assert list(fixture.archive_root.iterdir()) == []
    assert not (fixture.receipt_root / "latest.json").exists()


def test_explicit_missing_destination_opt_in_leaves_absent_lane_byte_identical(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _republish(fixture, destination_entries=[fixture.destination_only_entry])
    destination_before = fixture.destination_index.read_bytes()
    reference_before_entries = json.loads(fixture.reference_index.read_text(encoding="utf-8"))["entries"]
    _apply_env(monkeypatch, fixture)

    dry_exit = repair.main(["remove-entry", "--state-id", "shared-state", "--allow-missing-destination"])
    enforce_exit = repair.main(
        ["remove-entry", "--state-id", "shared-state", "--allow-missing-destination", "--enforce"]
    )

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    reference_after = json.loads(fixture.reference_index.read_text(encoding="utf-8"))["entries"]
    assert dry_exit == 0
    assert enforce_exit == 0
    assert fixture.destination_index.read_bytes() == destination_before
    assert reference_after == [entry for entry in reference_before_entries if entry["state_id"] != "shared-state"]
    assert receipt["lanes"]["destination"]["action"] == "skip"
    assert receipt["lanes"]["destination"]["untouched_reason"] == "explicit_missing_lane"
    assert receipt["lanes"]["reference"]["action"] == "remove-entry"
    assert list(fixture.archive_root.glob("*-destination.json")) == []
    assert list(fixture.archive_root.glob("*-reference.json"))


def test_missing_lane_without_opt_in_is_a_refusal(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _republish(fixture, destination_entries=[fixture.destination_only_entry])
    _apply_env(monkeypatch, fixture)
    destination_before = fixture.destination_index.read_bytes()
    reference_before = fixture.reference_index.read_bytes()

    exit_code = repair.main(["remove-entry", "--state-id", "shared-state", "--enforce"])

    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == "repair_selector_absent"
    assert fixture.destination_index.read_bytes() == destination_before
    assert fixture.reference_index.read_bytes() == reference_before
    assert list(fixture.archive_root.iterdir()) == []


def test_private_root_and_archive_no_clobber_guards(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture.corrupt_destination_checksum()
    _apply_env(monkeypatch, fixture)
    fixture.archive_root.chmod(0o755)
    destination_before = fixture.destination_index.read_bytes()

    exit_code = repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == "archive_root_not_private"
    assert fixture.destination_index.read_bytes() == destination_before

    fixture.archive_root.chmod(0o700)
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 7, 28, 12, 0, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(state_manager_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(state_manager_module.uuid, "uuid4", lambda: SimpleNamespace(hex="fixedarchive"))
    blocker = fixture.archive_root / "20260728T120000Z-fixedarchive-destination.json"
    blocker.write_bytes(b"keep-me")
    os.chmod(blocker, 0o600)
    exit_code = repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == "repair_archive_exists"
    assert blocker.read_bytes() == b"keep-me"
    assert fixture.destination_index.read_bytes() == destination_before


def test_symlink_archive_root_and_non_directory_receipt_root_refuse(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture.corrupt_destination_checksum()
    linked = fixture.root / "linked-archives"
    linked.symlink_to(fixture.archive_root)
    monkeypatch.setenv(repair.ARCHIVE_ROOT_ENV, str(linked))
    monkeypatch.setenv(repair.RECEIPT_ROOT_ENV, str(fixture.receipt_root))
    monkeypatch.setenv(repair.REFERENCE_ROOT_ENV, str(fixture.reference_root))
    monkeypatch.setenv(repair.DESTINATION_ROOT_ENV, str(fixture.destination_root))
    monkeypatch.setenv(repair.OBJECT_STORE_PREFIX_ENV, PREFIX)
    destination_before = fixture.destination_index.read_bytes()

    exit_code = repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == "archive_root_invalid"
    assert fixture.destination_index.read_bytes() == destination_before

    receipt_file = fixture.root / "receipt-file"
    receipt_file.write_text("nope", encoding="utf-8")
    monkeypatch.setenv(repair.ARCHIVE_ROOT_ENV, str(fixture.archive_root))
    monkeypatch.setenv(repair.RECEIPT_ROOT_ENV, str(receipt_file))
    exit_code = repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == "receipt_root_invalid"
    assert fixture.destination_index.read_bytes() == destination_before


def test_required_archives_exist_before_first_cas(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_before = fixture.reference_index.read_bytes()
    destination_before = fixture.destination_index.read_bytes()
    _apply_env(monkeypatch, fixture)
    real_publish = state_manager_module.publish_state_snapshot_index
    seen: list[str] = []

    def publish_after_archives(*args: Any, **kwargs: Any) -> Any:
        names = sorted(path.name for path in fixture.archive_root.glob("*.json"))
        assert any(name.endswith("-reference.json") for name in names)
        assert any(name.endswith("-destination.json") for name in names)
        if not seen:
            assert fixture.reference_index.read_bytes() == reference_before
            assert fixture.destination_index.read_bytes() == destination_before
        seen.append("cas")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(state_manager_module, "publish_state_snapshot_index", publish_after_archives)
    exit_code = repair.main(["remove-entry", "--state-id", "shared-state", "--enforce"])
    assert exit_code == 0
    assert seen == ["cas", "cas"]


def test_injected_archive_and_first_cas_precommit_failures_are_zero_write_refusals(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    reference_before = fixture.reference_index.read_bytes()
    destination_before = fixture.destination_index.read_bytes()
    real_write = state_manager_module.atomic_write_bytes_no_follow

    def fail_destination_archive(path: Path, content: bytes, **kwargs: Any) -> Any:
        if str(path).endswith("-destination.json"):
            raise OSError("archive volume failed")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(state_manager_module, "atomic_write_bytes_no_follow", fail_destination_archive)
    exit_code = repair.main(["remove-entry", "--state-id", "shared-state", "--enforce"])
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == "repair_archive_failed"
    assert fixture.reference_index.read_bytes() == reference_before
    assert fixture.destination_index.read_bytes() == destination_before
    assert not (fixture.receipt_root / "latest.json").exists()

    monkeypatch.setattr(state_manager_module, "atomic_write_bytes_no_follow", real_write)
    real_replace = state_manager_module.atomic_replace_provider_bytes

    def fail_first_cas(path: Path, content: bytes, **kwargs: Any) -> Any:
        if Path(path) == fixture.reference_index:
            raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        return real_replace(path, content, **kwargs)

    monkeypatch.setattr(state_manager_module, "atomic_replace_provider_bytes", fail_first_cas)
    exit_code = repair.main(["remove-entry", "--state-id", "shared-state", "--enforce"])
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["reason"] == "provider_preimage_changed"
    assert fixture.reference_index.read_bytes() == reference_before
    assert fixture.destination_index.read_bytes() == destination_before


def test_second_cas_precommit_after_reference_commit_is_exit_3(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    destination_before = fixture.destination_index.read_bytes()
    real_replace = state_manager_module.atomic_replace_provider_bytes

    def fail_destination_cas(path: Path, content: bytes, **kwargs: Any) -> Any:
        if Path(path) == fixture.destination_index:
            raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        return real_replace(path, content, **kwargs)

    monkeypatch.setattr(state_manager_module, "atomic_replace_provider_bytes", fail_destination_cas)
    exit_code = repair.main(["remove-entry", "--state-id", "shared-state", "--enforce"])
    captured = capsys.readouterr()
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    reference_after = json.loads(fixture.reference_index.read_text(encoding="utf-8"))["entries"]
    assert exit_code == 3
    assert error["status"] == "repair_committed_incomplete"
    assert error["status"] != "refused"
    assert summary["lanes"]["reference"]["committed"] is True
    assert [entry["state_id"] for entry in reference_after] == ["private-state"]
    assert fixture.destination_index.read_bytes() == destination_before


def test_replace_uncertain_and_lock_release_and_readback_are_exit_3(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    real_write = provider_atomic_module.atomic_write_bytes_no_follow

    def fail_destination_fsync(path: Path, content: bytes, **kwargs: Any) -> Any:
        written = real_write(path, content, **kwargs)
        if Path(path) == fixture.destination_index:
            raise SafeFilesystemError(
                f"Atomic replacement for {path} completed but directory fsync failed",
                kind="indeterminate",
            )
        return written

    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", fail_destination_fsync)
    exit_code = repair.main(["remove-entry", "--state-id", "shared-state", "--enforce"])
    captured = capsys.readouterr()
    error = json.loads(captured.err.strip().splitlines()[-1])
    summary = json.loads(captured.out.strip().splitlines()[-1])
    assert exit_code == 3
    assert error["reason"] == "provider_replace_uncertain"
    assert summary["mutation_started"] is True
    assert "shared-state" not in [
        entry["state_id"]
        for entry in json.loads(fixture.reference_index.read_text(encoding="utf-8"))["entries"]
    ]


def test_lock_release_failure_after_commit_is_exit_3(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    seam = _LockReleaseSeam(fixture.destination_index)
    monkeypatch.setattr(provider_atomic_module, "fcntl", seam)

    exit_code = repair.main(["remove-entry", "--state-id", "shared-state", "--enforce"])

    captured = capsys.readouterr()
    error = json.loads(captured.err.strip().splitlines()[-1])
    summary = json.loads(captured.out.strip().splitlines()[-1])
    assert exit_code == 3
    assert error["reason"] == "provider_lock_release_failed"
    assert summary["lanes"]["reference"]["committed"] is True
    assert summary["lanes"]["destination"]["committed"] is True
    reference_ids = [
        entry["state_id"]
        for entry in json.loads(fixture.reference_index.read_text(encoding="utf-8"))["entries"]
    ]
    destination_ids = [
        entry["state_id"]
        for entry in json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    ]
    assert reference_ids == ["private-state"]
    assert destination_ids == ["destination-state"]


def test_destination_readback_failure_after_cas_is_exit_3(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture.corrupt_destination_checksum()
    _apply_env(monkeypatch, fixture)
    real_read = state_manager_module.read_provider_snapshot
    dest_reads = {"count": 0}

    def fail_second_destination_read(path: Path, **kwargs: Any) -> Any:
        result = real_read(path, **kwargs)
        if Path(path) == fixture.destination_index:
            dest_reads["count"] += 1
            if dest_reads["count"] >= 2:
                raise ProviderAtomicError("provider_destination_unreadable", phase="precommit")
        return result

    monkeypatch.setattr(state_manager_module, "read_provider_snapshot", fail_second_destination_read)
    exit_code = repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])
    captured = capsys.readouterr()
    error = json.loads(captured.err.strip().splitlines()[-1])
    summary = json.loads(captured.out.strip().splitlines()[-1])
    assert exit_code == 3
    assert error["status"] != "refused"
    assert summary["mutation_started"] is True
    repaired = json.loads(fixture.destination_index.read_text(encoding="utf-8"))
    assert repaired["checksum"] == f"sha256:{_independent_payload_checksum(repaired)}"


def test_receipt_failure_after_successful_repair_is_exit_3(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture.corrupt_destination_checksum()
    _apply_env(monkeypatch, fixture)
    real_write = repair.atomic_write_bytes_no_follow

    def fail_receipt(path: Path, content: bytes, **kwargs: Any) -> Any:
        if Path(path).parent == fixture.receipt_root:
            raise OSError("receipt volume is read-only")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(repair, "atomic_write_bytes_no_follow", fail_receipt)
    exit_code = repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])
    captured = capsys.readouterr()
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert exit_code == 3
    assert error["reason"] == "receipt_write_failed_after_repair"
    assert error["status"] != "refused"
    assert summary["lanes"]["destination"]["committed"] is True
    assert not (fixture.receipt_root / "latest.json").exists()
    repaired = json.loads(fixture.destination_index.read_text(encoding="utf-8"))
    assert repaired["checksum"] == f"sha256:{_independent_payload_checksum(repaired)}"


def test_repair_and_copyback_share_lock_order_and_do_not_deadlock(
    fixture: RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)
    requested: dict[int, list[str]] = {}
    real_lock = state_manager_module.provider_destination_lock

    @contextmanager
    def recording(path: Path, **kwargs: Any) -> Iterator[None]:
        requested.setdefault(threading.get_ident(), []).append(str(path))
        with real_lock(path, **kwargs):
            yield

    monkeypatch.setattr(state_manager_module, "provider_destination_lock", recording)
    fixture.reference_index.parent.chmod(0o700)
    fixture.destination_index.parent.chmod(0o700)

    def run_repair() -> int:
        return repair.main(["recompute-checksum", "--lane", "destination", "--enforce"])

    def run_copyback() -> dict[str, Any]:
        return merge_state_snapshot_index_copyback(
            source_path=fixture.reference_index,
            destination_path=fixture.destination_index,
            reference_object_store_root=fixture.reference_root,
            object_store_prefix=PREFIX,
            source_containment_root=fixture.reference_root,
            destination_containment_root=fixture.destination_root,
            authoritative_run_ids=[PRIVATE_RUN],
        )

    repair_outcome: dict[str, Any] = {}
    copyback_outcome: dict[str, Any] = {}

    def repair_target() -> None:
        try:
            repair_outcome["value"] = run_repair()
        except BaseException as error:  # noqa: BLE001
            repair_outcome["error"] = error

    def copyback_target() -> None:
        try:
            copyback_outcome["value"] = run_copyback()
        except BaseException as error:  # noqa: BLE001
            copyback_outcome["error"] = error

    first = threading.Thread(target=repair_target, daemon=True)
    second = threading.Thread(target=copyback_target, daemon=True)
    first.start()
    second.start()
    first.join(5.0)
    second.join(5.0)
    assert not first.is_alive() and not second.is_alive()
    if "error" in copyback_outcome:
        assert getattr(copyback_outcome["error"], "reason", "") in {
            "provider_preimage_changed",
            "state_snapshot_index_copyback_conflict",
        }
    if "error" in repair_outcome:
        assert getattr(repair_outcome["error"], "reason", "") == "provider_preimage_changed"
    else:
        assert repair_outcome["value"] in {0, 2, 3}
    assert requested
    for order in requested.values():
        assert order
        assert order[0] == str(fixture.reference_index)
        if len(order) > 1:
            assert order[1] == str(fixture.destination_index)
    destination = json.loads(fixture.destination_index.read_text(encoding="utf-8"))
    assert destination["checksum"] == f"sha256:{_independent_payload_checksum(destination)}"


class RepairFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.reference_root = root / "object-store"
        self.destination_root = root / "shared-object-store"
        self.archive_root = root / "archives"
        self.receipt_root = root / "receipts"
        self.archive_root.mkdir(parents=True)
        self.receipt_root.mkdir(parents=True)
        os.chmod(self.archive_root, 0o700)
        os.chmod(self.receipt_root, 0o700)
        store = LocalObjectStore(self.reference_root, PREFIX)
        self.shared_content = _valid_state_bytes(b"shared")
        self.private_content = _valid_state_bytes(b"private")
        self.destination_content = _valid_state_bytes(b"destination")
        shared_uri = store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", self.shared_content)
        private_uri = store.write_bytes_atomic("states/gfs/model_a/private/state.cfg.ic", self.private_content)
        destination_uri = store.write_bytes_atomic(
            "states/gfs/model_a/destination/state.cfg.ic", self.destination_content
        )
        self.shared_entry = _entry(
            state_id="shared-state",
            run_id=SHARED_RUN,
            state_uri=shared_uri,
            content=self.shared_content,
            valid_time="2026-07-20T12:00:00Z",
            created_at="2026-07-20T13:00:00Z",
            cycle_id="gfs_2026072000",
        )
        self.private_entry = _entry(
            state_id="private-state",
            run_id=PRIVATE_RUN,
            state_uri=private_uri,
            content=self.private_content,
            valid_time="2026-07-19T12:00:00Z",
            created_at="2026-07-19T13:00:00Z",
            cycle_id="gfs_2026071900",
        )
        self.destination_only_entry = _entry(
            state_id="destination-state",
            run_id=DEST_ONLY_RUN,
            state_uri=destination_uri,
            content=self.destination_content,
            valid_time="2026-07-05T12:00:00Z",
            created_at="2026-07-05T13:00:00Z",
            cycle_id="gfs_2026070500",
        )
        self.reference_index = self.reference_root / "scheduler/state-index/index-last.json"
        self.destination_index = self.destination_root / "scheduler/state-index/index-last.json"
        _republish(
            self,
            reference_entries=[self.private_entry, self.shared_entry],
            destination_entries=[self.destination_only_entry, self.shared_entry],
        )

    def corrupt_destination_checksum(self) -> bytes:
        before = self.destination_index.read_bytes()
        payload = json.loads(before.decode("utf-8"))
        payload["checksum"] = "sha256:deadbeef"
        write_provider_destination(
            self.destination_index,
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
        )
        return before


@pytest.fixture(name="fixture")
def fixture_factory(tmp_path: Path) -> RepairFixture:
    return RepairFixture(tmp_path)


def _apply_env(monkeypatch: pytest.MonkeyPatch, fixture: RepairFixture) -> None:
    monkeypatch.setenv(repair.REFERENCE_ROOT_ENV, str(fixture.reference_root))
    monkeypatch.setenv(repair.DESTINATION_ROOT_ENV, str(fixture.destination_root))
    monkeypatch.setenv(repair.OBJECT_STORE_PREFIX_ENV, PREFIX)
    monkeypatch.setenv(repair.ARCHIVE_ROOT_ENV, str(fixture.archive_root))
    monkeypatch.setenv(repair.RECEIPT_ROOT_ENV, str(fixture.receipt_root))


def _republish(
    fixture: RepairFixture,
    *,
    reference_entries: list[dict[str, Any]] | None = None,
    destination_entries: list[dict[str, Any]] | None = None,
) -> None:
    if reference_entries is not None:
        publish_state_snapshot_index(
            reference_entries,
            fixture.reference_index,
            object_store_root=fixture.reference_root,
            object_store_prefix=PREFIX,
            generated_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
        )
    if destination_entries is not None:
        publish_state_snapshot_index(
            destination_entries,
            fixture.destination_index,
            object_store_root=fixture.destination_root,
            object_store_prefix=PREFIX,
            generated_at=datetime(2026, 7, 25, 18, tzinfo=UTC),
            verify_objects=False,
        )


def _entry(
    *,
    state_id: str,
    run_id: str,
    state_uri: str,
    content: bytes,
    valid_time: str,
    created_at: str,
    cycle_id: str,
    lead_hours: int = 12,
) -> dict[str, Any]:
    return {
        "state_id": state_id,
        "model_id": "model_a",
        "run_id": run_id,
        "source_id": "gfs",
        "valid_time": valid_time,
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "created_at": created_at,
        "cycle_id": cycle_id,
        "lead_hours": lead_hours,
    }


def _valid_state_bytes(seed: bytes) -> bytes:
    minute = 27_000_000.0 + (int.from_bytes(seed[:4].ljust(4, b"\x00"), "big") % 1000)
    return (
        f"2\t1\t{minute:.6f}\n"
        "1\t0.1\t0.1\t0.1\t0.1\t0.1\n"
        "2\t0.1\t0.1\t0.1\t0.1\t0.1\n"
        "1\t0.5\n"
    ).encode()


def _independent_payload_checksum(payload: dict[str, Any]) -> str:
    content = json.dumps(
        {key: value for key, value in payload.items() if key != "checksum"},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256_bytes(content)


def _assert_production_index_readable(index_path: Path, root: Path, state_id: str) -> None:
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION
    assert payload["checksum"] == f"sha256:{_independent_payload_checksum(payload)}"
    entries = state_manager_module._validate_state_snapshot_index(
        payload,
        object_store_root=root,
        object_store_prefix=PREFIX,
        published_artifact_root=None,
        now=datetime.fromisoformat(str(payload["generated_at"]).replace("Z", "+00:00")),
        max_age_hours=168,
        verify_objects=False,
        enforce_freshness=False,
    )
    assert any(entry.get("state_id") == state_id for entry in entries.values())


def _fs_fingerprint(root: Path) -> dict[str, tuple[Any, ...]]:
    fingerprint: dict[str, tuple[Any, ...]] = {}
    for path in sorted(root.rglob("*"), key=str):
        rel = str(path.relative_to(root))
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            fingerprint[rel] = ("lnk", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            fingerprint[rel] = ("dir", stat.S_IMODE(info.st_mode))
        elif stat.S_ISREG(info.st_mode):
            fingerprint[rel] = ("reg", path.read_bytes(), stat.S_IMODE(info.st_mode))
        else:
            fingerprint[rel] = ("other", info.st_mode)
    return fingerprint


def _mutate_payload(path: Path, mutator: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutator(payload)
    payload["checksum"] = f"sha256:{_independent_payload_checksum(payload)}"
    write_provider_destination(path, json.dumps(payload, sort_keys=True, indent=2) + "\n")
