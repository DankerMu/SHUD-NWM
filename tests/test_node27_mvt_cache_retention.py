"""Issue #2032 -- MVT tile file-cache retention runner, wrapper and units.

Governing invariant (design.md Invariant Matrix): every file the display API
creates under `NHMS_MVT_FILE_CACHE_DIR` outside `precip/` has a bounded life,
and this runner touches EXACTLY three path shapes to give it one -- never
`precip/**`, never a symlink, never a directory wearing a `.pbf` name, never a
level deeper than `<root>/<hh>/`.
"""

from __future__ import annotations

import ast
import fcntl
import json
import os
import shutil
import stat
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import services.tiles.mvt as mvt
from scripts import node27_mvt_cache_retention as runner
from scripts import node27_raw_retention

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WRAPPER_PATH = _REPO_ROOT / "scripts/node27_mvt_cache_retention_once.sh"
_SERVICE_PATH = _REPO_ROOT / "infra/systemd/nhms-node27-mvt-cache-retention.service"
_TIMER_PATH = _REPO_ROOT / "infra/systemd/nhms-node27-mvt-cache-retention.timer"
_ENV_EXAMPLE_PATH = _REPO_ROOT / "infra/env/node27-mvt-cache-retention.example"

_NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
_AGED = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC).timestamp()
_FRESH = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC).timestamp()

_SHA_A = "a" + "0" * 63
_SHA_B = "b" + "1" * 63
_SHA_C = "c" + "2" * 63
_SHA_D = "d" + "3" * 63
_SHA_E = "e" + "4" * 63

_ENV_NAMES = (
    "NHMS_MVT_FILE_CACHE_DIR",
    "NODE27_MVT_CACHE_RETENTION_DAYS",
    "NODE27_MVT_CACHE_RETENTION_ENABLED",
    "NODE27_MVT_CACHE_RETENTION_PLAN_ONLY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _touch(path: Path, *, mtime: float, content: bytes = b"tile") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))
    return path


def _config(root: Path, **overrides: Any) -> runner.MvtCacheRetentionConfig:
    defaults: dict[str, Any] = {
        "cache_root": root,
        "retention_days": 14,
        "summary_path": None,
    }
    defaults.update(overrides)
    return runner.MvtCacheRetentionConfig(**defaults)


def _paths(entries: list[dict[str, Any]]) -> list[str]:
    return [str(entry["path"]) for entry in entries]


def _reasons(entries: list[dict[str, Any]]) -> set[str]:
    return {str(entry["reason"]) for entry in entries}


def _all_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["planned"] + payload["deleted"] + payload["failed"] + payload["skipped"]


def _code_only(text: str) -> str:
    """Source with docstrings and `#` comments stripped, so a prose mention of a
    flag cannot satisfy (or break) a source-level pin."""
    lines = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        lines.append(stripped)
    body = "\n".join(lines)
    parts = body.split('"""')
    return "".join(parts[::2])


# ---------------------------------------------------------------------------
# Scenario: aged tile, intermediate and lock files are pruned; all else survives
# ---------------------------------------------------------------------------
def _mixed_tree(root: Path) -> dict[str, Path]:
    """One cache root holding the three targets and every near-miss decoy."""
    tree = {
        "aged_pbf": _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED),
        "aged_tmp": _touch(root / "ab" / f".{_SHA_B}.pbf.123.tmp", mtime=_AGED),
        "aged_lock": _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b""),
        # Decoys, every one of them aged unless the name says otherwise.
        "fresh_pbf": _touch(root / "cd" / f"{_SHA_C}.pbf", mtime=_FRESH),
        "precip_png": _touch(root / "precip" / "IFS" / "2026060100" / "x.png", mtime=_AGED),
        "non_hex_dir_pbf": _touch(root / "zz" / f"{_SHA_A}.pbf", mtime=_AGED),
        "unshaped_name": _touch(root / "ab" / "notes.txt", mtime=_AGED),
        "too_deep": _touch(root / "ab" / "cd" / f"{_SHA_E}.pbf", mtime=_AGED),
        "pbf_in_locks": _touch(root / ".locks" / "ab" / f"{_SHA_C}.pbf", mtime=_AGED),
        "lock_in_tile_lane": _touch(root / "ab" / f"{_SHA_D}.lock", mtime=_AGED),
        "root_level_pbf": _touch(root / f"{_SHA_A}.pbf", mtime=_AGED),
    }
    # A symlink wearing a target name, pointing outside the cache root.
    outside = root.parent / "outside.pbf"
    _touch(outside, mtime=_AGED)
    symlink = root / "ab" / f"{_SHA_C}.pbf"
    symlink.symlink_to(outside)
    tree["symlink_pbf"] = symlink
    tree["symlink_victim"] = outside
    # A DIRECTORY wearing a target name.
    directory = root / "ab" / f"{_SHA_D}.pbf"
    directory.mkdir()
    os.utime(directory, (_AGED, _AGED))
    tree["directory_pbf"] = directory
    return tree


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


# ---------------------------------------------------------------------------
# Scenario: a directory that cannot be enumerated is a FAILURE, never silence
# ---------------------------------------------------------------------------
_ENUMERATION_FAILURE_KEYS = {"path", "kind", "reason", "error", "error_type"}


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


# ---------------------------------------------------------------------------
# Scenario: missing or unsafe cache root blocks before any deletion
# ---------------------------------------------------------------------------
def _blocked_fields(payload: dict[str, Any]) -> set[tuple[str, str]]:
    return {(str(entry["field"]), str(entry["reason"])) for entry in payload["blockers"]}


def test_unset_cache_root_blocks(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = runner.main([])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["status"] == "preflight_blocked"
    assert payload["execution_mode"] == "preflight_blocked"
    assert ("cache_root", "missing") in _blocked_fields(payload)
    assert payload["counts"] == {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0}


@pytest.mark.parametrize(
    "kind, reason",
    [
        ("relative", "path_not_absolute"),
        ("root", "path_is_root"),
        ("missing", "path_missing"),
        ("symlink", "path_is_symlink"),
        ("dangling_symlink", "path_is_symlink"),
        ("file", "path_not_directory"),
    ],
)
def test_unsafe_cache_roots_block_before_any_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    reason: str,
) -> None:
    real_root = tmp_path / "cache"
    aged_pbf = _touch(real_root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    if kind == "relative":
        value = "relative/cache"
    elif kind == "root":
        value = "/"
    elif kind == "missing":
        value = str(tmp_path / "absent")
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(real_root, target_is_directory=True)
        value = str(link)
    elif kind == "dangling_symlink":
        link = tmp_path / "dangling"
        link.symlink_to(tmp_path / "never-existed")
        value = str(link)
    else:
        value = str(_touch(tmp_path / "cache.txt", mtime=_AGED))
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", value)

    exit_code = runner.main([])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert ("cache_root", reason) in _blocked_fields(payload)
    assert aged_pbf.exists(), "a blocked preflight must delete nothing"


@pytest.mark.parametrize(
    "argv, env, field, reason",
    [
        (["--retention-days", "0"], {}, "retention_days", "must_be_at_least_one"),
        (["--retention-days", "-3"], {}, "retention_days", "must_be_at_least_one"),
        (["--retention-days", "seven"], {}, "retention_days", "not_an_integer"),
        (
            [],
            {"NODE27_MVT_CACHE_RETENTION_DAYS": "0"},
            "NODE27_MVT_CACHE_RETENTION_DAYS",
            "must_be_at_least_one",
        ),
        (
            [],
            {"NODE27_MVT_CACHE_RETENTION_DAYS": "fourteen"},
            "NODE27_MVT_CACHE_RETENTION_DAYS",
            "not_an_integer",
        ),
        (["--reference-time", "2026/09/08"], {}, "reference_time", "not_rfc3339"),
        (["--reference-time", "2026-09-08T12:00:00"], {}, "reference_time", "not_rfc3339"),
    ],
)
def test_malformed_age_or_reference_time_blocks_without_falling_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    env: dict[str, str],
    field: str,
    reason: str,
) -> None:
    """The explicit departure from `node27_raw_retention._env_int`: no silent
    fallback to the 14-day default, and `--retention-days 0` is not `or`-ed away."""
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    exit_code = runner.main(argv)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert (field, reason) in _blocked_fields(payload)
    assert aged_pbf.exists()


def test_a_blocked_run_writes_its_receipt_to_the_summary_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same sink a completed run would use, or the operator reads a stale file."""
    summary_path = tmp_path / "logs" / "summary.json"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path / "absent"))

    exit_code = runner.main(["--summary-path", str(summary_path)])
    capsys.readouterr()

    assert exit_code == 2
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["status"] == "preflight_blocked"
    assert ("cache_root", "path_missing") in _blocked_fields(payload)


# ---------------------------------------------------------------------------
# Requirement 2: summary schema, gates and exit codes
# ---------------------------------------------------------------------------
def test_disabled_gate_yields_a_disabled_summary_with_zero_deletions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_ENABLED", "false")

    exit_code = runner.main([])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["execution_mode"] == "disabled"
    assert payload["status"] == "disabled"
    assert payload["counts"] == {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0}
    assert payload["freed_bytes"] == 0
    assert aged_pbf.exists()


def test_env_gates_are_parsed_into_the_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_ENABLED", "false")
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_PLAN_ONLY", "true")
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_DAYS", "3")

    config, blockers = runner.config_from_env(
        runner.build_parser().parse_args(["--summary-path", str(tmp_path / "s.json")])
    )

    assert blockers == []
    assert config is not None
    assert (config.enabled, config.plan_only, config.retention_days) == (False, True, 3)
    assert config.cache_root == root


def test_a_leftover_dry_run_variable_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1407: the gate is deliberately not named `*_DRY_RUN`."""
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_DRY_RUN", "true")
    try:
        config, _ = runner.config_from_env(runner.build_parser().parse_args([]))
    finally:
        monkeypatch.delenv("NODE27_MVT_CACHE_RETENTION_DRY_RUN", raising=False)

    assert config is not None
    assert config.plan_only is False


def test_summary_carries_every_required_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED, content=b"1234567")
    summary_path = tmp_path / "logs" / "summary.json"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))

    exit_code = runner.main(
        ["--summary-path", str(summary_path), "--reference-time", "2026-09-08T12:00:00Z"]
    )
    printed = json.loads(capsys.readouterr().out)
    payload = json.loads(summary_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert printed == payload, "the file and stdout must carry the same receipt"
    assert set(payload) >= {
        "schema_version",
        "started_at",
        "finished_at",
        "reference_time",
        "cache_root",
        "retention_days",
        "cutoff",
        "enabled",
        "plan_only",
        "execution_mode",
        "status",
        "counts",
        "planned",
        "deleted",
        "skipped",
        "failed",
        "freed_bytes",
        "precip_root_untouched",
    }
    assert payload["schema_version"] == "nhms.node27_mvt_cache_retention.production.v1"
    assert payload["reference_time"] == "2026-09-08T12:00:00Z"
    assert payload["cutoff"] == "2026-08-25T12:00:00Z"
    assert payload["retention_days"] == 14
    assert payload["cache_root"] == str(root)
    assert payload["precip_root_untouched"] == str(root / "precip")
    assert payload["freed_bytes"] == 7
    entry = payload["deleted"][0]
    assert entry == {
        "path": str(aged_pbf),
        "kind": "pbf",
        "size_bytes": 7,
        "mtime": "2026-08-01T00:00:00Z",
    }


def test_a_relative_summary_path_is_a_valid_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The receipt is a file this process CREATES, not a tree it deletes from,
    so the absoluteness `cache_root` needs buys nothing here."""
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.chdir(tmp_path)

    exit_code = runner.main(
        ["--summary-path", "logs/summary.json", "--reference-time", "2026-09-08T12:00:00Z"]
    )
    printed = json.loads(capsys.readouterr().out)
    payload = json.loads((tmp_path / "logs" / "summary.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert printed == payload
    assert _paths(payload["deleted"]) == [str(aged_pbf)]


def test_the_summary_path_env_variable_is_not_a_runner_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH` belongs to the WRAPPER, which
    resolves it and always passes the result as `--summary-path`. A second
    reader here would only be a way for the two to disagree."""
    root = tmp_path / "cache"
    _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    ghost = tmp_path / "ghost-logs" / "summary.json"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH", str(ghost))

    exit_code = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "completed"
    assert payload["counts"]["deleted"] == 1
    assert not ghost.parent.exists(), "stdout was the sink; the env var is inert"


def test_an_undeletable_target_is_a_failure_and_exit_code_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes, so the failure cannot be simulated")
    root = tmp_path / "cache"
    blocked_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    deletable = _touch(root / "cd" / f"{_SHA_B}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    (root / "ab").chmod(0o555)
    try:
        exit_code = runner.main([])
        payload = json.loads(capsys.readouterr().out)
    finally:
        (root / "ab").chmod(0o755)

    assert exit_code == 1
    assert payload["failed"] == [
        {
            "path": str(blocked_pbf),
            "kind": "pbf",
            "error": payload["failed"][0]["error"],
            "error_type": "PermissionError",
        }
    ]
    assert payload["failed"][0]["error"]
    assert _paths(payload["deleted"]) == [str(deletable)]
    assert blocked_pbf.exists()


def test_repeated_ticks_are_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _mixed_tree(root)

    first = runner.run_retention(_config(root), now=_NOW)
    second = runner.run_retention(_config(root), now=_NOW)

    assert first["counts"]["deleted"] == 3
    assert second["counts"] == {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0}


def test_the_cutoff_is_wall_clock_relative_to_the_reference_time(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    just_inside = _touch(
        root / "ab" / f"{_SHA_A}.pbf",
        mtime=datetime(2026, 8, 25, 11, 59, 59, tzinfo=UTC).timestamp(),
    )
    just_outside = _touch(
        root / "ab" / f"{_SHA_B}.pbf",
        mtime=datetime(2026, 8, 25, 12, 0, 1, tzinfo=UTC).timestamp(),
    )
    # The cutoff instant itself. The comparison is `st_mtime < cutoff`, so this
    # one SURVIVES; a `<=` would delete it and nothing else in this file notices.
    on_the_cutoff = _touch(
        root / "ab" / f"{_SHA_C}.pbf",
        mtime=datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC).timestamp(),
    )

    payload = runner.run_retention(_config(root, reference_time=_NOW), now=_NOW)

    assert _paths(payload["deleted"]) == [str(just_inside)]
    assert _paths(payload["planned"]) == [str(just_inside)]
    assert just_outside.exists()
    assert on_the_cutoff.exists()


# ---------------------------------------------------------------------------
# Shared-shape contract with services/tiles/mvt.py (both directions)
# ---------------------------------------------------------------------------
def test_the_three_patterns_match_the_paths_mvt_actually_produces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-source pin: the producer builds the paths, the runner's regexes must
    match them. Change the layout in `services/tiles/mvt.py` and this reds."""
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    tile = mvt.TileInput(
        layer_id="hydro-national",
        source_id="gfs",
        source_version="v1",
        valid_time="2026-09-01T00:00:00Z",
        z=4,
        x=13,
        y=6,
    )
    key = mvt.cache_key(tile)
    body_path = mvt._file_cache_path(key)
    lock_path = mvt._file_cache_lock_path(key)
    assert body_path is not None and lock_path is not None
    # The `.tmp` name `_write_file_cache` really leaves behind, observed rather
    # than reconstructed: the writer is driven and the intermediate captured.
    observed_tmp: list[str] = []
    real_replace = os.replace

    def capture(src: Any, dst: Any) -> None:
        observed_tmp.append(Path(src).name)
        real_replace(src, dst)

    # Scoped: `os.replace` is a process-global, and pyproject.toml's author
    # contract calls an unscoped global `os.*` patch a teardown hazard.
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", capture)
        assert mvt._write_file_cache(key, b"tile-bytes") is True

    assert runner.HEX_DIR_PATTERN.match(body_path.parent.name)
    assert runner.HEX_DIR_PATTERN.match(lock_path.parent.name)
    assert body_path.parent.parent == Path(tmp_path)
    assert lock_path.parent.parent == Path(tmp_path) / runner.LOCKS_DIR_NAME
    assert runner.PBF_PATTERN.match(body_path.name)
    assert runner.LOCK_PATTERN.match(lock_path.name)
    assert observed_tmp and runner.TMP_PATTERN.match(observed_tmp[0])
    # And no pattern matches the other lane's name.
    assert not runner.LOCK_PATTERN.match(body_path.name)
    assert not runner.PBF_PATTERN.match(lock_path.name)
    assert not runner.PBF_PATTERN.match(observed_tmp[0])


def test_a_real_mvt_cache_write_is_collected_by_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end on one root: the display API writes, the runner collects."""
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    tile = mvt.TileInput(
        layer_id="river-network-national",
        source_id="gfs",
        source_version="v1",
        valid_time="2026-09-01T00:00:00Z",
        z=6,
        x=50,
        y=25,
    )
    key = mvt.cache_key(tile)
    assert mvt._write_file_cache(key, b"tile-bytes") is True
    with mvt.tile_generation_lock(tile):
        lock_path = mvt._file_cache_lock_path(key)
        assert lock_path is not None
        os.utime(lock_path, (_AGED, _AGED))
        body = mvt._file_cache_path(key)
        assert body is not None
        os.utime(body, (_AGED, _AGED))
        targets, _, _ = runner.collect_targets(tmp_path, cutoff=datetime(2026, 8, 25, tzinfo=UTC))

    assert sorted(target.kind for target in targets) == ["lock", "pbf"]
    assert {str(target.path) for target in targets} == {str(body), str(lock_path)}


def test_the_two_runners_never_list_each_others_paths(tmp_path: Path) -> None:
    """Mirror-image exclusion on ONE shared root (spec Requirement 5)."""
    root = tmp_path / "cache"
    precip_cycle = root / "precip" / "IFS" / "2026060100"
    precip_cycle.mkdir(parents=True)
    png = precip_cycle / "2026-06-01T03:00:00Z.cma24h6-abcdef01.0123456789ab.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    os.utime(precip_cycle, (_AGED, _AGED))
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    aged_lock = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")

    object_store = tmp_path / "object-store"
    (object_store / "raw").mkdir(parents=True)
    raw_payload = node27_raw_retention.run_retention(
        node27_raw_retention.RawRetentionConfig(
            object_store_root=object_store,
            retention_days=14,
            sources=frozenset({"gfs", "ifs"}),
            summary_path=None,
            dry_run=True,
            precip_cache_root=root,
        ),
        now=_NOW,
    )
    mvt_payload = runner.run_retention(_config(root, plan_only=True), now=_NOW)

    raw_paths = _paths(raw_payload["planned"])
    mvt_paths = _paths(mvt_payload["planned"])
    assert raw_paths == [str(precip_cycle)]
    assert sorted(mvt_paths) == sorted([str(aged_pbf), str(aged_lock)])
    assert not [path for path in raw_paths if not path.startswith(str(root / "precip"))]
    assert not [path for path in mvt_paths if path.startswith(str(root / "precip"))]
    assert set(raw_paths).isdisjoint(mvt_paths)


# ---------------------------------------------------------------------------
# Unit / timer / env template files
# ---------------------------------------------------------------------------
def test_mvt_cache_retention_service_bootstraps_log_dir() -> None:
    service_text = _SERVICE_PATH.read_text(encoding="utf-8")

    assert (
        "ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-mvt-cache-retention-logs" in service_text
    )
    assert (
        "StandardOutput=append:/home/nwm/node27-mvt-cache-retention-logs/systemd.log"
        in service_text
    )
    assert (
        "StandardError=append:/home/nwm/node27-mvt-cache-retention-logs/systemd.err"
        in service_text
    )
    assert "Type=oneshot" in service_text
    assert "WorkingDirectory=/home/nwm/NWM" in service_text
    assert (
        "Environment=NODE27_MVT_CACHE_RETENTION_ENV_FILE="
        "/home/nwm/NWM/infra/env/node27-mvt-cache-retention.env" in service_text
    )
    assert (
        "ExecStart=/home/nwm/NWM/scripts/node27_mvt_cache_retention_once.sh" in service_text
    )
    lines = service_text.splitlines()
    pre_index = next(i for i, line in enumerate(lines) if line.startswith("ExecStartPre="))
    start_index = next(i for i, line in enumerate(lines) if line.startswith("ExecStart="))
    assert pre_index < start_index


def test_mvt_cache_retention_timer_fires_daily_and_catches_up() -> None:
    timer_text = _TIMER_PATH.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 04:05:00 UTC" in timer_text
    assert "Persistent=true" in timer_text
    assert "Unit=nhms-node27-mvt-cache-retention.service" in timer_text
    assert "WantedBy=timers.target" in timer_text


def test_env_example_pins_the_display_process_cache_root_and_a_health_criterion() -> None:
    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt" in text
    assert "the value the display API PROCESS" in text
    assert "display.example" in text
    assert "NODE27_MVT_CACHE_RETENTION_DAYS=14" in text
    assert "NODE27_MVT_CACHE_RETENTION_LOG_ROOT=/home/nwm/node27-mvt-cache-retention-logs" in text
    assert "NODE27_MVT_CACHE_RETENTION_LOCK_PATH=/tmp/node27-mvt-cache-retention.lock" in text
    assert "# NODE27_MVT_CACHE_RETENTION_ENABLED=true" in text
    assert "# NODE27_MVT_CACHE_RETENTION_PLAN_ONLY=false" in text
    assert 'execution_mode == "production_execute"' in text
    assert "now - 26*3600" in text
    assert ".failed | length == 0" in text


def test_the_raw_retention_env_example_points_at_this_runner() -> None:
    sibling = (_REPO_ROOT / "infra/env/node27-raw-retention.example").read_text(encoding="utf-8")

    assert "the MVT tile cache is a sibling directory here" in sibling
    assert "scripts/node27_mvt_cache_retention.py" in sibling


# ---------------------------------------------------------------------------
# Wrapper contract (real bash subprocess)
# ---------------------------------------------------------------------------
def _wrapper_repo(tmp_path: Path, *, runner_rc: int = 0, with_python: bool = True) -> Path:
    """A stand-in repo: `.venv/bin/python` plays the runner, no real work."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "node27_mvt_cache_retention.py").write_text("", encoding="utf-8")
    if with_python:
        (repo / ".venv" / "bin").mkdir(parents=True)
        python_bin = repo / ".venv" / "bin" / "python"
        python_bin.write_text(
            "#!/bin/sh\n" 'echo "RUNNER_INVOKED args=$*"\n' f"exit {runner_rc}\n",
            encoding="utf-8",
        )
        python_bin.chmod(0o755)
    return repo


def _wrapper_env(tmp_path: Path, repo: Path, env_file: Path, *, bin_dir: Path | None = None) -> dict[str, str]:
    path = os.environ.get("PATH", "/usr/bin:/bin")
    if bin_dir is not None:
        path = f"{bin_dir}:{path}"
    return {
        "PATH": path,
        "NODE27_MVT_CACHE_RETENTION_REPO": str(repo),
        "NODE27_MVT_CACHE_RETENTION_ENV_FILE": str(env_file),
        "NODE27_MVT_CACHE_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        "NODE27_MVT_CACHE_RETENTION_LOG_ROOT": str(tmp_path / "logs"),
        "NODE27_MVT_CACHE_RETENTION_LOG_FILE": str(tmp_path / "logs" / "wrapper.log"),
        "NODE27_MVT_CACHE_RETENTION_LOCK_PATH": str(tmp_path / "wrapper.lock"),
        "NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH": str(tmp_path / "logs" / "summary.json"),
    }


def _flock_shim(tmp_path: Path, *, exit_code: int) -> Path:
    """macOS ships no `flock(1)`; without a shim every wrapper test would take
    the "previous run still active" branch and pass vacuously."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "flock"
    shim.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


def _run_wrapper(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPER_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _wrapper_output(tmp_path: Path, result: subprocess.CompletedProcess[str]) -> str:
    bootstrap = tmp_path / "bootstrap.log"
    log_file = tmp_path / "logs" / "wrapper.log"
    return "".join(
        [
            result.stdout,
            result.stderr,
            bootstrap.read_text(encoding="utf-8") if bootstrap.exists() else "",
            log_file.read_text(encoding="utf-8") if log_file.exists() else "",
        ]
    )


def test_wrapper_refuses_a_missing_env_file(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    result = _run_wrapper(_wrapper_env(tmp_path, repo, tmp_path / "absent.env"))

    assert result.returncode == 2
    combined = _wrapper_output(tmp_path, result)
    assert "ENV_FILE_MISSING" in combined
    assert "RUNNER_INVOKED" not in combined


def test_wrapper_refuses_a_symlinked_env_file(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    target = tmp_path / "real.env"
    target.write_text("", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "link.env"
    link.symlink_to(target)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, link))

    assert result.returncode == 2
    combined = _wrapper_output(tmp_path, result)
    assert "ENV_FILE_SYMLINK_FORBIDDEN" in combined
    assert "RUNNER_INVOKED" not in combined


def test_wrapper_refuses_a_world_readable_env_file(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o640)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, env_file))

    assert result.returncode == 2
    combined = _wrapper_output(tmp_path, result)
    assert "ENV_FILE_MODE_UNSAFE" in combined
    assert "RUNNER_INVOKED" not in combined


def test_wrapper_refuses_a_missing_interpreter(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path, with_python=False)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, env_file))

    assert result.returncode == 2
    combined = _wrapper_output(tmp_path, result)
    assert "PYTHON_EXECUTABLE_UNAVAILABLE" in combined


@pytest.mark.parametrize("runner_rc", [0, 1, 2])
def test_wrapper_propagates_the_runner_exit_code(tmp_path: Path, runner_rc: int) -> None:
    repo = _wrapper_repo(tmp_path, runner_rc=runner_rc)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    bin_dir = _flock_shim(tmp_path, exit_code=0)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, env_file, bin_dir=bin_dir))

    assert result.returncode == runner_rc
    log_text = (tmp_path / "logs" / "wrapper.log").read_text(encoding="utf-8")
    assert "RUNNER_INVOKED args=" in log_text
    assert "--summary-path" in log_text
    assert f"node27-mvt-cache-retention: done rc={runner_rc}" in log_text


def test_wrapper_skips_the_tick_when_the_lock_is_held(tmp_path: Path) -> None:
    """`flock -n` failing is a benign skip (rc 0), not a failure."""
    repo = _wrapper_repo(tmp_path)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    bin_dir = _flock_shim(tmp_path, exit_code=1)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, env_file, bin_dir=bin_dir))

    assert result.returncode == 0
    combined = _wrapper_output(tmp_path, result)
    assert "previous run still active, skipping tick" in combined
    assert "RUNNER_INVOKED" not in combined


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) is not installed (macOS)")
def test_wrapper_skips_the_tick_against_a_really_held_flock(tmp_path: Path) -> None:
    """The same skip, driven through the real `flock(1)` on the CI/node-27 lane."""
    repo = _wrapper_repo(tmp_path)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    env = _wrapper_env(tmp_path, repo, env_file)
    holder = os.open(env["NODE27_MVT_CACHE_RETENTION_LOCK_PATH"], os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run_wrapper(env)
    finally:
        os.close(holder)

    assert result.returncode == 0
    combined = _wrapper_output(tmp_path, result)
    assert "previous run still active, skipping tick" in combined
    assert "RUNNER_INVOKED" not in combined


def test_wrapper_sources_the_env_file_and_creates_the_log_root(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    env_file = tmp_path / "runner.env"
    env_file.write_text(
        f"NODE27_MVT_CACHE_RETENTION_LOG_ROOT={tmp_path / 'sourced-logs'}\n", encoding="utf-8"
    )
    env_file.chmod(0o600)
    bin_dir = _flock_shim(tmp_path, exit_code=0)
    env = _wrapper_env(tmp_path, repo, env_file, bin_dir=bin_dir)
    del env["NODE27_MVT_CACHE_RETENTION_LOG_ROOT"]
    del env["NODE27_MVT_CACHE_RETENTION_LOG_FILE"]
    del env["NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH"]

    result = _run_wrapper(env)

    assert result.returncode == 0, result.stderr
    sourced = tmp_path / "sourced-logs"
    assert sourced.is_dir()
    log_text = (sourced / "mvt-cache-retention.log").read_text(encoding="utf-8")
    assert "RUNNER_INVOKED args=" in log_text
    assert f"--summary-path {sourced}/mvt-cache-retention-" in log_text


def test_wrapper_never_opens_the_lock_file_with_o_creat_in_the_runner() -> None:
    """Source-level pin for the one flag whose absence a test cannot stage:
    a deleter that can recreate the file it removes is not a deleter."""
    text = (_REPO_ROOT / "scripts/node27_mvt_cache_retention.py").read_text(encoding="utf-8")

    assert "os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK" in text
    assert "os.O_CREAT" not in text
    assert "O_CREAT" not in _code_only(text)


def test_the_runner_never_imports_the_raw_retention_module() -> None:
    """D1: stdlib only, no DB. Importing the sibling would drag in psycopg and
    the watermark read this runner deliberately does not have."""
    text = (_REPO_ROOT / "scripts/node27_mvt_cache_retention.py").read_text(encoding="utf-8")

    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [name for name in imported if name.startswith(("scripts", "packages", "services", "apps"))]
    assert not [name for name in imported if name in {"psycopg2", "psycopg", "sqlalchemy"}]
    assert imported <= {
        "argparse",
        "errno",
        "fcntl",
        "json",
        "os",
        "re",
        "stat",
        "dataclasses",
        "datetime",
        "pathlib",
        "typing",
        "__future__",
    }


def test_collect_targets_reads_exactly_two_levels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resource bound: a fixed-depth enumeration, never a whole-tree walk."""
    root = tmp_path / "cache"
    _mixed_tree(root)
    scanned: list[str] = []
    real_scandir = os.scandir

    def recording_scandir(path: Any = ".") -> Any:
        scanned.append(str(path))
        return real_scandir(path)

    # Scoped for the same reason as the `os.replace` patch above: an unscoped
    # `os.scandir` patch stays installed through teardown, which is exactly the
    # hazard pyproject.toml's tmp_path retention note calls out.
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", recording_scandir)
        runner.collect_targets(root, cutoff=datetime(2026, 8, 25, tzinfo=UTC))

    assert sorted(scanned) == sorted(
        [
            str(root),
            str(root / "ab"),
            str(root / "cd"),
            str(root / ".locks"),
            str(root / ".locks" / "ab"),
        ]
    )
    assert str(root / "precip") not in scanned
    assert str(root / "ab" / "cd") not in scanned


def test_a_fifo_wearing_a_target_name_is_not_a_target(tmp_path: Path) -> None:
    """`lstat` must say REGULAR file, not merely "not a directory"."""
    root = tmp_path / "cache"
    (root / "ab").mkdir(parents=True)
    fifo = root / "ab" / f"{_SHA_A}.pbf"
    os.mkfifo(fifo)
    os.utime(fifo, (_AGED, _AGED))

    targets, _, _ = runner.collect_targets(root, cutoff=datetime(2026, 8, 25, tzinfo=UTC))

    assert targets == []
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
