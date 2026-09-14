"""Requirement-driven tests for the copyback guard's lock primitive, timeout and
failure vocabulary (#2252, #2262).

Contract, from the `object-store-copyback-mutual-exclusion` delta of
`harden-copyback-mutex-residuals`:

* an explicitly passed timeout that is not a positive finite number is a
  configuration refusal made before any filesystem call, so no lock file is
  created (EF-1);
* the host that exports the copyback root takes the mutex with a POSIX record
  lock (`primitive="posix"`), which contends with a second process exactly as
  `flock` does, with identical identity refusals (EF-2);
* a busy `lockf` may report `EACCES` as well as `EAGAIN`; both mean "held"
  (EF-3);
* retention lanes classify a lock error into one of three typed shapes, and the
  budget-exhausted error is still a `CopybackLockError` (EF-4).

`lockf` is per PROCESS: a second descriptor opened in this test process could
not observe a hold, and closing it would drop the hold. Every `posix` holder and
every `posix` probe therefore runs in its own subprocess. No outcome here depends
on how `flock` and `lockf` interact (they do on macOS, not on local Linux
filesystems).

Names added by this change are imported inside the tests that need them, so a
run against the pre-change guard fails per test rather than at collection.
"""

from __future__ import annotations

import errno
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from packages.common import copyback_guard as copyback_guard_module
from packages.common.copyback_guard import (
    COPYBACK_BATCH_LOCK_NAME,
    CopybackLockError,
    CopybackLockTimeout,
    acquire_copyback_batch_lock,
    copyback_batch_lock,
    release_copyback_batch_lock,
    resolve_copyback_lock_timeout_seconds,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ("flock", "posix")


def _real_root(tmp_path: Path, name: str = "copyback-root") -> Path:
    """A copyback root with no symlinked ancestor (macOS `tmp_path` sits under `/var`)."""

    root = tmp_path.resolve() / name
    root.mkdir(parents=True, exist_ok=True)
    return root


_HOLDER_PROGRAM = """
import sys
sys.path.insert(0, {repo!r})
from packages.common.copyback_guard import acquire_copyback_batch_lock, release_copyback_batch_lock
fd = acquire_copyback_batch_lock({root!r}, timeout_seconds=10, primitive={primitive!r})
print("held", flush=True)
sys.stdin.readline()
release_copyback_batch_lock(fd, primitive={primitive!r})
print("released", flush=True)
sys.stdin.readline()
"""

_PROBE_PROGRAM = """
import json, sys
sys.path.insert(0, {repo!r})
from packages.common.copyback_guard import (
    CopybackLockError, CopybackLockTimeout, acquire_copyback_batch_lock, release_copyback_batch_lock,
)
try:
    fd = acquire_copyback_batch_lock({root!r}, timeout_seconds={timeout!r}, primitive={primitive!r})
except CopybackLockTimeout as error:
    print(json.dumps({{"outcome": "timeout", "error": str(error)}}))
except CopybackLockError as error:
    print(json.dumps({{"outcome": "error", "error": str(error)}}))
else:
    release_copyback_batch_lock(fd, primitive={primitive!r})
    print(json.dumps({{"outcome": "acquired"}}))
"""


class _Holder:
    """A second process holding the mutex until told to release."""

    def __init__(self, root: Path, primitive: str) -> None:
        program = _HOLDER_PROGRAM.format(repo=str(REPO_ROOT), root=str(root), primitive=primitive)
        self.process = subprocess.Popen(
            [sys.executable, "-c", program],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdout is not None
        line = self.process.stdout.readline().strip()
        if line != "held":
            _out, err = self.process.communicate(timeout=10)
            raise AssertionError(f"holder did not acquire: {line!r} {err}")

    def release(self) -> None:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write("release\n")
        self.process.stdin.flush()
        assert self.process.stdout.readline().strip() == "released"

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)


def _probe(root: Path, *, primitive: str, timeout: float) -> dict[str, str]:
    program = _PROBE_PROGRAM.format(repo=str(REPO_ROOT), root=str(root), timeout=timeout, primitive=primitive)
    completed = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# --- EF-1: a non-finite explicit timeout is a configuration refusal -----------


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 0.0])
def test_ef1_a_non_finite_or_non_positive_explicit_timeout_is_refused_before_any_file(
    tmp_path: Path, value: float
) -> None:
    root = _real_root(tmp_path)

    with pytest.raises(CopybackLockError) as error_info:
        resolve_copyback_lock_timeout_seconds(value)
    assert not isinstance(error_info.value, CopybackLockTimeout)

    with pytest.raises(CopybackLockError) as acquire_error:
        acquire_copyback_batch_lock(root, timeout_seconds=value)
    assert not isinstance(acquire_error.value, CopybackLockTimeout)
    assert not (root / COPYBACK_BATCH_LOCK_NAME).exists()


# --- EF-2: the posix primitive contends across processes ----------------------


def test_ef2_a_posix_holder_blocks_a_posix_attempt_from_another_process_until_released(
    tmp_path: Path,
) -> None:
    root = _real_root(tmp_path)
    holder = _Holder(root, "posix")
    try:
        blocked = _probe(root, primitive="posix", timeout=0.2)
        assert blocked["outcome"] == "timeout", blocked
        assert "deadline" in blocked["error"]

        holder.release()
        freed = _probe(root, primitive="posix", timeout=5.0)
        assert freed == {"outcome": "acquired"}
    finally:
        holder.stop()
    # Never unlinked, same as under `flock`.
    lock_file = root / COPYBACK_BATCH_LOCK_NAME
    assert lock_file.is_file()
    assert oct(lock_file.stat().st_mode & 0o777) == oct(0o600)


def test_ef2_a_killed_posix_holder_releases_the_lock(tmp_path: Path) -> None:
    root = _real_root(tmp_path)
    holder = _Holder(root, "posix")
    holder.stop()

    assert _probe(root, primitive="posix", timeout=5.0) == {"outcome": "acquired"}


@pytest.mark.parametrize("primitive", PRIMITIVES)
def test_ef2_a_symlinked_lock_path_is_refused_under_either_primitive(tmp_path: Path, primitive: str) -> None:
    root = _real_root(tmp_path)
    elsewhere = tmp_path.resolve() / "planted.lock"
    elsewhere.write_bytes(b"")
    os.chmod(elsewhere, 0o600)
    (root / COPYBACK_BATCH_LOCK_NAME).symlink_to(elsewhere)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5, primitive=primitive)

    assert "symlink" in str(error_info.value)
    assert not isinstance(error_info.value, CopybackLockTimeout)


@pytest.mark.parametrize("primitive", PRIMITIVES)
def test_ef2_a_wrong_mode_lock_file_is_refused_under_either_primitive(tmp_path: Path, primitive: str) -> None:
    root = _real_root(tmp_path)
    lock_file = root / COPYBACK_BATCH_LOCK_NAME
    lock_file.write_bytes(b"")
    os.chmod(lock_file, 0o644)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5, primitive=primitive)

    assert "0600" in str(error_info.value)
    assert lock_file.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("primitive", PRIMITIVES)
def test_ef2_a_hard_linked_lock_file_is_refused_under_either_primitive(tmp_path: Path, primitive: str) -> None:
    root = _real_root(tmp_path)
    lock_file = root / COPYBACK_BATCH_LOCK_NAME
    lock_file.write_bytes(b"")
    os.chmod(lock_file, 0o600)
    os.link(lock_file, root / "second-name")

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5, primitive=primitive)

    assert "hard link" in str(error_info.value)


@pytest.mark.parametrize("primitive", PRIMITIVES)
def test_ef2_a_non_owner_is_refused_before_creating_under_either_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, primitive: str
) -> None:
    root = _real_root(tmp_path)
    foreign_uid = os.getuid() + 4242
    monkeypatch.setattr(os, "geteuid", lambda: foreign_uid)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5, primitive=primitive)

    assert "refusing to create" in str(error_info.value)
    assert not (root / COPYBACK_BATCH_LOCK_NAME).exists()


def test_ef2_an_unknown_primitive_is_refused_before_any_file(tmp_path: Path) -> None:
    root = _real_root(tmp_path)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5, primitive="ofd")  # type: ignore[arg-type]
    assert "primitive" in str(error_info.value)
    assert not (root / COPYBACK_BATCH_LOCK_NAME).exists()

    with pytest.raises(CopybackLockError):
        with copyback_batch_lock(root, timeout_seconds=0.5, primitive="ofd"):  # type: ignore[arg-type]
            pass  # pragma: no cover - never entered
    assert not (root / COPYBACK_BATCH_LOCK_NAME).exists()


def test_ef2_every_existing_caller_keeps_flock_by_default() -> None:
    for function in (
        copyback_guard_module.acquire_copyback_batch_lock,
        copyback_guard_module.release_copyback_batch_lock,
        copyback_guard_module.copyback_batch_lock,
    ):
        assert inspect.signature(function).parameters["primitive"].default == "flock", function.__name__


# --- EF-3: EACCES from lockf means "held", not "unsafe" -----------------------


def test_ef3_eacces_from_lockf_keeps_polling_until_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is actually held here: `lockf` itself is replaced, so no subprocess is needed."""

    root = _real_root(tmp_path)
    attempts: list[int] = []

    def busy_lockf(fd: int, cmd: int, *args: object) -> None:
        if cmd & copyback_guard_module.fcntl.LOCK_UN:
            return None
        attempts.append(cmd)
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(copyback_guard_module.fcntl, "lockf", busy_lockf)

    with pytest.raises(CopybackLockTimeout):
        acquire_copyback_batch_lock(root, timeout_seconds=0.2, primitive="posix")

    # The poll continued: EACCES was read as busy, not as a refusal.
    assert len(attempts) > 1


def test_ef3_a_non_busy_lockf_errno_is_a_lock_error_not_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _real_root(tmp_path)

    def broken_lockf(fd: int, cmd: int, *args: object) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(copyback_guard_module.fcntl, "lockf", broken_lockf)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=5.0, primitive="posix")

    assert not isinstance(error_info.value, CopybackLockTimeout)
    assert "No locks available" in str(error_info.value)


def test_ef3_release_with_the_posix_primitive_unlocks_with_lockf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(copyback_guard_module.fcntl, "lockf", lambda fd, cmd: calls.append("lockf"))
    monkeypatch.setattr(copyback_guard_module.fcntl, "flock", lambda fd, cmd: calls.append("flock"))
    fd = os.open(tmp_path / "scratch", os.O_RDWR | os.O_CREAT, 0o600)

    release_copyback_batch_lock(fd, primitive="posix")

    assert calls == ["lockf"]
    with pytest.raises(OSError):
        os.fstat(fd)


# --- EF-4: the typed failure vocabulary ---------------------------------------


def test_ef4_lock_errors_classify_into_three_shapes() -> None:
    from packages.common.copyback_guard import CopybackLockBudgetExhausted, copyback_lock_failure_kind

    assert copyback_lock_failure_kind(CopybackLockTimeout("held")) == "lock_timeout"
    assert copyback_lock_failure_kind(CopybackLockBudgetExhausted("spent")) == "lock_budget_exhausted"
    assert copyback_lock_failure_kind(CopybackLockError("mode")) == "lock_unsafe"


def test_ef4_budget_exhaustion_is_still_caught_as_a_lock_error() -> None:
    from packages.common.copyback_guard import CopybackLockBudgetExhausted

    try:
        raise CopybackLockBudgetExhausted("spent")
    except CopybackLockError as error:
        assert not isinstance(error, CopybackLockTimeout)
    else:  # pragma: no cover - the raise above always runs
        raise AssertionError("unreachable")


def test_ef4_failure_counts_always_carry_all_three_keys() -> None:
    from packages.common.copyback_guard import count_copyback_lock_failures

    assert count_copyback_lock_failures([]) == {
        "lock_timeout": 0,
        "lock_unsafe": 0,
        "lock_budget_exhausted": 0,
    }
    assert count_copyback_lock_failures(
        [
            {"lock_failure": "lock_timeout"},
            {"lock_failure": "lock_budget_exhausted"},
            {"lock_failure": "lock_budget_exhausted"},
            {"error_type": "PermissionError"},
        ]
    ) == {"lock_timeout": 1, "lock_unsafe": 0, "lock_budget_exhausted": 2}


def test_ef4_the_retention_budget_moved_into_the_guard_with_the_same_value() -> None:
    from packages.common.copyback_guard import DEFAULT_RETENTION_COPYBACK_LOCK_WAIT_BUDGET_SECONDS
    from services.orchestrator import retention

    assert DEFAULT_RETENTION_COPYBACK_LOCK_WAIT_BUDGET_SECONDS == 300.0
    assert retention.DEFAULT_COPYBACK_LOCK_WAIT_BUDGET_SECONDS == 300.0
