from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.provider_atomic import ProviderAtomicError
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_state_index_copyback_replay as replay

PREFIX = "s3://nhms"
AUTHORITATIVE_RUN = "fcst_gfs_2026072000_model_a"
IFS_RUN = "fcst_ifs_2026072000_model_a"
HISTORICAL_RUN = "fcst_gfs_2026070500_model_a"


def test_replay_dry_run_previews_without_touching_index_or_objects(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)
    index_before = fixture.destination_index.read_bytes()

    exit_code = replay.main(["--cycle", "GFS_2026072000"])

    assert exit_code == 0
    assert fixture.destination_index.read_bytes() == index_before
    assert not fixture.new_shared_object.exists()
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["schema_version"] == replay.SCHEMA_VERSION
    assert receipt["mode"] == "dry_run"
    assert receipt["requested_cycles"] == ["gfs_2026072000"]
    assert receipt["resolved_run_ids"] == [AUTHORITATIVE_RUN]
    assert receipt["destination_entry_count_before"] == 1
    assert receipt["destination_entry_count_after"] == 1
    assert receipt["preview_new_state_ids"] == ["fresh-state"]
    assert receipt["checkpoint_copied_count"] is None
    assert receipt["merge"] is None


def test_replay_enforce_publishes_missing_entries_and_is_idempotent(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)

    first = replay.main(["--cycle", "gfs_2026072000", "--enforce"])
    first_receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    entries_after_first = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    second = replay.main(["--cycle", "gfs_2026072000", "--enforce"])
    second_receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))

    assert (first, second) == (0, 0)
    assert first_receipt["mode"] == "enforce"
    assert first_receipt["destination_entry_count_before"] == 1
    assert first_receipt["destination_entry_count_after"] == 2
    assert first_receipt["checkpoint_copied_count"] == 1
    assert first_receipt["checkpoint_reused_count"] == 0
    assert first_receipt["merge"]["published_entry_count"] == 2
    assert fixture.new_shared_object.read_bytes() == fixture.fresh_content
    # The historical entry stays published and its archived object stays gone.
    assert [entry["state_id"] for entry in entries_after_first] == ["archived-state", "fresh-state"]
    assert not fixture.archived_shared_object.exists()

    assert second_receipt["destination_entry_count_before"] == 2
    assert second_receipt["destination_entry_count_after"] == 2
    # The entry is already published byte-identically, so the repeat enforce
    # copies nothing at all (#1189 A2) instead of re-copying it as "reused".
    assert second_receipt["checkpoint_copied_count"] == 0
    assert second_receipt["checkpoint_reused_count"] == 0
    assert second_receipt["checkpoint_replaced_count"] == 0
    assert second_receipt["preview_new_state_ids"] == []
    assert json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"] == entries_after_first


def test_replay_resolves_flat_cycle_id_and_skips_entries_without_one(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    # The source index also holds a historical entry, an entry of another cycle
    # and a cycle-less entry; none may be resolved into the authoritative set.
    assert receipt["source_entry_count"] == 4
    assert receipt["resolved_run_ids"] == [AUTHORITATIVE_RUN]
    assert receipt["matched_source_entry_count"] == 1
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["archived-state", "fresh-state"]


def test_replay_enforce_honors_every_repeated_cycle_flag(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The production recovery is `--cycle gfs_... --cycle ifs_...`; honouring
    # only the last flag would silently leave half the backlog behind.
    _apply_env(monkeypatch, fixture)

    exit_code = replay.main(
        ["--cycle", "gfs_2026072000", "--cycle", "IFS_2026072000", "--enforce"]
    )

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert receipt["requested_cycles"] == ["gfs_2026072000", "ifs_2026072000"]
    assert receipt["resolved_run_ids"] == sorted([AUTHORITATIVE_RUN, IFS_RUN])
    assert receipt["matched_source_entry_count"] == 2
    assert receipt["destination_entry_count_before"] == 1
    assert receipt["destination_entry_count_after"] == 3
    assert receipt["checkpoint_copied_count"] == 2
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert sorted(entry["state_id"] for entry in published) == [
        "archived-state",
        "fresh-state",
        "ifs-fresh-state",
    ]
    assert fixture.new_shared_object.read_bytes() == fixture.fresh_content
    assert fixture.ifs_shared_object.read_bytes() == fixture.ifs_content


def test_replay_enforce_refuses_missing_destination_index(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A wrong destination root (typo, unmounted NFS stub) must not be
    # bootstrapped into a fake canonical index holding only this replay.
    _apply_env(monkeypatch, fixture)
    empty_destination = fixture.root / "empty-shared-object-store"
    empty_destination.mkdir()
    monkeypatch.setenv(replay.DESTINATION_ROOT_ENV, str(empty_destination))

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert payload["status"] == "refused"
    assert payload["reason"] == "destination_index_missing"
    assert not (empty_destination / "scheduler").exists()
    assert not (empty_destination / "states").exists()
    assert list(empty_destination.iterdir()) == []
    assert not (fixture.receipt_root / "latest.json").exists()


def test_replay_enforce_bootstraps_destination_index_when_allowed(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)
    empty_destination = fixture.root / "empty-shared-object-store"
    empty_destination.mkdir()
    monkeypatch.setenv(replay.DESTINATION_ROOT_ENV, str(empty_destination))

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce", "--allow-bootstrap"])

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert receipt["destination_index_existed"] is False
    assert receipt["allow_bootstrap"] is True
    assert receipt["destination_entry_count_before"] == 0
    assert receipt["destination_entry_count_after"] == 1
    assert receipt["checkpoint_copied_count"] == 1
    published = json.loads(
        (empty_destination / "scheduler/state-index/index-last.json").read_text(encoding="utf-8")
    )
    assert [entry["state_id"] for entry in published["entries"]] == ["fresh-state"]
    assert (empty_destination / "states/gfs/model_a/fresh/state.cfg.ic").read_bytes() == (
        fixture.fresh_content
    )


def test_replay_dry_run_previews_against_missing_destination_index(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)
    empty_destination = fixture.root / "empty-shared-object-store"
    empty_destination.mkdir()
    monkeypatch.setenv(replay.DESTINATION_ROOT_ENV, str(empty_destination))

    exit_code = replay.main(["--cycle", "gfs_2026072000"])

    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert receipt["destination_index_existed"] is False
    assert receipt["destination_entry_count_before"] == 0
    assert receipt["preview_new_state_ids"] == ["fresh-state"]
    assert list(empty_destination.iterdir()) == []


def test_replay_receipt_write_failure_after_merge_reports_committed_mutation(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The merge is already committed at this point, so the tool must not report
    # a refusal and must hand the operator the merge evidence on stdout.
    _apply_env(monkeypatch, fixture)
    real_write = replay.atomic_write_bytes_no_follow

    def failing_receipt_write(path: Path, content: bytes, **kwargs: Any) -> Any:
        if Path(path).parent == fixture.receipt_root:
            raise OSError("receipt volume is read-only")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(replay, "atomic_write_bytes_no_follow", failing_receipt_write)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    assert exit_code == 3
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["reason"] == "receipt_write_failed_after_merge"
    assert error["status"] != "refused"
    assert error["receipt_failure_reason"] == "receipt_write_failed"
    assert summary["mode"] == "enforce"
    assert summary["destination_entry_count_before"] == 1
    assert summary["destination_entry_count_after"] == 2
    assert summary["merge"]["published_entry_count"] == 2
    # The index mutation stands: the merge is not rolled back by the receipt.
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["archived-state", "fresh-state"]
    assert fixture.new_shared_object.read_bytes() == fixture.fresh_content
    assert not (fixture.receipt_root / "latest.json").exists()


def test_replay_readback_failure_after_merge_keeps_receipt_and_reports_committed(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The post-merge read-back can fail on its own (NFS EIO/ESTALE, a concurrent
    # preimage change): the index mutation is already committed, so this must not
    # be reported as a refusal and the receipt must survive with a null after
    # count rather than being dropped.
    _apply_env(monkeypatch, fixture)
    real_read = replay.read_provider_snapshot
    destination_reads = 0

    def flaky_read(path: Path, **kwargs: Any) -> Any:
        nonlocal destination_reads
        if Path(path) == fixture.destination_index:
            destination_reads += 1
            # 1 = the pre-merge guard read, 2 = the post-merge read-back.
            if destination_reads == 2:
                raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        return real_read(path, **kwargs)

    monkeypatch.setattr(replay, "read_provider_snapshot", flaky_read)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    assert exit_code == 3
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["reason"] == "post_merge_readback_failed"
    assert error["status"] != "refused"
    assert error["readback_failure_reason"] == "index_unreadable"
    assert summary["mode"] == "enforce"
    assert summary["destination_entry_count_before"] == 1
    assert summary["destination_entry_count_after"] is None
    assert summary["merge"]["published_entry_count"] == 2
    assert summary["checkpoint_copied_count"] == 1
    # Keeping the receipt beats keeping the after count: it is written with the
    # degraded fields, and the index mutation stands.
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["destination_entry_count_after"] is None
    assert receipt["post_merge_readback_reason"] == "index_unreadable"
    assert receipt["destination_entries_lost_count"] is None
    assert receipt["merge"]["published_entry_count"] == 2
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["archived-state", "fresh-state"]
    assert fixture.new_shared_object.read_bytes() == fixture.fresh_content


def test_replay_reports_destination_entries_lost_across_the_merge(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The pre-merge guard reads the destination outside the provider lock.  If the
    # index disappears in that window (NFS mount drop, out-of-band delete) the
    # merge's bootstrap branch republishes only this replay's entries: 1645 -> 36
    # in production.  That must never surface as a green receipt.
    _apply_env(monkeypatch, fixture)
    real_merge = replay.merge_state_snapshot_index_copyback

    def vanishing_merge(**kwargs: Any) -> Any:
        fixture.destination_index.unlink()
        return real_merge(**kwargs)

    monkeypatch.setattr(replay, "merge_state_snapshot_index_copyback", vanishing_merge)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    assert exit_code == 3
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["reason"] == "destination_entries_lost_after_merge"
    assert error["status"] != "refused"
    assert error["lost_entry_count"] == 1
    assert error["destination_entry_count_before"] == 1
    assert error["destination_entry_count_after"] == 1
    # An equal-count contraction is exactly the case a `after >= before` count
    # comparison would wave through, so the identity set is what is compared.
    assert summary["destination_entry_count_before"] == 1
    assert summary["destination_entry_count_after"] == 1
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["destination_entries_lost_count"] == 1
    assert receipt["mode"] == "enforce"
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["fresh-state"]


def test_replay_run_ids_selection_requires_source_index_entries(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    index_before = fixture.destination_index.read_bytes()

    ok = replay.main(["--run-ids", f"{AUTHORITATIVE_RUN},{AUTHORITATIVE_RUN}", "--enforce"])
    assert ok == 0
    assert json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))[
        "resolved_run_ids"
    ] == [AUTHORITATIVE_RUN]

    index_after_ok = fixture.destination_index.read_bytes()
    refused = replay.main(["--run-ids", "unknown-run", "--enforce"])

    assert refused == 2
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["reason"] == "run_ids_absent_from_source_index"
    assert payload["missing_run_ids"] == ["unknown-run"]
    assert fixture.destination_index.read_bytes() == index_after_ok
    assert index_before != index_after_ok


def test_replay_empty_cycle_resolution_fails_closed_without_writes(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    index_before = fixture.destination_index.read_bytes()

    exit_code = replay.main(["--cycle", "gfs_2026072012", "--enforce"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["reason"] == "cycles_absent_from_source_index"
    assert payload["unresolved_cycles"] == ["gfs_2026072012"]
    assert fixture.destination_index.read_bytes() == index_before
    assert not fixture.new_shared_object.exists()
    assert not (fixture.receipt_root / "latest.json").exists()


def test_replay_refuses_identical_and_overlapping_roots(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    nested = fixture.destination_root / "nested-private"
    nested.mkdir()

    identical = replay.main(
        [
            "--reference-root",
            str(fixture.reference_root),
            "--destination-root",
            str(fixture.reference_root),
            "--cycle",
            "gfs_2026072000",
            "--enforce",
        ]
    )
    identical_payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    overlapping = replay.main(
        [
            "--reference-root",
            str(nested),
            "--destination-root",
            str(fixture.destination_root),
            "--cycle",
            "gfs_2026072000",
            "--enforce",
        ]
    )
    overlapping_payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])

    assert (identical, overlapping) == (2, 2)
    assert identical_payload["reason"] == "roots_identical"
    assert overlapping_payload["reason"] == "roots_overlap"
    assert not (fixture.receipt_root / "latest.json").exists()


def test_replay_enforce_requires_private_receipt_root(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _apply_env(monkeypatch, fixture)
    monkeypatch.delenv(replay.RECEIPT_ROOT_ENV)
    index_before = fixture.destination_index.read_bytes()

    unset = replay.main(["--cycle", "gfs_2026072000", "--enforce"])
    unset_payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])

    world_readable = fixture.root / "world-readable-receipts"
    world_readable.mkdir(mode=0o755)
    os.chmod(world_readable, 0o755)
    monkeypatch.setenv(replay.RECEIPT_ROOT_ENV, str(world_readable))
    not_private = replay.main(["--cycle", "gfs_2026072000", "--enforce"])
    not_private_payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])

    assert (unset, not_private) == (2, 2)
    assert unset_payload["reason"] == "receipt_root_unset"
    assert not_private_payload["reason"] == "receipt_root_not_private"
    assert fixture.destination_index.read_bytes() == index_before


def test_replay_creates_receipt_root_private_and_writes_history_file(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_env(monkeypatch, fixture)
    fresh_receipt_root = fixture.root / "fresh-receipts"
    monkeypatch.setenv(replay.RECEIPT_ROOT_ENV, str(fresh_receipt_root))

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    assert exit_code == 0
    assert stat.S_IMODE(fresh_receipt_root.stat().st_mode) == 0o700
    receipts = sorted(path.name for path in fresh_receipt_root.iterdir())
    assert "latest.json" in receipts
    history = [name for name in receipts if name.endswith("-enforce.json")]
    assert len(history) == 1
    assert stat.S_IMODE((fresh_receipt_root / history[0]).stat().st_mode) == 0o600
    assert json.loads((fresh_receipt_root / history[0]).read_text(encoding="utf-8"))["mode"] == "enforce"


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.reference_root = root / "object-store"
        self.destination_root = root / "shared-object-store"
        self.receipt_root = root / "receipts"
        self.receipt_root.mkdir(mode=0o700)
        os.chmod(self.receipt_root, 0o700)
        private_store = LocalObjectStore(self.reference_root, PREFIX)
        self.archived_content = _valid_state_bytes(b"archived")
        self.fresh_content = _valid_state_bytes(b"fresh")
        self.ifs_content = _valid_state_bytes(b"ifs-fresh")
        self.cycleless_content = _valid_state_bytes(b"cycleless")
        archived_uri = private_store.write_bytes_atomic(
            "states/gfs/model_a/archived/state.cfg.ic", self.archived_content
        )
        fresh_uri = private_store.write_bytes_atomic(
            "states/gfs/model_a/fresh/state.cfg.ic", self.fresh_content
        )
        ifs_uri = private_store.write_bytes_atomic(
            "states/ifs/model_a/fresh/state.cfg.ic", self.ifs_content
        )
        cycleless_uri = private_store.write_bytes_atomic(
            "states/gfs/model_b/cycleless/state.cfg.ic", self.cycleless_content
        )
        self.archived_entry = _entry(
            state_id="archived-state",
            run_id=HISTORICAL_RUN,
            state_uri=archived_uri,
            content=self.archived_content,
            valid_time="2026-07-05T12:00:00Z",
            created_at="2026-07-05T13:00:00Z",
            cycle_id="gfs_2026070500",
        )
        self.fresh_entry = _entry(
            state_id="fresh-state",
            run_id=AUTHORITATIVE_RUN,
            state_uri=fresh_uri,
            content=self.fresh_content,
            valid_time="2026-07-20T12:00:00Z",
            created_at="2026-07-27T01:00:00Z",
            cycle_id="gfs_2026072000",
        )
        # Production replays both deterministic sources of one cycle time, so
        # the fixture carries a second cycle whose entry is also missing from
        # the shared index.
        self.ifs_entry = _entry(
            state_id="ifs-fresh-state",
            run_id=IFS_RUN,
            state_uri=ifs_uri,
            content=self.ifs_content,
            valid_time="2026-07-20T12:00:00Z",
            created_at="2026-07-27T01:00:00Z",
            cycle_id="ifs_2026072000",
            source_id="ifs",
        )
        self.cycleless_entry = {
            **_entry(
                state_id="cycleless-state",
                run_id="fcst_gfs_2026072000_model_b",
                state_uri=cycleless_uri,
                content=self.cycleless_content,
                valid_time="2026-07-20T12:00:00Z",
                created_at="2026-07-27T01:00:00Z",
                cycle_id="gfs_2026072000",
            ),
            "model_id": "model_b",
            "cycle_id": None,
            "lead_hours": None,
        }
        self.source_index = self.reference_root / "scheduler/state-index/index-last.json"
        self.destination_index = self.destination_root / "scheduler/state-index/index-last.json"
        publish_state_snapshot_index(
            [self.archived_entry, self.fresh_entry, self.ifs_entry, self.cycleless_entry],
            self.source_index,
            object_store_root=self.reference_root,
            object_store_prefix=PREFIX,
            generated_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
        )
        publish_state_snapshot_index(
            [self.archived_entry],
            self.destination_index,
            object_store_root=self.destination_root,
            object_store_prefix=PREFIX,
            generated_at=datetime(2026, 7, 25, 18, tzinfo=UTC),
            verify_objects=False,
        )
        self.archived_shared_object = self.destination_root / "states/gfs/model_a/archived/state.cfg.ic"
        self.new_shared_object = self.destination_root / "states/gfs/model_a/fresh/state.cfg.ic"
        self.ifs_shared_object = self.destination_root / "states/ifs/model_a/fresh/state.cfg.ic"


@pytest.fixture(name="fixture")
def fixture_factory(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _apply_env(monkeypatch: pytest.MonkeyPatch, fixture: Fixture) -> None:
    monkeypatch.setenv(replay.REFERENCE_ROOT_ENV, str(fixture.reference_root))
    monkeypatch.setenv(replay.DESTINATION_ROOT_ENV, str(fixture.destination_root))
    monkeypatch.setenv(replay.OBJECT_STORE_PREFIX_ENV, PREFIX)
    monkeypatch.setenv(replay.RECEIPT_ROOT_ENV, str(fixture.receipt_root))


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
    source_id: str = "gfs",
) -> dict[str, Any]:
    return {
        "state_id": state_id,
        "model_id": "model_a",
        "run_id": run_id,
        "source_id": source_id,
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
