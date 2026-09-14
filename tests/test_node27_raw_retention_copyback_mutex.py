"""Requirement-driven tests for node-27 raw retention's copyback mutex (#2252, #2239, #2262).

Contract, from `harden-copyback-mutex-residuals` (mutual-exclusion delta and the
node-27 raw-retention delta):

* on node-27 the object-store root IS the shared copyback root, so every
  `canonical/<storage-source>/<cycle>` removal holds the batch mutex, taken with
  the POSIX record-lock primitive, once per tree and spanning only its own
  `rmtree`; raw and precip-cache removals never acquire (EF-10);
* one pass-level wait budget bounds the sweep: the first blocked entry records
  `lock_timeout`, the rest `lock_budget_exhausted` without an attempt, the trees
  stay, other lanes still delete and the pass completes (EF-11);
* an unsafe, unopenable or not-yet-created (non-owner) lock file is
  `lock_unsafe`, the tree stays, no lock file is created (EF-12);
* the lock file itself is never a removal candidate (EF-12b);
* disabled, plan-only and preflight-blocked runs acquire nothing and still
  carry the zero `copyback_lock_failures` block (EF-13);
* the tolerated plan-to-delete window is pinned: a writer that commits into a
  planned tree before the pass acquires does not change what is removed (EF-15).

`lockf` is per PROCESS. The pass under test runs in this process and holds a
POSIX lock during each canonical `rmtree`; any probe or competing holder
opened here could not see that hold and its `close()` would silently drop it.
Every holder, probe and writer therefore runs in its own subprocess, with the
`posix` primitive, so no outcome depends on `flock`/`lockf` interaction.

Recording wrappers are installed with `raising=False` so a run against the
pre-change script fails on the behaviour assertions, not on the patch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.copyback_guard import COPYBACK_BATCH_LOCK_NAME
from scripts import node27_raw_retention

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 6, 27, 12, tzinfo=UTC)
REFERENCE_ARGS = ["--sources", "gfs,ifs", "--reference-time", "2026-06-27T12:00:00Z"]
ZERO_LOCK_FAILURES = {"lock_timeout": 0, "lock_unsafe": 0, "lock_budget_exhausted": 0}


def _store(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "object-store"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _raw_cycle(root: Path, source: str, cycle: str) -> Path:
    path = root / "raw" / source / cycle
    path.mkdir(parents=True)
    (path / "manifest.json").write_text("payload", encoding="utf-8")
    return path


def _canonical_cycle(root: Path, storage_source: str, cycle: str) -> Path:
    path = root / "canonical" / storage_source / cycle / "prcp_rate_or_amount"
    path.mkdir(parents=True)
    (path / f"{storage_source.lower()}_{cycle}_f003.nc").write_text("slice", encoding="utf-8")
    return path.parent


def _cache_cycle(cache_root: Path, storage_source: str, cycle: str) -> Path:
    path = cache_root / "precip" / storage_source / cycle
    path.mkdir(parents=True)
    (path / "frame.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


def _config(
    root: Path, *, cache: Path | None = None, enabled: bool = True, dry_run: bool = False
) -> node27_raw_retention.RawRetentionConfig:
    return node27_raw_retention.RawRetentionConfig(
        object_store_root=root,
        retention_days=14,
        sources=frozenset({"gfs", "ifs"}),
        summary_path=None,
        enabled=enabled,
        dry_run=dry_run,
        precip_cache_root=cache,
    )


def _keys(entries: list[dict[str, Any]]) -> list[str]:
    return [str(entry["key"]) for entry in entries]


def _production_env(monkeypatch: pytest.MonkeyPatch, *, store: Path, cache: Path | None = None) -> None:
    for name in (
        "NODE27_RAW_RETENTION_ENABLED",
        "NODE27_RAW_RETENTION_PLAN_ONLY",
        "NODE27_RAW_RETENTION_SUMMARY_PATH",
        "NODE27_RAW_RETENTION_DAYS",
        "NODE27_RAW_RETENTION_SOURCES",
        "NHMS_MVT_FILE_CACHE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    if cache is not None:
        monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(cache))
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(store))


def _main(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict[str, Any]]:
    exit_code = node27_raw_retention.main(argv)
    return exit_code, json.loads(capsys.readouterr().out)


def _record_lock_calls(monkeypatch: pytest.MonkeyPatch, events: list[tuple[Any, ...]]) -> None:
    """Record every acquire/release the script makes, calling through to the guard."""

    from packages.common import copyback_guard

    def acquire(root: Path | str, **kwargs: Any) -> int:
        events.append(("acquire", str(root), kwargs.get("primitive", "flock")))
        return copyback_guard.acquire_copyback_batch_lock(root, **kwargs)

    def release(fd: int, **kwargs: Any) -> None:
        events.append(("release", kwargs.get("primitive", "flock")))
        copyback_guard.release_copyback_batch_lock(fd, **kwargs)

    monkeypatch.setattr(node27_raw_retention, "acquire_copyback_batch_lock", acquire, raising=False)
    monkeypatch.setattr(node27_raw_retention, "release_copyback_batch_lock", release, raising=False)


def _forbid_acquisitions(monkeypatch: pytest.MonkeyPatch) -> None:
    """An acquisition becomes an `AssertionError`, which no retention `except` absorbs."""

    def forbidden(*args: object, **kwargs: object) -> int:
        raise AssertionError("this run must not acquire the copyback batch mutex")

    monkeypatch.setattr(node27_raw_retention, "acquire_copyback_batch_lock", forbidden, raising=False)


_HOLDER_PROGRAM = """
import sys
sys.path.insert(0, {repo!r})
from packages.common.copyback_guard import acquire_copyback_batch_lock
acquire_copyback_batch_lock({root!r}, timeout_seconds=10, primitive="posix")
print("held", flush=True)
sys.stdin.readline()
"""

_PROBE_PROGRAM = """
import sys
sys.path.insert(0, {repo!r})
from packages.common.copyback_guard import (
    CopybackLockTimeout, acquire_copyback_batch_lock, release_copyback_batch_lock,
)
try:
    fd = acquire_copyback_batch_lock({root!r}, timeout_seconds=0.2, primitive="posix")
except CopybackLockTimeout:
    print("held")
else:
    release_copyback_batch_lock(fd, primitive="posix")
    print("free")
"""

_WRITER_PROGRAM = """
import shutil, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from packages.common.copyback_guard import copyback_batch_lock
target = Path({target!r})
with copyback_batch_lock({root!r}, timeout_seconds=10, primitive="posix"):
    shutil.rmtree(target)
    (target / "prcp_rate_or_amount").mkdir(parents=True)
    (target / "prcp_rate_or_amount" / "rewritten.nc").write_text("new", encoding="utf-8")
print("committed", flush=True)
"""


def _python(program: str) -> list[str]:
    return [sys.executable, "-c", program]


def _start_holder(root: Path) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        _python(_HOLDER_PROGRAM.format(repo=str(REPO_ROOT), root=str(root))),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


def _stop(holder: subprocess.Popen[str]) -> None:
    if holder.poll() is None:
        holder.kill()
    holder.wait(timeout=10)


def _probe_from_another_process(root: Path) -> str:
    completed = subprocess.run(
        _python(_PROBE_PROGRAM.format(repo=str(REPO_ROOT), root=str(root))),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


# --- EF-10: one posix acquisition per canonical tree, none elsewhere -----------


def test_ef10_each_canonical_removal_holds_its_own_posix_acquisition_and_no_other_lane_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    cache = tmp_path.resolve() / "cache"
    _raw_cycle(store, "gfs", "2026060100")
    canonical = [_canonical_cycle(store, "IFS", cycle) for cycle in ("2026060100", "2026060112")]
    _cache_cycle(cache, "IFS", "2026060100")
    events: list[tuple[Any, ...]] = []
    _record_lock_calls(monkeypatch, events)
    real_rmtree = node27_raw_retention.shutil.rmtree

    def observing_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        # A real competing POSIX acquisition from ANOTHER process: "held" only
        # while this removal is inside the pass's own acquisition.
        events.append(("rmtree", Path(path).relative_to(store.parent).as_posix(), _probe_from_another_process(store)))
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(node27_raw_retention.shutil, "rmtree", observing_rmtree)

    result = node27_raw_retention.run_retention(_config(store, cache=cache), now=NOW)

    assert result["status"] == "completed"
    assert _keys(result["deleted"]) == [
        "raw/gfs/2026060100",
        "canonical/IFS/2026060100",
        "canonical/IFS/2026060112",
        "precip-cache/IFS/2026060100",
    ]
    assert result["failed"] == []
    assert result["copyback_lock_failures"] == ZERO_LOCK_FAILURES
    assert events == [
        ("rmtree", "object-store/raw/gfs/2026060100", "free"),
        ("acquire", str(store), "posix"),
        ("rmtree", "object-store/canonical/IFS/2026060100", "held"),
        ("release", "posix"),
        ("acquire", str(store), "posix"),
        ("rmtree", "object-store/canonical/IFS/2026060112", "held"),
        ("release", "posix"),
        ("rmtree", "cache/precip/IFS/2026060100", "free"),
    ]
    for tree in canonical:
        assert not tree.exists()
    # Released at the end, and the lock file is left in place (never unlinked).
    assert _probe_from_another_process(store) == "free"
    assert (store / COPYBACK_BATCH_LOCK_NAME).is_file()


def test_ef10_a_canonical_removal_waits_for_a_holder_in_another_process_then_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    tree = _canonical_cycle(store, "IFS", "2026060100")
    acquiring = threading.Event()
    real_acquire = node27_raw_retention.acquire_copyback_batch_lock

    def announcing_acquire(root: Path | str, **kwargs: Any) -> int:
        acquiring.set()
        return real_acquire(root, **kwargs)

    monkeypatch.setattr(node27_raw_retention, "acquire_copyback_batch_lock", announcing_acquire, raising=False)
    box: dict[str, Any] = {}
    holder = _start_holder(store)
    try:
        thread = threading.Thread(
            target=lambda: box.update(
                result=node27_raw_retention.run_retention(_config(store), now=NOW, copyback_lock_wait_budget_seconds=30)
            )
        )
        thread.start()
        assert acquiring.wait(timeout=10)
        time.sleep(0.3)
        # Still blocked on the other process's hold: nothing removed yet.
        assert (tree / "prcp_rate_or_amount").is_dir()
        assert thread.is_alive()
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=10)
        thread.join(timeout=30)
    finally:
        _stop(holder)

    assert not thread.is_alive()
    result = box["result"]
    assert result["failed"] == []
    assert _keys(result["deleted"]) == ["canonical/IFS/2026060100"]
    assert not tree.exists()


# --- EF-11: a holder past the budget -------------------------------------------


def test_ef11_a_holder_past_the_budget_times_out_once_then_exhausts_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    raw = _raw_cycle(store, "gfs", "2026060100")
    canonical = [_canonical_cycle(store, "IFS", cycle) for cycle in ("2026060100", "2026060112", "2026060200")]
    events: list[tuple[Any, ...]] = []
    _record_lock_calls(monkeypatch, events)

    holder = _start_holder(store)
    try:
        result = node27_raw_retention.run_retention(_config(store), now=NOW, copyback_lock_wait_budget_seconds=0.5)
    finally:
        _stop(holder)

    assert result["status"] == "completed"
    assert _keys(result["deleted"]) == ["raw/gfs/2026060100"]
    assert not raw.exists()
    shapes = [(entry["key"], entry["lock_failure"], entry["error_type"]) for entry in result["failed"]]
    assert shapes == [
        ("canonical/IFS/2026060100", "lock_timeout", "CopybackLockTimeout"),
        ("canonical/IFS/2026060112", "lock_budget_exhausted", "CopybackLockBudgetExhausted"),
        ("canonical/IFS/2026060200", "lock_budget_exhausted", "CopybackLockBudgetExhausted"),
    ]
    assert all(entry["error"] for entry in result["failed"])
    # The exhausted entries were refused before any attempt.
    assert [event for event in events if event[0] == "acquire"] == [("acquire", str(store), "posix")]
    assert result["counts"]["failed"] == 3
    assert result["copyback_lock_failures"] == {"lock_timeout": 1, "lock_unsafe": 0, "lock_budget_exhausted": 2}
    assert result["freed_bytes"] == sum(int(entry["size_bytes"]) for entry in result["deleted"])
    for tree in canonical:
        assert (tree / "prcp_rate_or_amount").is_dir()


def test_ef11_main_exits_one_when_the_budget_leaves_canonical_trees_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    _raw_cycle(store, "gfs", "2026060100")
    canonical = _canonical_cycle(store, "IFS", "2026060100")
    _production_env(monkeypatch, store=store)
    monkeypatch.setattr(node27_raw_retention, "DEFAULT_RETENTION_COPYBACK_LOCK_WAIT_BUDGET_SECONDS", 0.3, raising=False)

    holder = _start_holder(store)
    try:
        exit_code, payload = _main(capsys, REFERENCE_ARGS)
    finally:
        _stop(holder)

    assert exit_code == 1
    assert payload["schema_version"] == "nhms.node27_raw_retention.production.v5"
    assert payload["status"] == "completed"
    assert [entry["lock_failure"] for entry in payload["failed"]] == ["lock_timeout"]
    assert payload["copyback_lock_failures"]["lock_timeout"] == 1
    assert canonical.is_dir()


# --- EF-12: an unsafe, unopenable or foreign lock ------------------------------


def test_ef12_an_unsafe_lock_file_is_lock_unsafe_and_main_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    raw = _raw_cycle(store, "gfs", "2026060100")
    canonical = [_canonical_cycle(store, "IFS", cycle) for cycle in ("2026060100", "2026060112")]
    lock_file = store / COPYBACK_BATCH_LOCK_NAME
    lock_file.write_bytes(b"")
    os.chmod(lock_file, 0o644)
    _production_env(monkeypatch, store=store)

    exit_code, payload = _main(capsys, REFERENCE_ARGS)

    assert exit_code == 1
    assert [(entry["key"], entry["lock_failure"]) for entry in payload["failed"]] == [
        ("canonical/IFS/2026060100", "lock_unsafe"),
        ("canonical/IFS/2026060112", "lock_unsafe"),
    ]
    assert all("0600" in entry["error"] for entry in payload["failed"])
    assert payload["copyback_lock_failures"] == {"lock_timeout": 0, "lock_unsafe": 2, "lock_budget_exhausted": 0}
    assert _keys(payload["deleted"]) == ["raw/gfs/2026060100"]
    assert not raw.exists()
    for tree in canonical:
        assert (tree / "prcp_rate_or_amount").is_dir()
    assert lock_file.stat().st_mode & 0o777 == 0o644


def test_ef12_an_unopenable_lock_file_is_lock_unsafe(tmp_path: Path) -> None:
    """The production shape: a `0600` lock file this account cannot open."""

    if os.geteuid() == 0:
        pytest.skip("root opens any file mode, so the denial cannot be simulated")
    store = _store(tmp_path)
    canonical = _canonical_cycle(store, "IFS", "2026060100")
    lock_file = store / COPYBACK_BATCH_LOCK_NAME
    lock_file.write_bytes(b"")
    os.chmod(lock_file, 0o000)
    try:
        result = node27_raw_retention.run_retention(_config(store), now=NOW)
    finally:
        os.chmod(lock_file, 0o600)

    assert [entry["lock_failure"] for entry in result["failed"]] == ["lock_unsafe"]
    assert result["failed"][0]["error_type"] == "CopybackLockError"
    assert "Permission denied" in result["failed"][0]["error"]
    assert canonical.is_dir()


def test_ef12_a_non_owner_with_no_lock_file_is_refused_without_creating_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    raw = _raw_cycle(store, "gfs", "2026060100")
    canonical = _canonical_cycle(store, "IFS", "2026060100")
    _production_env(monkeypatch, store=store)
    foreign_uid = os.getuid() + 4242
    monkeypatch.setattr(os, "geteuid", lambda: foreign_uid)

    exit_code, payload = _main(capsys, REFERENCE_ARGS)

    assert exit_code == 1
    assert [(entry["key"], entry["lock_failure"]) for entry in payload["failed"]] == [
        ("canonical/IFS/2026060100", "lock_unsafe")
    ]
    assert "refusing to create" in payload["failed"][0]["error"]
    assert not (store / COPYBACK_BATCH_LOCK_NAME).exists()
    assert canonical.is_dir()
    assert not raw.exists()


# --- EF-12b: the lock file is never a removal candidate ------------------------


def test_ef12b_the_lock_file_at_the_root_is_never_planned_and_survives_a_pass(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _raw_cycle(store, "gfs", "2026060100")
    canonical = _canonical_cycle(store, "IFS", "2026060100")
    lock_file = store / COPYBACK_BATCH_LOCK_NAME
    lock_file.write_bytes(b"")
    os.chmod(lock_file, 0o600)

    result = node27_raw_retention.run_retention(_config(store), now=NOW)

    assert result["execution_mode"] == "production_execute"
    for entry in result["planned"] + result["skipped"]:
        assert COPYBACK_BATCH_LOCK_NAME not in str(entry.get("key", ""))
        assert COPYBACK_BATCH_LOCK_NAME not in str(entry.get("path", ""))
    assert result["failed"] == []
    assert not canonical.exists()
    assert lock_file.is_file()


# --- EF-13: runs that remove nothing acquire nothing ----------------------------


@pytest.mark.parametrize(
    ("enabled", "dry_run", "execution_mode"),
    [(False, False, "disabled"), (True, True, "plan_only")],
)
def test_ef13_disabled_and_plan_only_runs_acquire_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, dry_run: bool, execution_mode: str
) -> None:
    store = _store(tmp_path)
    canonical = _canonical_cycle(store, "IFS", "2026060100")
    _forbid_acquisitions(monkeypatch)

    result = node27_raw_retention.run_retention(_config(store, enabled=enabled, dry_run=dry_run), now=NOW)

    assert result["execution_mode"] == execution_mode
    assert result["copyback_lock_failures"] == ZERO_LOCK_FAILURES
    assert not (store / COPYBACK_BATCH_LOCK_NAME).exists()
    assert canonical.is_dir()


def test_ef13_preflight_blocked_runs_acquire_nothing_and_carry_the_zero_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    canonical = _canonical_cycle(store, "IFS", "2026060100")
    _forbid_acquisitions(monkeypatch)

    # Blocked at config: a relative root.
    _production_env(monkeypatch, store=Path("relative-object-store"))
    exit_code, payload = _main(capsys, REFERENCE_ARGS)
    assert exit_code == 2
    assert payload["status"] == "preflight_blocked"
    assert payload["copyback_lock_failures"] == ZERO_LOCK_FAILURES

    # Blocked at the watermark read, with a real root configured.
    _production_env(monkeypatch, store=store)
    monkeypatch.setenv("NODE27_DISPLAY_WATERMARK_DATABASE_URL", "")
    exit_code, payload = _main(capsys, ["--sources", "gfs,ifs"])
    assert exit_code == 2
    assert payload["status"] == "preflight_blocked"
    assert payload["copyback_lock_failures"] == ZERO_LOCK_FAILURES
    assert not (store / COPYBACK_BATCH_LOCK_NAME).exists()
    assert canonical.is_dir()


# --- EF-15: the plan-to-delete window is tolerated, not re-adjudicated ----------


def test_ef15_a_writer_committing_into_a_planned_tree_before_acquisition_does_not_change_the_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    planned_tree = _canonical_cycle(store, "IFS", "2026060100")
    expected_keys = ["canonical/IFS/2026060100"]
    plan_only = node27_raw_retention.run_retention(_config(store, dry_run=True), now=NOW)
    assert _keys(plan_only["planned"]) == expected_keys

    from packages.common import copyback_guard

    writes: list[str] = []

    def acquire_after_a_writer_commits(root: Path | str, **kwargs: Any) -> int:
        if not writes:
            completed = subprocess.run(
                _python(_WRITER_PROGRAM.format(repo=str(REPO_ROOT), root=str(store), target=str(planned_tree))),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            writes.append(completed.stdout.strip())
            assert (planned_tree / "prcp_rate_or_amount" / "rewritten.nc").is_file()
        return copyback_guard.acquire_copyback_batch_lock(root, **kwargs)

    monkeypatch.setattr(
        node27_raw_retention, "acquire_copyback_batch_lock", acquire_after_a_writer_commits, raising=False
    )

    result = node27_raw_retention.run_retention(_config(store), now=NOW)

    assert writes == ["committed"]
    # The removal predicate was not re-evaluated under the lock: the tree the
    # writer just committed is removed, and the planned/deleted sets match the
    # plan made before the writer ran.
    assert _keys(result["planned"]) == expected_keys
    assert _keys(result["deleted"]) == expected_keys
    assert result["failed"] == []
    assert not planned_tree.exists()
