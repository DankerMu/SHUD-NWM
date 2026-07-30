"""Scoped dual-lane state-index reset (#1164 change 2, tasks 2.1-2.3)."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import replay_state_scope_reset as reset_module
from scripts.replay_state_scope_reset import (
    ResetCommitUncertain,
    ResetRefused,
    reset_state_scopes,
)

SCOPE_MODELS = ("dg_dth_ls_gfs", "dg_hhe_gfs")
OUT_OF_SCOPE_MODEL = "dg_other_basin_gfs"
LEGACY_MODEL = "basins_dth_ls_shud"

SCHEMA = json.loads(Path("schemas/replay_state_scope_reset_receipt.schema.json").read_text(encoding="utf-8"))


def _entry(model_id: str, source_id: str, *, valid_time: str, state_id: str) -> dict[str, Any]:
    return {
        "state_id": state_id,
        "model_id": model_id,
        "run_id": f"fcst_{source_id.lower()}_2026070600_{model_id}",
        "source_id": source_id,
        "valid_time": valid_time,
        "state_uri": f"s3://nhms/states/{model_id}/{state_id}/state.cfg.ic",
        "checksum": f"sha256:{'0' * 64}",
        "usable_flag": True,
        "created_at": "2026-07-06T12:00:00Z",
    }


def _write_index(path: Path, entries: list[dict[str, Any]]) -> bytes:
    payload: dict[str, Any] = {
        "schema_version": "nhms.scheduler.file_state_snapshot_index.v1",
        "generated_at": "2026-07-21T00:00:00Z",
        "entries": entries,
    }
    payload["checksum"] = f"sha256:{reset_module.index_payload_checksum(payload)}"
    content = reset_module.canonical_index_bytes(payload, pretty=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def _fixture(tmp_path: Path) -> dict[str, Any]:
    object_root = tmp_path / "object-store"
    entries: list[dict[str, Any]] = []
    for model_id in SCOPE_MODELS:
        for index, valid_time in enumerate(("2026-07-06T12:00:00Z", "2026-07-07T00:00:00Z")):
            entry = _entry(model_id, "gfs", valid_time=valid_time, state_id=f"{model_id}-{index}")
            entries.append(entry)
            object_path = object_root / "states" / model_id / entry["state_id"] / "state.cfg.ic"
            object_path.parent.mkdir(parents=True, exist_ok=True)
            object_path.write_bytes(f"ic-bytes-{model_id}-{index}".encode())
    entries.append(_entry(SCOPE_MODELS[0], "IFS", valid_time="2026-07-06T12:00:00Z", state_id="ifs-untouched"))
    entries.append(_entry(OUT_OF_SCOPE_MODEL, "gfs", valid_time="2026-07-06T12:00:00Z", state_id="other-untouched"))
    entries.append(_entry(LEGACY_MODEL, "gfs", valid_time="2026-06-01T12:00:00Z", state_id="legacy-untouched"))

    nfs_index = tmp_path / "nfs" / "index-last.json"
    scratch_index = tmp_path / "scratch" / "index-last.json"
    nfs_bytes = _write_index(nfs_index, entries)
    scratch_bytes = _write_index(scratch_index, entries)
    archive_root = tmp_path / "recovery"
    archive_root.mkdir()
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    return {
        "nfs_index": nfs_index,
        "scratch_index": scratch_index,
        "nfs_bytes": nfs_bytes,
        "scratch_bytes": scratch_bytes,
        "archive_root": archive_root,
        "journal_root": journal_root,
        "object_root": object_root,
        "entries": entries,
    }


def _run(fixture: dict[str, Any], *, enforce: bool, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "scopes": [(model_id, "gfs") for model_id in SCOPE_MODELS],
        "enforce": enforce,
        "nfs_index": fixture["nfs_index"],
        "scratch_index": fixture["scratch_index"],
        "archive_root": fixture["archive_root"],
        "journal_root": fixture["journal_root"],
        "object_store_root": fixture["object_root"],
        "object_store_prefix": "s3://nhms",
        "timer_probe": lambda: ("inactive", "inactive"),
    }
    kwargs.update(overrides)
    return reset_state_scopes(**kwargs)


def test_dry_run_performs_zero_writes_and_reports_the_full_plan(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receipt = _run(fixture, enforce=False)

    jsonschema.validate(receipt, SCHEMA)
    assert receipt["outcome"] == "completed"
    assert receipt["enforced"] is False
    assert receipt["archive_dir"] is None
    assert receipt["totals"]["removed_entries"] == 8  # 4 entries x 2 lanes
    assert {lane["lane"] for lane in receipt["lanes"]} == {"scratch", "nfs"}
    assert all(lane["readback_verified"] is None for lane in receipt["lanes"])
    assert fixture["nfs_index"].read_bytes() == fixture["nfs_bytes"]
    assert fixture["scratch_index"].read_bytes() == fixture["scratch_bytes"]
    assert list(fixture["archive_root"].iterdir()) == []
    # Objects are probed read-only, never removed.
    assert receipt["totals"]["objects_present"] == 4
    assert all(item["archived_path"] is None for item in receipt["objects"])


def test_enforce_removes_both_lanes_and_archives_everything(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receipt = _run(fixture, enforce=True)

    jsonschema.validate(receipt, SCHEMA)
    assert receipt["outcome"] == "completed"
    assert receipt["enforced"] is True
    archive_dir = Path(receipt["archive_dir"])
    assert archive_dir.is_dir()

    for lane_receipt in receipt["lanes"]:
        assert lane_receipt["removed_count"] == 4
        assert lane_receipt["entry_count_after"] == lane_receipt["entry_count_before"] - 4
        assert lane_receipt["readback_verified"] is True
        assert Path(lane_receipt["snapshot_path"]).is_file()

    # Lane order is load-bearing: scratch is written before the NFS admission lane.
    assert [lane["lane"] for lane in receipt["lanes"]] == ["scratch", "nfs"]

    for index_path, original in (
        (fixture["nfs_index"], fixture["nfs_bytes"]),
        (fixture["scratch_index"], fixture["scratch_bytes"]),
    ):
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        model_ids = {entry["model_id"] for entry in payload["entries"]}
        assert not (model_ids & set(SCOPE_MODELS) - {SCOPE_MODELS[0]})
        assert index_path.read_bytes() != original

    # Byte archives of every affected state object, plus the removed-entry lists.
    archived = sorted(path.name for path in (archive_dir / "objects").iterdir())
    assert archived == sorted(f"{model_id}-{index}.bin" for model_id in SCOPE_MODELS for index in (0, 1))
    for lane in ("scratch", "nfs"):
        removed = json.loads((archive_dir / f"{lane}-removed-entries.json").read_text(encoding="utf-8"))
        assert len(removed["removed_entries"]) == 4
    assert (archive_dir / "reset-receipt.json").is_file()
    jsonschema.validate(json.loads((archive_dir / "reset-receipt.json").read_text(encoding="utf-8")), SCHEMA)


def test_out_of_scope_and_legacy_entries_stay_byte_identical(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    before = {
        json.dumps(entry, sort_keys=True)
        for entry in fixture["entries"]
        if entry["model_id"] in {OUT_OF_SCOPE_MODEL, LEGACY_MODEL} or entry["source_id"] == "IFS"
    }
    _run(fixture, enforce=True)
    payload = json.loads(fixture["nfs_index"].read_text(encoding="utf-8"))
    after = {json.dumps(entry, sort_keys=True) for entry in payload["entries"]}
    assert before == after
    assert {entry["model_id"] for entry in payload["entries"]} == {
        SCOPE_MODELS[0],  # the IFS-scoped row of the same model survives
        OUT_OF_SCOPE_MODEL,
        LEGACY_MODEL,
    }


def test_state_objects_are_never_deleted(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    objects = sorted(path for path in (fixture["object_root"] / "states").rglob("*.ic"))
    _run(fixture, enforce=True)
    assert sorted(path for path in (fixture["object_root"] / "states").rglob("*.ic")) == objects


def test_absent_and_unreadable_objects_are_recorded_three_way_without_blocking(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    absent = fixture["object_root"] / "states" / SCOPE_MODELS[0] / f"{SCOPE_MODELS[0]}-0" / "state.cfg.ic"
    absent.unlink()
    unreadable_dir = fixture["object_root"] / "states" / SCOPE_MODELS[1] / f"{SCOPE_MODELS[1]}-0"
    os.chmod(unreadable_dir, 0o000)
    try:
        receipt = _run(fixture, enforce=True)
    finally:
        os.chmod(unreadable_dir, 0o700)

    jsonschema.validate(receipt, SCHEMA)
    statuses = {item["state_id"]: item["stat_status"] for item in receipt["objects"]}
    assert statuses[f"{SCOPE_MODELS[0]}-0"] == "absent"
    assert statuses[f"{SCOPE_MODELS[1]}-0"] == "undeterminable"
    # The reset still completed and the entries are gone from both lanes.
    assert receipt["outcome"] == "completed"
    assert all(lane["readback_verified"] is True for lane in receipt["lanes"])
    assert receipt["totals"]["objects_absent"] == 1
    assert receipt["totals"]["objects_undeterminable"] == 1


def test_readback_failure_is_commit_uncertain_not_refused(tmp_path: Path, monkeypatch: Any) -> None:
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(
        reset_module,
        "_verify_lane_readback",
        lambda path, *, expected, scopes: (False, "simulated read-back failure"),
    )
    with pytest.raises(ResetCommitUncertain) as error:
        _run(fixture, enforce=True)
    assert error.value.reason == "lane_readback_failed"
    receipt = error.value.receipt
    jsonschema.validate(receipt, SCHEMA)
    assert receipt["outcome"] == "commit_uncertain"
    assert receipt["commit_uncertain_reason"] == "lane_readback_failed"
    assert error.value.to_dict()["outcome"] != "refused"
    # The scratch lane was written before the verification failed; the receipt
    # says so instead of claiming nothing happened.
    assert fixture["scratch_index"].read_bytes() != fixture["scratch_bytes"]
    # ... and the NFS admission lane was never reached, so the fail-closed
    # direction holds: scratch cleared, NFS still holding the old chain.
    assert fixture["nfs_index"].read_bytes() == fixture["nfs_bytes"]


def test_active_timer_refuses_before_any_write(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(ResetRefused) as error:
        _run(fixture, enforce=True, timer_probe=lambda: ("active", "active"))
    assert error.value.reason == "scheduler_timer_not_provably_inactive"
    assert fixture["nfs_index"].read_bytes() == fixture["nfs_bytes"]
    assert fixture["scratch_index"].read_bytes() == fixture["scratch_bytes"]


def test_undeterminable_timer_probe_counts_as_active(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(ResetRefused) as error:
        _run(fixture, enforce=True, timer_probe=lambda: ("undeterminable", "systemctl missing"))
    assert error.value.reason == "scheduler_timer_not_provably_inactive"
    assert error.value.details["timer"]["verdict"] == "undeterminable"


def test_fresh_journal_lock_refuses(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    lock_dir = fixture["journal_root"] / ".locks"
    lock_dir.mkdir()
    (lock_dir / "scheduler.lock").write_text("{}", encoding="utf-8")
    with pytest.raises(ResetRefused) as error:
        _run(fixture, enforce=True)
    assert error.value.reason == "journal_lock_activity"
    assert fixture["nfs_index"].read_bytes() == fixture["nfs_bytes"]


def test_stale_journal_lock_does_not_refuse(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    lock_dir = fixture["journal_root"] / ".locks"
    lock_dir.mkdir()
    lock_file = lock_dir / "scheduler.lock"
    lock_file.write_text("{}", encoding="utf-8")
    stale = datetime.now(tz=UTC).timestamp() - (reset_module.LOCK_FRESHNESS_SECONDS + 60)
    os.utime(lock_file, (stale, stale))
    receipt = _run(fixture, enforce=False)
    assert receipt["preflight"]["journal_locks"]["status"] == "absent"


def test_unreadable_lane_index_refuses(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["scratch_index"].unlink()
    with pytest.raises(ResetRefused) as error:
        _run(fixture, enforce=True)
    assert error.value.reason == "lane_index_missing"
    assert fixture["nfs_index"].read_bytes() == fixture["nfs_bytes"]


def test_missing_archive_root_refuses(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(ResetRefused) as error:
        _run(fixture, enforce=True, archive_root=tmp_path / "no-such-root")
    assert error.value.reason == "archive_root_unavailable"
    assert fixture["nfs_index"].read_bytes() == fixture["nfs_bytes"]


def test_insufficient_archive_space_refuses(tmp_path: Path, monkeypatch: Any) -> None:
    fixture = _fixture(tmp_path)

    class _Usage:
        total = 1 << 40
        used = 1 << 40
        free = 1

    monkeypatch.setattr(reset_module.shutil, "disk_usage", lambda path: _Usage())
    with pytest.raises(ResetRefused) as error:
        _run(fixture, enforce=True)
    assert error.value.reason == "archive_root_insufficient_space"
    assert fixture["nfs_index"].read_bytes() == fixture["nfs_bytes"]


def test_rewritten_index_stays_readable_by_the_publisher_contract(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _run(fixture, enforce=True)
    payload = json.loads(fixture["nfs_index"].read_text(encoding="utf-8"))
    assert payload["schema_version"] == "nhms.scheduler.file_state_snapshot_index.v1"
    recomputed = reset_module.index_payload_checksum(payload)
    assert payload["checksum"] == f"sha256:{recomputed}"
    # The archived snapshot still hashes to the pre-reset lane bytes.
    archive_dir = tmp_path / "recovery"
    snapshot = next(archive_dir.glob("six-basin-replay-*/nfs-index-before.json"))
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == hashlib.sha256(fixture["nfs_bytes"]).hexdigest()


def test_empty_scope_set_refuses(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(ResetRefused) as error:
        _run(fixture, enforce=True, scopes=[])
    assert error.value.reason == "scopes_empty"


def test_cli_defaults_to_dry_run(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(
        reset_module,
        "default_timer_probe",
        lambda unit=reset_module.TIMER_UNIT: ("inactive", "inactive"),
    )
    exit_code = reset_module.main(
        [
            "--source",
            "gfs",
            *[argument for model_id in SCOPE_MODELS for argument in ("--model-id", model_id)],
            "--nfs-index",
            str(fixture["nfs_index"]),
            "--scratch-index",
            str(fixture["scratch_index"]),
            "--archive-root",
            str(fixture["archive_root"]),
            "--journal-root",
            str(fixture["journal_root"]),
            "--object-store-root",
            str(fixture["object_root"]),
            "--object-store-prefix",
            "s3://nhms",
        ]
    )
    assert exit_code == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["enforced"] is False
    assert fixture["nfs_index"].read_bytes() == fixture["nfs_bytes"]
