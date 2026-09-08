"""Issue #2032 -- lifecycle of the MVT tile generation lock file.

Governing invariant (design.md Invariant Matrix): every file the display API
creates under `NHMS_MVT_FILE_CACHE_DIR` outside `precip/` has a bounded life.
For the lock lane that means the `.lock` file exists ONLY while one in-flight
cache miss holds it, and cross-process single-flight survives that self-unlink.

`services.tiles.mvt` is imported as a MODULE, never `from ... import
_open_live_lock_file`: the red-proof run of this file happens against the
pre-change source, where those names do not exist yet, and a top-level
`from`-import would turn a behavioural red into a collection ImportError that
also fails the two tests which are supposed to be green before the change.
"""

from __future__ import annotations

import logging
import os
import sys
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import pytest

import services.tiles.mvt as mvt

_MARKER_TIMEOUT_SECONDS = 30.0


def _tile(valid_time: str = "2026-09-01T00:00:00Z") -> mvt.TileInput:
    return mvt.TileInput(
        layer_id="hydro-national",
        source_id="gfs",
        source_version="v1",
        valid_time=valid_time,
        z=4,
        x=13,
        y=6,
    )


def _open_fd_count() -> int:
    """Open descriptors of this process, on Linux CI and on a macOS worktree."""
    fd_dir = "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"
    return len(os.listdir(fd_dir))


# ---------------------------------------------------------------------------
# (a) The lock file does not outlive the miss -- normal return and producer
#     exception (the 424/413/500 paths of
#     apps/api/routes/hydro_display.py::_cached_or_generated_mvt_response all
#     raise INSIDE this block).
# ---------------------------------------------------------------------------
def test_lock_file_is_gone_after_a_normal_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    tile = _tile()
    key = mvt.cache_key(tile)
    lock_path = mvt._file_cache_lock_path(key)
    assert lock_path is not None

    with mvt.tile_generation_lock(tile):
        assert lock_path.exists(), "the lock file must exist while the miss is in flight"

    assert not lock_path.exists()
    assert list(lock_path.parent.iterdir()) == []


def test_lock_file_is_gone_after_the_producer_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A producer that raises 424/413/500 must not leak its lock file.

    The `_LOCAL_TILE_LOCKS` assertion below is deliberately OUTSIDE the
    `pytest.raises` scope and the exception is deliberately NOT bound with
    `as`: a live traceback pins the context manager's generator frame, which
    holds the only strong reference to the `threading.Lock` value, so the
    `WeakValueDictionary` entry would still be alive inside that scope. That
    half of this test pins EXISTING behaviour (`_LOCAL_TILE_LOCKS` is already a
    `weakref.WeakValueDictionary`) and is green before this change; only the
    lock-file assertion is new.
    """
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    tile = _tile("2026-09-01T03:00:00Z")
    key = mvt.cache_key(tile)
    lock_path = mvt._file_cache_lock_path(key)
    assert lock_path is not None

    with pytest.raises(RuntimeError):
        with mvt.tile_generation_lock(tile):
            assert lock_path.exists()
            raise RuntimeError("producer failed the way a 424 does")

    assert not lock_path.exists()
    assert key not in mvt._LOCAL_TILE_LOCKS


def test_repeated_misses_for_one_key_leave_no_lock_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Steady state: `.locks/**` holds nothing once the misses are done."""
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    tile = _tile("2026-09-01T06:00:00Z")

    for _ in range(3):
        with mvt.tile_generation_lock(tile):
            pass

    locks_root = tmp_path / ".locks"
    remaining = [path for path in locks_root.rglob("*") if path.is_file()]
    assert remaining == []


# ---------------------------------------------------------------------------
# (e) No cache directory configured -> unchanged no-file-lock branch.
# ---------------------------------------------------------------------------
def test_unconfigured_cache_dir_still_yields_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(mvt.MVT_FILE_CACHE_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    entered = False

    with mvt.tile_generation_lock(_tile("2026-09-01T09:00:00Z")):
        entered = True

    assert entered
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# (c) Identity recheck -- the deterministic, in-process oracle.
# ---------------------------------------------------------------------------
def test_reacquire_reopens_when_the_path_names_a_different_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One mismatching probe -> exactly one reopen, then the LIVE inode.

    This is the deterministic half of the inode-recheck evidence: the spawn
    test below cannot force a waiter to be blocked at the moment the holder
    unlinks, but here the probe sequence is fixed, so the recheck branch is
    guaranteed to execute.
    """
    lock_path = tmp_path / "ab" / ("c" * 8 + ".lock")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(b"")
    real_identity = mvt._lock_path_identity
    calls: list[tuple[int, int] | None] = []

    def probe(path: Path) -> tuple[int, int] | None:
        answer = real_identity(path)
        calls.append(answer)
        if len(calls) == 1:
            # Whatever the descriptor holds, it is not this.
            return (-1, -1)
        return answer

    monkeypatch.setattr(mvt, "_lock_path_identity", probe)

    lock_file = mvt._open_live_lock_file(lock_path)

    assert lock_file is not None
    try:
        assert len(calls) == 2
        assert mvt._lock_file_identity(lock_file.fileno()) == real_identity(lock_path)
    finally:
        lock_file.close()


# ---------------------------------------------------------------------------
# (d) Retry exhaustion: bounded, warned, no leaked descriptor, block still runs,
#     and the path is NOT unlinked (it may be someone else's live lock).
# ---------------------------------------------------------------------------
def test_retry_exhaustion_runs_the_block_without_the_cross_process_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    tile = _tile("2026-09-02T00:00:00Z")
    key = mvt.cache_key(tile)
    lock_path = mvt._file_cache_lock_path(key)
    assert lock_path is not None
    calls: list[Path] = []

    def never_matches(path: Path) -> tuple[int, int] | None:
        calls.append(path)
        return (-1, -1)

    monkeypatch.setattr(mvt, "_lock_path_identity", never_matches)
    entered = False
    before = _open_fd_count()

    with caplog.at_level(logging.WARNING, logger="services.tiles.mvt"):
        with mvt.tile_generation_lock(tile):
            entered = True

    after = _open_fd_count()

    assert entered
    assert len(calls) == mvt._TILE_LOCK_REACQUIRE_LIMIT
    assert after == before, "every failed attempt must close its own descriptor"
    assert any("without the cross-process lock" in record.getMessage() for record in caplog.records)
    # Give-up must not unlink: the path may already be another process's live
    # lock. The MVT retention runner ages it out instead.
    assert lock_path.exists()


# ---------------------------------------------------------------------------
# (b) + (c) Cross-process contention through a real `flock`, spawn context.
# ---------------------------------------------------------------------------
def _hold_lock_until_released(
    cache_root: str,
    valid_time: str,
    marker_root: str,
    ready_queue: Any,
    release_event: Any,
    result_queue: Any,
) -> None:
    """Process A: enter the lock, announce it, hold until the parent releases."""
    try:
        os.environ["NHMS_MVT_FILE_CACHE_DIR"] = cache_root
        import services.tiles.mvt as child_mvt

        tile = child_mvt.TileInput(
            layer_id="hydro-national",
            source_id="gfs",
            source_version="v1",
            valid_time=valid_time,
            z=4,
            x=13,
            y=6,
        )
        key = child_mvt.cache_key(tile)
        lock_path = child_mvt._file_cache_lock_path(key)
        assert lock_path is not None
        markers = Path(marker_root)
        with child_mvt.tile_generation_lock(tile):
            ready_queue.put({"role": "a", "state": "holding"})
            if not release_event.wait(_MARKER_TIMEOUT_SECONDS):
                raise TimeoutError("A timed out waiting for the parent's release")
            # Written INSIDE the block: B asserts it already exists when B's own
            # block starts, which is the no-overlap oracle (a marker file, not a
            # sleep).
            (markers / "a-done").write_text("done", encoding="utf-8")
        # Deliberately NOT `lock_path.exists()` here: B wakes at A's `LOCK_UN`
        # and RECREATES the path, so a post-block probe in A races B's reopen.
        # The parent's "`.locks/**` is empty after both joins" check is the
        # deterministic form of the same fact.
        result_queue.put({"ok": True, "role": "a"})
    except BaseException as error:  # pragma: no cover - reported to the parent
        result_queue.put({"ok": False, "role": "a", "error": repr(error)})


def _enter_lock_after_a(
    cache_root: str,
    valid_time: str,
    marker_root: str,
    ready_queue: Any,
    result_queue: Any,
) -> None:
    """Process B: contend for the same key and report what it observed."""
    try:
        os.environ["NHMS_MVT_FILE_CACHE_DIR"] = cache_root
        import services.tiles.mvt as child_mvt

        tile = child_mvt.TileInput(
            layer_id="hydro-national",
            source_id="gfs",
            source_version="v1",
            valid_time=valid_time,
            z=4,
            x=13,
            y=6,
        )
        key = child_mvt.cache_key(tile)
        lock_path = child_mvt._file_cache_lock_path(key)
        assert lock_path is not None
        markers = Path(marker_root)
        # Record what the acquisition probes saw. The identity B ends up
        # HOLDING is the assertable fact; how many attempts it took depends on
        # whether B was already blocked when A unlinked, so the parent only
        # reports that number.
        # `getattr` with a default, not attribute access: on the RED-PROOF run
        # these helpers do not exist yet, and an AttributeError here would turn
        # a clean assertion red into a 30s parent-side queue timeout. Missing
        # helpers simply leave `held` empty, which reds `holds_live_inode`.
        real_file_identity = getattr(child_mvt, "_lock_file_identity", None)
        real_path_identity = getattr(child_mvt, "_lock_path_identity", None)
        held: list[tuple[int, int]] = []
        probes: list[Any] = []

        if real_file_identity is not None and real_path_identity is not None:

            def record_file_identity(fd: int) -> tuple[int, int]:
                answer = real_file_identity(fd)
                held.append(answer)
                return answer

            def record_path_identity(path: Any) -> Any:
                answer = real_path_identity(path)
                probes.append(answer)
                return answer

            child_mvt._lock_file_identity = record_file_identity
            child_mvt._lock_path_identity = record_path_identity
        ready_queue.put({"role": "b", "state": "about-to-lock"})
        with child_mvt.tile_generation_lock(tile):
            saw_a_done = (markers / "a-done").exists()
            info = os.stat(lock_path)
            live = (info.st_dev, info.st_ino)
        result_queue.put(
            {
                "ok": True,
                "role": "b",
                "saw_a_done": saw_a_done,
                # B holds the inode the PATH names, never an orphan A unlinked.
                "holds_live_inode": bool(held) and held[-1] == live,
                "probe_calls": len(probes),
            }
        )
    except BaseException as error:  # pragma: no cover - reported to the parent
        result_queue.put({"ok": False, "role": "b", "error": repr(error)})


def test_two_processes_contending_for_one_key_stay_single_flight(tmp_path: Path) -> None:
    """Spawn, not threads: only a separate process exercises the `flock`.

    Sequencing: A enters and blocks on `release_event`; the parent starts B only
    after A is in; B announces itself immediately before entering the lock and
    only then is A released. The oracle is the `a-done` marker B reads INSIDE
    its own block -- never a sleep.
    """
    cache_root = tmp_path / "cache"
    markers = tmp_path / "markers"
    cache_root.mkdir()
    markers.mkdir()
    valid_time = "2026-09-03T00:00:00Z"
    context = get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    release_event = context.Event()

    process_a = context.Process(
        target=_hold_lock_until_released,
        args=(str(cache_root), valid_time, str(markers), ready_queue, release_event, result_queue),
    )
    process_b = context.Process(
        target=_enter_lock_after_a,
        args=(str(cache_root), valid_time, str(markers), ready_queue, result_queue),
    )
    process_a.start()
    try:
        announced_a = ready_queue.get(timeout=_MARKER_TIMEOUT_SECONDS)
        assert announced_a == {"role": "a", "state": "holding"}
        process_b.start()
        try:
            announced_b = ready_queue.get(timeout=_MARKER_TIMEOUT_SECONDS)
            assert announced_b == {"role": "b", "state": "about-to-lock"}
            release_event.set()
            results = {
                entry["role"]: entry
                for entry in (
                    result_queue.get(timeout=_MARKER_TIMEOUT_SECONDS),
                    result_queue.get(timeout=_MARKER_TIMEOUT_SECONDS),
                )
            }
        finally:
            process_b.join(_MARKER_TIMEOUT_SECONDS)
    finally:
        release_event.set()
        process_a.join(_MARKER_TIMEOUT_SECONDS)

    assert results["a"]["ok"], results["a"]
    assert results["b"]["ok"], results["b"]
    assert results["b"]["saw_a_done"] is True, "B's block started before A's ended"
    # B never serialised on an orphan: the descriptor it held is the inode the
    # PATH named. Deliberately NOT "B's inode number differs from A's" -- ext4
    # hands the just-freed number straight back on the next create in the same
    # directory, so that comparison is a false red on the Linux oracles while
    # passing on APFS. Whether B needed a reopen is timing-dependent and is
    # reported, not asserted (the deterministic reopen oracle is
    # `test_reacquire_reopens_when_the_path_names_a_different_inode`).
    assert results["b"]["holds_live_inode"] is True
    assert results["b"]["probe_calls"] >= 1
    assert process_a.exitcode == 0
    assert process_b.exitcode == 0
    remaining = [path for path in (cache_root / ".locks").rglob("*") if path.is_file()]
    assert remaining == []
