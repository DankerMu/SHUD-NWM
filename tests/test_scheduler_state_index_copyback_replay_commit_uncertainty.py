"""Post-merge failure triage: committed-mutation, lost-entry and commit-uncertain (#1611).

Partition of ``tests/test_scheduler_state_index_copyback_replay.py``; every case
below is a verbatim move. Shared fixtures live in
``tests/scheduler_state_index_copyback_replay_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from packages.common import provider_atomic, state_manager
from packages.common.provider_atomic import ProviderAtomicError
from scripts import scheduler_state_index_copyback_replay as replay
from tests.scheduler_state_index_copyback_replay_helpers import (
    AUTHORITATIVE_RUN,
    Fixture,
    _apply_env,
    _fail_index_fsync_after_replace,
    fixture_factory,  # noqa: F401  (registers the `fixture` fixture on this module)
)
from tests.test_state_manager import _LockReleaseSeam


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


def test_replay_merge_commit_uncertainty_runs_committed_tail_without_refusing(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `os.replace` succeeded and the directory fsync failed: the shared index
    # already holds the new bytes, so reporting a refusal (rc 2, "index unchanged")
    # would lie to the operator and skip the whole committed evidence chain.
    _apply_env(monkeypatch, fixture)
    _fail_index_fsync_after_replace(monkeypatch, fixture)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    assert exit_code == 3
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["reason"] == "merge_commit_uncertain"
    assert error["status"] == "merge_committed_incomplete"
    assert error["status"] != "refused"
    assert error["error_reason"] == "provider_replace_uncertain"
    assert error["resolved_run_ids"] == [AUTHORITATIVE_RUN]
    # The committed tail ran: read-back, superset guard and receipt.
    assert summary["destination_entry_count_after"] == 2
    assert summary["destination_entries_lost_count"] == 0
    assert summary["merge_commit_state"] == "uncertain"
    assert summary["merge_error_reason"] == "provider_replace_uncertain"
    # No merge return value exists on this path, so its evidence stays null.
    assert summary["merge"] is None
    assert summary["checkpoint_copied_count"] is None
    assert summary["checkpoint_reused_count"] is None
    assert summary["checkpoint_replaced_count"] is None
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["merge"] is None
    assert receipt["merge_commit_state"] == "uncertain"
    assert receipt["merge_error_reason"] == "provider_replace_uncertain"
    assert receipt["destination_entry_count_after"] == 2
    assert receipt["destination_entries_lost_count"] == 0
    # The mutation really is on disk, which is exactly why rc 2 is forbidden here.
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["archived-state", "fresh-state"]
    assert fixture.new_shared_object.read_bytes() == fixture.fresh_content


def test_replay_lost_entry_verdict_outranks_merge_commit_uncertainty(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Compound disaster: the index vanishes in the guard-to-lock window (so the
    # merge bootstraps a shrunken index) and the commit is uncertain on top.  The
    # loss verdict must survive -- rerunning enforce against the shrunken index
    # would otherwise freeze the data loss behind a green receipt.
    _apply_env(monkeypatch, fixture)
    real_merge = replay.merge_state_snapshot_index_copyback

    def vanishing_merge(**kwargs: Any) -> Any:
        fixture.destination_index.unlink()
        return real_merge(**kwargs)

    monkeypatch.setattr(replay, "merge_state_snapshot_index_copyback", vanishing_merge)
    _fail_index_fsync_after_replace(monkeypatch, fixture)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    assert exit_code == 3
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["reason"] == "destination_entries_lost_after_merge"
    assert error["status"] != "refused"
    assert error["lost_entry_count"] == 1
    assert error["failure_reasons"] == [
        "destination_entries_lost_after_merge",
        "merge_commit_uncertain",
    ]
    assert error["merge_commit_uncertain"] is True
    assert error["merge_error_reason"] == "provider_replace_uncertain"
    assert summary["destination_entries_lost_count"] == 1
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["destination_entries_lost_count"] == 1
    assert receipt["merge_commit_state"] == "uncertain"
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["fresh-state"]


def test_replay_lost_entry_verdict_outranks_receipt_write_failure(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Both failures in one run, and the runbook routes them in opposite
    # directions: a receipt failure says "rerun enforce", a loss says "stop and
    # rebuild".  The reported reason must therefore be the loss (#1189 r3 D2).
    _apply_env(monkeypatch, fixture)
    real_merge = replay.merge_state_snapshot_index_copyback

    def vanishing_merge(**kwargs: Any) -> Any:
        fixture.destination_index.unlink()
        return real_merge(**kwargs)

    monkeypatch.setattr(replay, "merge_state_snapshot_index_copyback", vanishing_merge)
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
    assert error["reason"] == "destination_entries_lost_after_merge"
    assert error["status"] != "refused"
    assert error["lost_entry_count"] == 1
    assert error["failure_reasons"] == [
        "destination_entries_lost_after_merge",
        "receipt_write_failed_after_merge",
    ]
    # Nothing observed is dropped: the receipt failure rides in the details.
    assert error["receipt_write_failed"] is True
    assert error["receipt_failure_reason"] == "receipt_write_failed"
    # The operator's only surviving evidence is the stdout summary.
    assert summary["destination_entries_lost_count"] == 1
    assert summary["destination_entry_count_before"] == 1
    assert not (fixture.receipt_root / "latest.json").exists()


def test_replay_provider_postread_failure_is_commit_uncertain(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `provider_postread_failed` is raised at phase="replace_uncertain" after the
    # compare-and-swap wrote the new bytes, so it is not a refusal either.
    _apply_env(monkeypatch, fixture)
    real_replace = state_manager.atomic_replace_provider_bytes

    def replace_then_fail_postread(path: Path, content: bytes, **kwargs: Any) -> Any:
        committed = real_replace(path, content, **kwargs)
        if Path(path) == fixture.destination_index:
            raise ProviderAtomicError("provider_postread_failed", phase="replace_uncertain")
        return committed

    monkeypatch.setattr(state_manager, "atomic_replace_provider_bytes", replace_then_fail_postread)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    assert exit_code == 3
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["reason"] == "merge_commit_uncertain"
    assert error["status"] != "refused"
    assert error["error_reason"] == "provider_postread_failed"
    assert summary["destination_entry_count_after"] == 2
    assert summary["destination_entries_lost_count"] == 0
    assert summary["merge"] is None
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["merge_error_reason"] == "provider_postread_failed"
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["archived-state", "fresh-state"]


def test_replay_untyped_merge_exception_is_commit_uncertain(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Any unclassified exception raised after the destination compare-and-swap
    # escaped the entire triage under a typed-only handler: rc 1 bare traceback,
    # no receipt, no stdout summary and no superset guard -- with the index
    # already committed.  The provider lock teardown used to be exactly that
    # case; #1193 made it typed (`provider_lock_release_failed`, pinned by
    # test_replay_lock_release_failure_after_commit_is_commit_uncertain), so this
    # case now stands for every remaining bare exception.
    _apply_env(monkeypatch, fixture)
    real_merge = replay.merge_state_snapshot_index_copyback

    def merge_then_raise_untyped(**kwargs: Any) -> Any:
        real_merge(**kwargs)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(replay, "merge_state_snapshot_index_copyback", merge_then_raise_untyped)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    # The forbidden shape is rc 1 with an empty stdout; both are asserted away.
    assert exit_code == 3
    assert exit_code != 1
    assert captured.out.strip()
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["status"] == "merge_committed_incomplete"
    assert error["status"] != "refused"
    assert error["reason"] == "merge_commit_uncertain"
    # The exception type stays legible to the operator, not a blank verdict.
    assert error["error_reason"] == "merge_unexpected_exception:OSError"
    assert "Input/output error" in error["error"]
    assert error["resolved_run_ids"] == [AUTHORITATIVE_RUN]
    # The committed tail ran: read-back, superset guard and receipt.
    assert summary["merge_commit_state"] == "uncertain"
    assert summary["merge_error_reason"] == "merge_unexpected_exception:OSError"
    assert summary["merge"] is None
    assert summary["destination_entry_count_after"] == 2
    assert summary["destination_entries_lost_count"] == 0
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["merge_commit_state"] == "uncertain"
    assert receipt["merge_error_reason"] == "merge_unexpected_exception:OSError"
    assert receipt["destination_entry_count_after"] == 2
    assert receipt["destination_entries_lost_count"] == 0
    # The mutation really is on disk, which is why a refusal would lie here.
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["archived-state", "fresh-state"]


def test_replay_lock_release_failure_after_commit_is_commit_uncertain(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #1193: the provider lock releases after the destination compare-and-swap,
    # so `provider_lock_release_failed` is deliberately kept off the pre-commit
    # allowlist and must ride the existing commit-uncertain channel -- with the
    # real reason now, instead of a synthetic `merge_unexpected_exception:*`.
    _apply_env(monkeypatch, fixture)
    assert "provider_lock_release_failed" not in replay.MERGE_PRE_COMMIT_REFUSAL_REASONS
    seam = _LockReleaseSeam(fixture.destination_index)
    monkeypatch.setattr(provider_atomic, "fcntl", seam)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    # The forbidden shapes: rc 1 with a bare traceback, an empty stdout, or a
    # refusal that would claim the shared index is untouched.
    assert exit_code == 3
    assert exit_code != 1
    assert captured.out.strip()
    assert seam.failed_releases == 1
    summary = json.loads(captured.out.strip().splitlines()[-1])
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["status"] == "merge_committed_incomplete"
    assert error["status"] != "refused"
    assert error["reason"] == "merge_commit_uncertain"
    assert error["error_reason"] == "provider_lock_release_failed"
    assert error["resolved_run_ids"] == [AUTHORITATIVE_RUN]
    assert summary["merge_commit_state"] == "uncertain"
    assert summary["merge_error_reason"] == "provider_lock_release_failed"
    assert summary["merge"] is None
    assert summary["destination_entry_count_after"] == 2
    assert summary["destination_entries_lost_count"] == 0
    receipt = json.loads((fixture.receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert receipt["merge_commit_state"] == "uncertain"
    assert receipt["merge_error_reason"] == "provider_lock_release_failed"
    assert receipt["merge"] is None
    assert receipt["destination_entry_count_after"] == 2
    # The commit really happened, which is why a refusal would lie here.
    published = json.loads(fixture.destination_index.read_text(encoding="utf-8"))["entries"]
    assert [entry["state_id"] for entry in published] == ["archived-state", "fresh-state"]
    assert fixture.new_shared_object.read_bytes() == fixture.fresh_content
