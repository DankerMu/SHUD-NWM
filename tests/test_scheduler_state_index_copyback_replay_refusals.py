"""Pre-commit refusals: root identity, lockfile identity and receipt-root posture (#1611).

Partition of ``tests/test_scheduler_state_index_copyback_replay.py``; every case
below is a verbatim move. Shared fixtures live in
``tests/scheduler_state_index_copyback_replay_helpers.py``.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from packages.common import provider_atomic, safe_fs, state_manager
from packages.common.provider_atomic import ProviderAtomicError
from scripts import scheduler_state_index_copyback_replay as replay
from tests.scheduler_state_index_copyback_replay_helpers import (
    Fixture,
    _apply_env,
    _call_without_hanging,
    _inject_alias_identity,
    _valid_state_bytes,
    fixture_factory,  # noqa: F401  (registers the `fixture` fixture on this module)
    private_umask_fixture_factory,  # noqa: F401  (registers `private_umask_fixture`)
)
from tests.test_state_manager import _LockReleaseSeam


def test_replay_double_fault_keeps_the_pre_commit_refusal(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Double fault: the merge refuses before the compare-and-swap *and* the lock
    # release fails while that refusal unwinds.  Before #1193 the bare release
    # OSError masked the body error and the run was reported commit-uncertain
    # (rc 3); now the audited pre-commit refusal survives, so this is a
    # deliberate uncertain -> refused reclassification.  It is the correct
    # direction -- the shared index really is untouched -- and rc 2 is the only
    # exit code that says so.
    _apply_env(monkeypatch, fixture)
    index_before = fixture.destination_index.read_bytes()
    real_replace = state_manager.atomic_replace_provider_bytes

    def refuse_replace(path: Path, content: bytes, **kwargs: Any) -> Any:
        if Path(path) == fixture.destination_index:
            raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        return real_replace(path, content, **kwargs)

    monkeypatch.setattr(state_manager, "atomic_replace_provider_bytes", refuse_replace)
    seam = _LockReleaseSeam(fixture.destination_index)
    monkeypatch.setattr(provider_atomic, "fcntl", seam)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert exit_code == 2
    assert error["status"] == "refused"
    assert error["reason"] == "merge_failed"
    assert error["error_reason"] == "provider_preimage_changed"
    # The release really did fail and really was suppressed.
    assert seam.failed_releases == 1
    assert fixture.destination_index.read_bytes() == index_before
    assert not (fixture.receipt_root / "latest.json").exists()


def test_replay_pre_commit_allowlisted_merge_failure_still_refuses(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The allowlist must not creep: `provider_preimage_changed` is raised before
    # the compare-and-swap writes anything (provider_atomic.py:305-307), so "index
    # unchanged" holds and rc 2 remains correct.
    _apply_env(monkeypatch, fixture)
    assert "provider_preimage_changed" in replay.MERGE_PRE_COMMIT_REFUSAL_REASONS
    index_before = fixture.destination_index.read_bytes()
    real_replace = state_manager.atomic_replace_provider_bytes

    def refuse_replace(path: Path, content: bytes, **kwargs: Any) -> Any:
        if Path(path) == fixture.destination_index:
            raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        return real_replace(path, content, **kwargs)

    monkeypatch.setattr(state_manager, "atomic_replace_provider_bytes", refuse_replace)

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    assert exit_code == 2
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["status"] == "refused"
    assert error["reason"] == "merge_failed"
    assert error["error_reason"] == "provider_preimage_changed"
    assert fixture.destination_index.read_bytes() == index_before
    assert not (fixture.receipt_root / "latest.json").exists()


def test_replay_source_object_checksum_divergence_refuses_before_any_commit(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A natural (uninjected) pre-commit refusal: the source-side full-index object
    # verification fails closed, long before the destination compare-and-swap.
    _apply_env(monkeypatch, fixture)
    private_fresh_object = fixture.reference_root / "states/gfs/model_a/fresh/state.cfg.ic"
    private_fresh_object.write_bytes(_valid_state_bytes(b"tampered"))
    index_before = fixture.destination_index.read_bytes()

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    captured = capsys.readouterr()
    assert exit_code == 2
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["status"] == "refused"
    assert error["reason"] == "merge_failed"
    assert error["error_reason"] == "state_snapshot_index_object_checksum_mismatch"
    assert error["error_reason"] in replay.MERGE_PRE_COMMIT_REFUSAL_REASONS
    assert fixture.destination_index.read_bytes() == index_before
    assert not fixture.new_shared_object.exists()


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


def test_replay_refuses_alias_roots_reporting_one_filesystem_identity(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two distinct realpaths, one inode: the guard must still refuse (#1192).

    The injection replaces the probe the guard actually calls, so it pins that
    the guard consumes filesystem identity rather than the resolved path
    string.  Under the pre-#1192 string comparison this alias pair is read as
    two different roots, the replay proceeds, and the scoped merge takes the
    provider destination lock twice on one lockfile and blocks forever.
    """

    _apply_env(monkeypatch, fixture)
    alias_destination = fixture.root / "alias-destination"
    alias_destination.mkdir()
    _inject_alias_identity(monkeypatch, fixture.reference_root, alias_destination)

    exit_code = _call_without_hanging(
        lambda: replay.main(
            [
                "--reference-root",
                str(fixture.reference_root),
                "--destination-root",
                str(alias_destination),
                "--cycle",
                "gfs_2026072000",
                "--enforce",
            ]
        )
    )

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert payload["reason"] == "roots_identical"
    assert not (alias_destination / "scheduler/state-index/index-last.json").exists()
    assert not (fixture.receipt_root / "latest.json").exists()


def test_replay_still_refuses_a_symlink_alias_destination_root(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Symlink aliases are caught by `.resolve()`, NOT by the identity probe.

    `_resolved_root` folds the link away before the guard runs, so both roots
    arrive as one resolved path and the identity comparison holds trivially.
    The probe itself cannot take this input: its per-component no-follow walk
    rejects a symlink final component outright. Do not read this test as
    evidence that the helper handles symlinks.

    Accepted limit recorded alongside (proposal Known Limits): the overlap
    check stays a resolved-path string comparison, so an alias that makes one
    root a *child* of the other stays undetectable. No test claims otherwise.
    """

    _apply_env(monkeypatch, fixture)
    alias = fixture.root / "reference-alias"
    alias.symlink_to(fixture.reference_root, target_is_directory=True)

    exit_code = replay.main(
        [
            "--reference-root",
            str(fixture.reference_root),
            "--destination-root",
            str(alias),
            "--cycle",
            "gfs_2026072000",
            "--enforce",
        ]
    )

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert payload["reason"] == "roots_identical"
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


def test_replay_state_index_lock_collision_is_a_refusal_not_an_uncertain_commit(
    private_umask_fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1609 A.7: this tool classifies by reason allowlist, not by phase.

    A phase-free error is enough for `run_tree_copyback`, but here an unlisted
    reason falls through to commit-uncertain -- exit 3, `merge_commit_state:
    "uncertain"`, the committed-tail verification and a receipt -- for a refusal
    that took no lock and touched nothing.  This is the nail that makes forgetting
    the allowlist a red instead of a false green: the suite otherwise only spot
    checks individual reasons and has no coverage test over the allowlist.
    """

    _apply_env(monkeypatch, private_umask_fixture)
    assert "state_snapshot_index_copyback_lock_identical" in replay.MERGE_PRE_COMMIT_REFUSAL_REASONS
    source_lock = provider_atomic.provider_lock_path(private_umask_fixture.source_index)
    destination_lock = provider_atomic.provider_lock_path(private_umask_fixture.destination_index)
    # Both lock parents private, or `provider_lock_parent_unsafe` fires first.
    source_lock.parent.chmod(0o700)
    destination_lock.parent.chmod(0o700)
    destination_lock.unlink()
    os.link(source_lock, destination_lock)
    index_before = private_umask_fixture.destination_index.read_bytes()

    previous_umask = os.umask(0o077)
    try:
        exit_code = _call_without_hanging(lambda: replay.main(["--cycle", "gfs_2026072000", "--enforce"]))
    finally:
        os.umask(previous_umask)

    captured = capsys.readouterr()
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert exit_code == 2
    assert exit_code != 3
    assert error["status"] == "refused"
    assert error["status"] != "merge_committed_incomplete"
    assert error["reason"] == "merge_failed"
    assert error["error_reason"] == "state_snapshot_index_copyback_lock_identical"
    # The committed tail never ran: no receipt, no uncertain verdict, no
    # read-back of a destination nothing wrote.
    assert not (private_umask_fixture.receipt_root / "latest.json").exists()
    assert private_umask_fixture.destination_index.read_bytes() == index_before
    assert not private_umask_fixture.new_shared_object.exists()


def test_replay_allowlists_the_lock_identity_unavailable_refusal() -> None:
    """#1610: the guard's *other* reason must classify as a refusal too.

    `_refuse_identical_copyback_lockfiles` raises two reasons, both from the same
    pre-commit point, and only one of them was pinned above.  A probe that cannot
    answer takes no lock and touches nothing, so leaving
    `state_snapshot_index_copyback_lock_identity_unavailable` off the allowlist
    would fall through to commit-uncertain -- exit 3, the committed tail, a
    receipt -- for a merge that provably never started.
    """

    assert (
        "state_snapshot_index_copyback_lock_identity_unavailable"
        in replay.MERGE_PRE_COMMIT_REFUSAL_REASONS
    )


def test_replay_root_identity_probe_failure_stays_root_unavailable(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1610: `_root_identity`'s own failure posture, previously unenforced.

    A probe failure must reuse `root_unavailable` and name the field, not escape
    as a bare OSError traceback (rc 1, no stderr payload) and not degrade into a
    permissive pass.
    """

    _apply_env(monkeypatch, fixture)
    real_probe = safe_fs.directory_identity_no_follow
    target = fixture.destination_root.resolve()

    def probe(path: Path) -> tuple[int, int]:
        if Path(path).resolve() == target:
            raise OSError("probe blocked")
        return real_probe(path)

    monkeypatch.setattr(replay, "directory_identity_no_follow", probe)
    index_before = fixture.destination_index.read_bytes()

    exit_code = replay.main(["--cycle", "gfs_2026072000", "--enforce"])

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert exit_code == 2
    assert exit_code != 1
    assert payload["reason"] == "root_unavailable"
    assert payload["field"] == "destination_root"
    assert payload["path"] == str(target)
    assert fixture.destination_index.read_bytes() == index_before
    assert not (fixture.receipt_root / "latest.json").exists()
