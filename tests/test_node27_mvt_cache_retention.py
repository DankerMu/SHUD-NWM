"""Issue #2032 -- MVT tile file-cache retention runner, wrapper and units.

Governing invariant (design.md Invariant Matrix): every file the display API
creates under `NHMS_MVT_FILE_CACHE_DIR` outside `precip/` has a bounded life,
and this runner touches EXACTLY three path shapes to give it one -- never
`precip/**`, never a symlink, never a directory wearing a `.pbf` name, never a
level deeper than `<root>/<hh>/`.

#2490 split the suite into three partitions (pure move): this file (the three
pruned shapes and the lock lane), `tests/test_node27_mvt_cache_retention_enumeration.py`
and `tests/test_node27_mvt_cache_retention_wrapper.py`; the shared builders and
the autouse `_clean_env` live in `tests/node27_mvt_cache_retention_helpers.py`.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_mvt_cache_retention as runner
from tests.node27_mvt_cache_retention_helpers import (
    _AGED,
    _ENUMERATION_FAILURE_KEYS,
    _NOW,
    _SHA_A,
    _SHA_B,
    _all_entries,
    _clean_env,  # noqa: F401 -- autouse
    _config,
    _mixed_tree,
    _paths,
    _reasons,
    _touch,
)


def test_only_the_three_aged_shapes_are_deleted(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    tree = _mixed_tree(root)

    payload = runner.run_retention(_config(root), now=_NOW)

    assert payload["status"] == "completed"
    assert payload["execution_mode"] == "production_execute"
    assert sorted(_paths(payload["deleted"])) == sorted(
        str(tree[name]) for name in ("aged_pbf", "aged_tmp", "aged_lock")
    )
    assert payload["counts"] == {"planned": 3, "deleted": 3, "skipped": 0, "failed": 0}
    assert {entry["kind"] for entry in payload["deleted"]} == {"pbf", "tmp", "lock"}
    for name in ("aged_pbf", "aged_tmp", "aged_lock"):
        assert not tree[name].exists()
    for name, path in tree.items():
        if name in {"aged_pbf", "aged_tmp", "aged_lock"}:
            continue
        assert os.path.lexists(path), f"{name} must survive"
    # Symlink victim untouched through the link, directory decoy still a directory.
    assert tree["symlink_victim"].read_bytes() == b"tile"
    assert tree["directory_pbf"].is_dir()
    # The precip subtree is never named under ANY key.
    precip_prefix = str(root / "precip")
    assert not [entry for entry in _all_entries(payload) if str(entry["path"]).startswith(precip_prefix)]
    assert payload["precip_root_untouched"] == precip_prefix


def test_plan_only_lists_the_same_targets_and_removes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    tree = _mixed_tree(root)
    execute = runner.run_retention(_config(root, plan_only=True), now=_NOW)

    assert execute["execution_mode"] == "plan_only"
    assert execute["status"] == "completed"
    assert execute["deleted"] == []
    assert execute["counts"]["planned"] == 3
    assert execute["counts"]["deleted"] == 0
    assert execute["freed_bytes"] == 0
    assert sorted(_paths(execute["planned"])) == sorted(
        str(tree[name]) for name in ("aged_pbf", "aged_tmp", "aged_lock")
    )
    for path in tree.values():
        assert os.path.lexists(path)


def test_plan_only_and_execute_plan_the_identical_set(tmp_path: Path) -> None:
    plan_root = tmp_path / "plan"
    execute_root = tmp_path / "execute"
    _mixed_tree(plan_root)
    _mixed_tree(execute_root)

    planned = runner.run_retention(_config(plan_root, plan_only=True), now=_NOW)
    executed = runner.run_retention(_config(execute_root), now=_NOW)

    assert [
        (Path(entry["path"]).relative_to(plan_root).as_posix(), entry["kind"])
        for entry in planned["planned"]
    ] == [
        (Path(entry["path"]).relative_to(execute_root).as_posix(), entry["kind"])
        for entry in executed["deleted"]
    ]


# ---------------------------------------------------------------------------
# Scenario: a held lock file is skipped, not deleted
# ---------------------------------------------------------------------------
def test_a_held_lock_file_is_skipped_and_the_other_targets_still_go(tmp_path: Path) -> None:
    """`flock` is per open-file-description, so a second `open()` in THIS
    process is a faithful stand-in for another process holding the lock."""
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    aged_lock = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")

    holder = os.open(aged_lock, os.O_RDONLY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = runner.run_retention(_config(root), now=_NOW)
    finally:
        os.close(holder)

    assert payload["counts"]["planned"] == 2
    assert _paths(payload["deleted"]) == [str(aged_pbf)]
    assert payload["skipped"] == [
        {"path": str(aged_lock), "kind": "lock", "reason": "lock_held"}
    ]
    assert payload["failed"] == []
    assert aged_lock.exists()
    assert not aged_pbf.exists()


# ---------------------------------------------------------------------------
# Scenario: a lock path recreated after the runner opened it is not unlinked
# ---------------------------------------------------------------------------
def test_a_lock_path_recreated_after_open_is_not_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window the identity recheck closes.

    `_open_lock_fd` is the seam: the test hands back a descriptor on the ORIGINAL
    inode and only then replaces the path, which is exactly the interleaving
    "holder finished and unlinked, a live miss recreated the path" produces.
    Without the `fstat`-vs-`lstat` comparison the unlink below would delete a
    lock somebody is holding right now.
    """
    root = tmp_path / "cache"
    lock_path = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"old")
    replacement = _touch(tmp_path / "replacement.lock", mtime=_AGED, content=b"new")
    original_inode = os.lstat(lock_path).st_ino

    targets, _, _ = runner.collect_targets(root, cutoff=datetime(2026, 8, 25, tzinfo=UTC))
    assert [target.kind for target in targets] == ["lock"]

    stale_fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    os.replace(replacement, lock_path)
    monkeypatch.setattr(runner, "_open_lock_fd", lambda path: stale_fd)

    outcome, error = runner._remove_lock_target(targets[0])

    assert (outcome, error) == ("already_gone", None)
    assert lock_path.read_bytes() == b"new"
    assert os.lstat(lock_path).st_ino != original_inode


def _collected_lock_target(root: Path) -> runner.CacheTarget:
    """The one aged lock target of `root`, exactly as `collect_targets` sees it.

    Every branch below is driven with a REAL collected target and then races the
    path, which is the only interleaving that reaches `_remove_lock_target`'s
    post-collection classifications in production.
    """
    targets, _, failed = runner.collect_targets(root, cutoff=datetime(2026, 8, 25, tzinfo=UTC))
    assert failed == []
    assert [target.kind for target in targets] == ["lock"]
    return targets[0]


def test_a_lock_file_unlinked_after_collection_is_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `open` ENOENT branch: a display worker released between the two steps."""
    root = tmp_path / "cache"
    lock_path = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    target = _collected_lock_target(root)

    lock_path.unlink()

    assert runner._remove_lock_target(target) == ("already_gone", None)

    # The same race through the runner, so the summary vocabulary is pinned too.
    _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    real_collect = runner.collect_targets

    def collect_then_unlink(root_path: Path, *, cutoff: datetime) -> Any:
        collected = real_collect(root_path, cutoff=cutoff)
        lock_path.unlink()
        return collected

    monkeypatch.setattr(runner, "collect_targets", collect_then_unlink)

    payload = runner.run_retention(_config(root), now=_NOW)

    assert payload["skipped"] == [
        {"path": str(lock_path), "kind": "lock", "reason": "already_gone"}
    ]
    assert payload["failed"] == []
    assert payload["deleted"] == []


def test_a_lock_path_replaced_by_a_symlink_after_collection_is_not_regular_file(
    tmp_path: Path,
) -> None:
    """`O_NOFOLLOW` refuses it (ELOOP/EMLINK), so the link's TARGET is safe."""
    root = tmp_path / "cache"
    lock_path = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    victim = _touch(tmp_path / "victim.lock", mtime=_AGED, content=b"live")
    target = _collected_lock_target(root)

    lock_path.unlink()
    lock_path.symlink_to(victim)

    assert runner._remove_lock_target(target) == ("not_regular_file", None)
    assert lock_path.is_symlink()
    assert victim.read_bytes() == b"live"


def test_a_lock_path_replaced_by_a_directory_after_collection_is_not_regular_file(
    tmp_path: Path,
) -> None:
    """`O_RDONLY` on a directory SUCCEEDS; only the `fstat` shape check catches it."""
    root = tmp_path / "cache"
    lock_path = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    target = _collected_lock_target(root)

    lock_path.unlink()
    lock_path.mkdir()

    assert runner._remove_lock_target(target) == ("not_regular_file", None)
    assert lock_path.is_dir()


def test_a_lock_path_replaced_by_a_fifo_after_collection_returns_promptly(tmp_path: Path) -> None:
    """The `O_NONBLOCK` oracle: without it this call never returns.

    Opening a writer-less FIFO read-only blocks until a writer appears, and the
    retention unit is `Type=oneshot` with `TimeoutStartSec=0` behind a wrapper
    whose `flock -n` would then skip every later tick at rc 0 -- a hang that
    reports as healthy. The worker thread is a daemon so that a red here cannot
    also wedge the interpreter at shutdown.
    """
    root = tmp_path / "cache"
    lock_path = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    target = _collected_lock_target(root)

    lock_path.unlink()
    os.mkfifo(lock_path)
    observed: list[Any] = []
    worker = threading.Thread(
        target=lambda: observed.append(runner._remove_lock_target(target)), daemon=True
    )
    worker.start()
    worker.join(5.0)

    assert not worker.is_alive(), "the open blocked: _open_lock_fd is missing os.O_NONBLOCK"
    assert observed == [("not_regular_file", None)]
    assert stat.S_ISFIFO(os.lstat(lock_path).st_mode)


def test_not_regular_file_is_a_lock_lane_skip_in_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cache"
    lock_path = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    aged_pbf = _touch(root / "ab" / f"{_SHA_B}.pbf", mtime=_AGED)
    real_collect = runner.collect_targets

    def collect_then_replace(root_path: Path, *, cutoff: datetime) -> Any:
        collected = real_collect(root_path, cutoff=cutoff)
        lock_path.unlink()
        lock_path.mkdir()
        return collected

    monkeypatch.setattr(runner, "collect_targets", collect_then_replace)

    payload = runner.run_retention(_config(root), now=_NOW)

    assert payload["skipped"] == [
        {"path": str(lock_path), "kind": "lock", "reason": "not_regular_file"}
    ]
    assert payload["failed"] == []
    assert _paths(payload["deleted"]) == [str(aged_pbf)]
    assert lock_path.is_dir()


def test_a_target_that_disappears_before_unlink_is_a_skip_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cache"
    vanishing = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    unaffected = _touch(root / "ab" / f".{_SHA_B}.pbf.7.tmp", mtime=_AGED)
    (root / ".locks").mkdir()
    real_collect = runner.collect_targets

    def collect_then_race(root_path: Path, *, cutoff: datetime) -> Any:
        collected = real_collect(root_path, cutoff=cutoff)
        vanishing.unlink()
        return collected

    monkeypatch.setattr(runner, "collect_targets", collect_then_race)

    payload = runner.run_retention(_config(root), now=_NOW)

    assert payload["failed"] == []
    assert payload["skipped"] == [
        {"path": str(vanishing), "kind": "pbf", "reason": "already_gone"}
    ]
    assert _paths(payload["deleted"]) == [str(unaffected)]
    assert payload["counts"] == {"planned": 2, "deleted": 1, "skipped": 1, "failed": 0}
    assert not unaffected.exists()


def test_already_gone_keeps_the_exit_code_at_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cache"
    vanishing = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    (root / ".locks").mkdir()
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    real_collect = runner.collect_targets

    def collect_then_race(root_path: Path, *, cutoff: datetime) -> Any:
        collected = real_collect(root_path, cutoff=cutoff)
        vanishing.unlink()
        return collected

    monkeypatch.setattr(runner, "collect_targets", collect_then_race)

    exit_code = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert _reasons(payload["skipped"]) == {"already_gone"}


# ---------------------------------------------------------------------------
# Scenario: every system call after the lock open is classified (#2160)
# ---------------------------------------------------------------------------
def test_a_failing_fstat_on_a_lock_target_is_failed_and_the_summary_is_still_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An NFS `ESTALE` from the `fstat` right after the open must not escape
    `run_retention`: an escape skips `_emit`, so no summary is written and the
    health check reads yesterday's receipt as today's."""
    root = tmp_path / "cache"
    lock_path = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    aged_pbf = _touch(root / "ab" / f"{_SHA_B}.pbf", mtime=_AGED)
    summary = tmp_path / "receipts" / "summary.json"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    opened: list[int] = []
    closed: list[int] = []
    real_open_lock_fd = runner._open_lock_fd
    real_fstat = os.fstat
    real_close = os.close

    def recording_open(path: Path) -> int:
        fd = real_open_lock_fd(path)
        opened.append(fd)
        return fd

    def stale_fstat(fd: int) -> os.stat_result:
        if fd in opened:
            raise OSError(errno.ESTALE, os.strerror(errno.ESTALE))
        return real_fstat(fd)

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    with monkeypatch.context() as patch:
        patch.setattr(runner, "_open_lock_fd", recording_open)
        patch.setattr(os, "fstat", stale_fstat)
        patch.setattr(os, "close", recording_close)
        exit_code = runner.main(
            ["--reference-time", "2026-09-08T12:00:00Z", "--summary-path", str(summary)]
        )
    capsys.readouterr()

    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["status"] == "completed"
    assert payload["counts"]["failed"] == 1
    assert payload["failed"] == [
        {
            "path": str(lock_path),
            "kind": "lock",
            "error": payload["failed"][0]["error"],
            "error_type": "OSError",
        }
    ]
    assert f"[Errno {errno.ESTALE}]" in payload["failed"][0]["error"]
    # The descriptor is closed on this path too, and the lock file survives.
    assert len(opened) == 1
    assert opened[0] in closed
    assert lock_path.exists()
    # The other targets are still processed.
    assert _paths(payload["deleted"]) == [str(aged_pbf)]
    assert not aged_pbf.exists()


def test_a_post_lock_identity_check_error_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `lstat` recheck runs only after the `flock` succeeded; an `OSError`
    other than ENOENT there is `failed`, and nothing is unlinked."""
    root = tmp_path / "cache"
    lock_path = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    calls: list[str] = []
    real_flock = fcntl.flock
    real_lstat = os.lstat

    def recording_flock(fd: int, operation: int) -> None:
        calls.append("flock")
        real_flock(fd, operation)

    def failing_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if str(path) == str(lock_path):
            calls.append("lstat")
            raise OSError(errno.EIO, os.strerror(errno.EIO), str(path))
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fcntl, "flock", recording_flock)
        patch.setattr(os, "lstat", failing_lstat)
        payload = runner.run_retention(_config(root), now=_NOW)

    assert calls == ["flock", "lstat"]
    assert payload["failed"] == [
        {
            "path": str(lock_path),
            "kind": "lock",
            "error": payload["failed"][0]["error"],
            "error_type": "OSError",
        }
    ]
    assert f"[Errno {errno.EIO}]" in payload["failed"][0]["error"]
    assert payload["deleted"] == []
    assert payload["skipped"] == []
    assert lock_path.exists()


def test_a_lock_file_removed_while_the_runner_holds_it_is_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external `rm` between the identity recheck and the unlink: the final
    `unlink` gets ENOENT, which is `already_gone`, never `failed`."""
    root = tmp_path / "cache"
    lock_path = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    real_unlink = os.unlink
    raced: list[str] = []

    def raced_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path) == str(lock_path):
            raced.append(str(path))
            real_unlink(path)
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", raced_unlink)
        payload = runner.run_retention(_config(root), now=_NOW)

    assert raced == [str(lock_path)]
    assert payload["skipped"] == [
        {"path": str(lock_path), "kind": "lock", "reason": "already_gone"}
    ]
    assert payload["failed"] == []
    assert payload["deleted"] == []
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# Scenario: a symlinked / missing `.locks` retires only the lock lane
# ---------------------------------------------------------------------------
def test_a_symlinked_locks_directory_retires_only_the_lock_lane(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    outside = tmp_path / "outside-locks"
    stray_lock = _touch(outside / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    aged_pbf = _touch(root / "ab" / f"{_SHA_B}.pbf", mtime=_AGED)
    (root / ".locks").symlink_to(outside, target_is_directory=True)

    payload = runner.run_retention(_config(root), now=_NOW)

    assert _paths(payload["deleted"]) == [str(aged_pbf)]
    assert payload["skipped"] == [
        {
            "path": str(root / ".locks"),
            "kind": None,
            "reason": "locks_root_unsafe",
            "detail": "path_is_symlink",
        }
    ]
    assert stray_lock.exists()
    assert not aged_pbf.exists()


def test_a_locks_path_that_is_a_regular_file_retires_only_the_lock_lane(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_B}.pbf", mtime=_AGED)
    _touch(root / ".locks", mtime=_AGED, content=b"not a directory")

    payload = runner.run_retention(_config(root), now=_NOW)

    assert _paths(payload["deleted"]) == [str(aged_pbf)]
    assert payload["skipped"][0]["detail"] == "path_not_directory"
    assert payload["skipped"][0]["reason"] == "locks_root_unsafe"
    assert (root / ".locks").is_file()


def test_a_missing_locks_directory_is_a_benign_lane_skip(tmp_path: Path) -> None:
    """A cache root that has not served a miss yet has no `.locks`; the tile
    lane must still prune, and this must not be an error."""
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)

    payload = runner.run_retention(_config(root), now=_NOW)

    assert _paths(payload["deleted"]) == [str(aged_pbf)]
    assert payload["skipped"] == [
        {"path": str(root / ".locks"), "kind": None, "reason": "locks_root_missing"}
    ]
    assert payload["counts"]["failed"] == 0


def test_a_symlinked_hex_directory_is_never_enumerated(tmp_path: Path) -> None:
    """`DirEntry.is_dir()` follows symlinks by default; `<root>/ab -> outside`
    must not carry the deletion surface out of the cache root."""
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    victim = _touch(outside / f"{_SHA_A}.pbf", mtime=_AGED)
    root.mkdir(parents=True)
    (root / "ab").symlink_to(outside, target_is_directory=True)

    payload = runner.run_retention(_config(root), now=_NOW)

    assert payload["counts"]["planned"] == 0
    assert victim.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 directory anyway")
def test_an_unreadable_hex_directory_fails_the_run_and_the_siblings_still_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unlistable `<hh>` hides an unknown number of aged files.

    Reporting it as a skip (or as nothing at all) would leave `counts.failed`
    at 0 and rc at 0, and the env template's health criterion
    (`.failed | length == 0`) would stay GREEN over a cache root that has
    silently stopped being pruned.
    """
    root = tmp_path / "cache"
    unreadable = root / "ab"
    hidden_pbf = _touch(unreadable / f"{_SHA_A}.pbf", mtime=_AGED)
    sibling_pbf = _touch(root / "cd" / f"{_SHA_B}.pbf", mtime=_AGED)
    (root / ".locks").mkdir()
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    unreadable.chmod(0o000)
    try:
        monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_PLAN_ONLY", "true")
        plan_rc = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
        plan_payload = json.loads(capsys.readouterr().out)
        monkeypatch.delenv("NODE27_MVT_CACHE_RETENTION_PLAN_ONLY")
        exit_code = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
        payload = json.loads(capsys.readouterr().out)
    finally:
        unreadable.chmod(0o755)

    expected_failure = {
        "path": str(unreadable),
        "kind": None,
        "reason": "enumeration_unavailable",
        "error": payload["failed"][0]["error"],
        "error_type": "PermissionError",
    }
    assert exit_code == 1
    assert payload["status"] == "completed"
    assert payload["failed"] == [expected_failure]
    assert set(payload["failed"][0]) == _ENUMERATION_FAILURE_KEYS
    assert payload["failed"][0]["error"]
    assert payload["counts"]["failed"] == 1
    # The readable lanes did their work anyway; one bad directory is not a halt.
    assert _paths(payload["deleted"]) == [str(sibling_pbf)]
    assert not sibling_pbf.exists()
    assert hidden_pbf.exists()
    # `plan_only` is not an excuse either: a plan that could not read a
    # directory is as incomplete as an execution that could not.
    assert plan_rc == 1
    assert plan_payload["execution_mode"] == "plan_only"
    assert plan_payload["deleted"] == []
    assert plan_payload["counts"]["planned"] == 1
    assert [
        {key: entry[key] for key in ("path", "kind", "reason", "error_type")}
        for entry in plan_payload["failed"]
    ] == [{key: expected_failure[key] for key in ("path", "kind", "reason", "error_type")}]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 directory anyway")
def test_an_unreadable_cache_root_is_a_failure_not_a_clean_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preflight passes -- `is_dir()` only needs the PARENT's permissions -- so
    the whole run would otherwise report `completed` with zero of everything."""
    root = tmp_path / "cache"
    survivor = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    root.chmod(0o000)
    try:
        exit_code = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
        payload = json.loads(capsys.readouterr().out)
    finally:
        root.chmod(0o755)

    assert exit_code == 1
    assert payload["status"] == "completed"
    assert payload["failed"] == [
        {
            "path": str(root),
            "kind": None,
            "reason": "enumeration_unavailable",
            "error": payload["failed"][0]["error"],
            "error_type": "PermissionError",
        }
    ]
    assert set(payload["failed"][0]) == _ENUMERATION_FAILURE_KEYS
    assert payload["counts"]["planned"] == 0
    assert payload["counts"]["deleted"] == 0
    # The lock lane never reaches an `os.scandir`: `<root>/.locks` cannot even
    # be `lstat`-ed through an unreadable parent, so it retires itself through
    # the existing lane skip instead of a second `failed[]` entry. rc is
    # already 1 from the root entry, which is what the health criterion reads.
    assert payload["skipped"] == [
        {
            "path": str(root / ".locks"),
            "kind": None,
            "reason": "locks_root_unsafe",
            "detail": "path_unavailable",
            "error": payload["skipped"][0]["error"],
        }
    ]
    assert survivor.exists()
