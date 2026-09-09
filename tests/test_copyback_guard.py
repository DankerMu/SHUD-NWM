"""Requirement-driven tests for `packages/common/copyback_guard.py` (#2035).

Contract, from the two spec deltas this change adds:

* `object-store-copyback-mutual-exclusion` -- one exclusive cross-process mutex
  per copyback root, fixed under that root, held for a whole directory-tree
  promote-and-commit batch; blocking with a bounded deadline; a distinct loud
  timeout error and never an unlocked promote; fail-closed on an unsafe lock
  file; two distinct roots must not contend.
* `filesystem-permission-determinism` -- every level a copyback call creates,
  the copyback root included, lands `0o755` regardless of the process umask;
  levels the call did not create keep their mode verbatim (#1513); the widening
  is unconditional because `safe_fs`'s explicit-mode `mkdir` already clamped an
  inherited ACL mask before any caller-side `chmod` runs (#1631).

`os.umask` is process-global, so every umask test sets and restores it in a
`try/finally` (the `tests/test_canonical_precip_copyback_backfill.py:426-430`
idiom) and this module is not xdist-parallel safe. `flock` is per open file
description, so two threads that each open the lock file do contend, which is
what makes the concurrency rows deterministic without subprocesses.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from packages.common import copyback_guard as copyback_guard_module
from packages.common.copyback_guard import (
    COPYBACK_BATCH_LOCK_NAME,
    COPYBACK_LOCK_TIMEOUT_ENV,
    DEFAULT_COPYBACK_LOCK_TIMEOUT_SECONDS,
    CopybackLockError,
    CopybackLockTimeout,
    acquire_copyback_batch_lock,
    copyback_batch_lock,
    copyback_batch_lock_path,
    ensure_traversable_copyback_directory,
    release_copyback_batch_lock,
    resolve_copyback_lock_timeout_seconds,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _real_root(tmp_path: Path, name: str = "copyback-root") -> Path:
    """A copyback root with no symlinked ancestor.

    `safe_fs` walks every component with `O_NOFOLLOW`, and on macOS pytest's
    `tmp_path` sits under `/var -> private/var`.
    """

    root = (tmp_path.resolve() / name)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- E1: the mutex itself ----------------------------------------------------


def test_the_lock_file_is_a_fixed_name_under_the_copyback_root(tmp_path: Path) -> None:
    """Anchored where every writer already resolved one inode, with no env override."""

    root = _real_root(tmp_path)

    assert copyback_batch_lock_path(root) == root / ".nhms-copyback-batch.lock"
    assert COPYBACK_BATCH_LOCK_NAME == ".nhms-copyback-batch.lock"

    with copyback_batch_lock(root):
        pass

    lock_file = root / COPYBACK_BATCH_LOCK_NAME
    # Never unlinked: a writer that removed it would let the next writer create a
    # fresh inode and flock that instead, which is the mutex silently splitting.
    assert lock_file.is_file()
    assert _mode(lock_file) == 0o600


def test_two_writers_on_one_root_serialize_and_the_second_observes_the_first(tmp_path: Path) -> None:
    root = _real_root(tmp_path)
    order: list[str] = []
    first_holds = threading.Event()
    second_started = threading.Event()
    errors: list[BaseException] = []

    def second_writer() -> None:
        try:
            second_started.set()
            with copyback_batch_lock(root):
                order.append("second-entered")
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    thread = threading.Thread(target=second_writer)
    with copyback_batch_lock(root):
        first_holds.set()
        thread.start()
        assert second_started.wait(timeout=10)
        # The second writer is blocked, not running: it cannot have appended.
        time.sleep(0.2)
        assert thread.is_alive()
        assert order == []
        order.append("first-committed")
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert order == ["first-committed", "second-entered"]


def test_two_distinct_copyback_roots_do_not_contend(tmp_path: Path) -> None:
    """Every pytest `tmp_path` gets its own lock file, so the suite never self-serializes."""

    first = _real_root(tmp_path, "root-a")
    second = _real_root(tmp_path, "root-b")

    with copyback_batch_lock(first):
        # Would raise `CopybackLockTimeout` within 0.5 s if the roots shared a lock.
        with copyback_batch_lock(second, timeout_seconds=0.5):
            pass

    assert (first / COPYBACK_BATCH_LOCK_NAME).is_file()
    assert (second / COPYBACK_BATCH_LOCK_NAME).is_file()


def test_an_aliased_copyback_root_reaches_the_same_lock_file(tmp_path: Path) -> None:
    """A second spelling of one root must not become a second mutex."""

    root = _real_root(tmp_path)
    alias = root.parent / "alias"
    alias.symlink_to(root, target_is_directory=True)

    with copyback_batch_lock(root):
        with pytest.raises(CopybackLockTimeout):
            acquire_copyback_batch_lock(alias, timeout_seconds=0.2)


def test_the_mutex_is_not_reentrant_within_one_process(tmp_path: Path) -> None:
    """`flock` is per open file description: a nested acquire blocks itself.

    Pinned as a test because it is the constraint that decides *where* every
    lane may acquire -- a second acquisition inside
    `publisher._copyback_object_tree_with_rollback` would deadlock the forcing
    backfill, which calls that helper while already holding the lock.
    """

    root = _real_root(tmp_path)

    with copyback_batch_lock(root):
        with pytest.raises(CopybackLockTimeout):
            acquire_copyback_batch_lock(root, timeout_seconds=0.2)


def test_the_deadline_is_bounded_and_raises_a_distinct_timeout_error(tmp_path: Path) -> None:
    root = _real_root(tmp_path)

    with copyback_batch_lock(root):
        started = time.monotonic()
        with pytest.raises(CopybackLockTimeout) as error_info:
            acquire_copyback_batch_lock(root, timeout_seconds=0.3)
        elapsed = time.monotonic() - started

    # Waits (contention must not become a dropped mirror) but is bounded.
    assert 0.3 <= elapsed < 10
    assert isinstance(error_info.value, CopybackLockError)
    assert "deadline" in str(error_info.value)


def test_a_killed_holders_lock_is_released_by_the_kernel(tmp_path: Path) -> None:
    """A stale lock *file* is not a stale lock; the module never unlinks it."""

    root = _real_root(tmp_path)
    program = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from packages.common.copyback_guard import acquire_copyback_batch_lock\n"
        f"acquire_copyback_batch_lock({str(root)!r})\n"
        "print('held', flush=True)\n"
        "time.sleep(120)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(CopybackLockTimeout):
            acquire_copyback_batch_lock(root, timeout_seconds=0.3)
        holder.kill()
        holder.wait(timeout=10)
    finally:
        if holder.poll() is None:  # pragma: no cover - defensive
            holder.kill()
            holder.wait(timeout=10)

    assert (root / COPYBACK_BATCH_LOCK_NAME).is_file()
    with copyback_batch_lock(root, timeout_seconds=10):
        pass


def test_a_symlinked_lock_path_fails_closed(tmp_path: Path) -> None:
    root = _real_root(tmp_path)
    elsewhere = tmp_path.resolve() / "planted.lock"
    elsewhere.write_bytes(b"")
    os.chmod(elsewhere, 0o600)
    (root / COPYBACK_BATCH_LOCK_NAME).symlink_to(elsewhere)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5)

    assert "symlink" in str(error_info.value)


def test_a_wrong_mode_lock_file_fails_closed(tmp_path: Path) -> None:
    root = _real_root(tmp_path)
    lock_file = root / COPYBACK_BATCH_LOCK_NAME
    lock_file.write_bytes(b"")
    os.chmod(lock_file, 0o644)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5)

    assert "0600" in str(error_info.value)
    # Refused, not repaired: a pre-existing wrong-mode file is a tamper signal.
    assert _mode(lock_file) == 0o644


def test_a_hard_linked_lock_file_fails_closed(tmp_path: Path) -> None:
    root = _real_root(tmp_path)
    lock_file = root / COPYBACK_BATCH_LOCK_NAME
    lock_file.write_bytes(b"")
    os.chmod(lock_file, 0o600)
    os.link(lock_file, root / "second-name")

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5)

    assert "hard link" in str(error_info.value)


def test_a_foreign_owned_lock_file_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer under another account fails closed instead of running unlocked."""

    root = _real_root(tmp_path)
    (root / COPYBACK_BATCH_LOCK_NAME).write_bytes(b"")
    os.chmod(root / COPYBACK_BATCH_LOCK_NAME, 0o600)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 4242)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5)

    assert "effective user" in str(error_info.value)


def test_a_foreign_uid_cannot_create_the_lock_file_in_the_first_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CREATE direction of the poisoning, which the euid compare alone misses.

    Comparing the lock file's owner to the current euid only refuses a writer
    that finds *someone else's* file. A foreign-uid writer that gets there first
    creates the file, passes its own euid check and -- because the lock file is
    never unlinked -- poisons the mutex for the real writers permanently. The
    live root `/ghdc/data/nwm/object-store` had no lock file yet, so the first
    post-merge writer would have set the owner for good.
    """

    root = _real_root(tmp_path)
    foreign_uid = os.getuid() + 4242
    monkeypatch.setattr(os, "geteuid", lambda: foreign_uid)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5)

    # Refused *before* creating: no orphan is left behind to poison the mutex.
    assert not (root / COPYBACK_BATCH_LOCK_NAME).exists()
    message = str(error_info.value)
    # Both uids and the path, so the operator can act without a round trip.
    assert str(foreign_uid) in message
    assert str(os.stat(root).st_uid) in message
    assert COPYBACK_BATCH_LOCK_NAME in message
    assert not isinstance(error_info.value, CopybackLockTimeout)


def test_a_lock_file_whose_owner_is_not_the_copyback_roots_owner_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root-owner mismatch, covered separately from the euid mismatch.

    Here the euid check passes -- this process owns the lock file -- and the
    refusal comes purely from the root anchor, which is the direction the
    pre-existing-file test cannot reach.
    """

    root = _real_root(tmp_path)
    lock_file = root / COPYBACK_BATCH_LOCK_NAME
    lock_file.write_bytes(b"")
    os.chmod(lock_file, 0o600)
    foreign_root_uid = os.getuid() + 4242
    monkeypatch.setattr(copyback_guard_module, "copyback_root_owner_uid", lambda _root: foreign_root_uid)

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(root, timeout_seconds=0.5)

    message = str(error_info.value)
    assert str(os.geteuid()) in message
    assert str(foreign_root_uid) in message
    assert str(lock_file) in message
    assert not isinstance(error_info.value, CopybackLockTimeout)


def test_an_unreadable_copyback_root_is_a_lock_error_not_a_timeout(tmp_path: Path) -> None:
    """`copyback_root_owner_uid` fails closed on the anchor it cannot stat."""

    missing = _real_root(tmp_path) / "not-created"

    with pytest.raises(CopybackLockError) as error_info:
        acquire_copyback_batch_lock(missing, timeout_seconds=0.5)

    assert "copyback root owner is unavailable" in str(error_info.value)
    assert not isinstance(error_info.value, CopybackLockTimeout)


def test_a_restrictive_umask_does_not_make_a_writer_fail_against_its_own_lock_file(
    tmp_path: Path,
) -> None:
    """`O_CREAT`'s mode is masked exactly as `mkdir`'s is."""

    root = _real_root(tmp_path)

    previous_umask = os.umask(0o077)
    try:
        with copyback_batch_lock(root):
            pass
    finally:
        os.umask(previous_umask)

    assert _mode(root / COPYBACK_BATCH_LOCK_NAME) == 0o600


# --- production config: the deadline override --------------------------------


def test_the_deadline_defaults_to_nine_hundred_seconds() -> None:
    """900 s, sized against the measured hold rather than the hook's position.

    2.2 GB per acquisition at 62 MB/s is ~36 s, and
    `_stage_should_copyback_run_trees` acquires twice per cycle, so 900 s admits
    ~24 acquisitions ~= 12 concurrent execution units against the 2 of live
    steady state. 300 s admitted only ~4 and left a replay pass short.
    """

    assert DEFAULT_COPYBACK_LOCK_TIMEOUT_SECONDS == 900.0
    assert resolve_copyback_lock_timeout_seconds(env={}) == 900.0
    assert resolve_copyback_lock_timeout_seconds(env={COPYBACK_LOCK_TIMEOUT_ENV: ""}) == 900.0
    assert resolve_copyback_lock_timeout_seconds(env={COPYBACK_LOCK_TIMEOUT_ENV: "  "}) == 900.0


def test_the_deadline_override_is_read_from_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert resolve_copyback_lock_timeout_seconds(env={COPYBACK_LOCK_TIMEOUT_ENV: "12.5"}) == 12.5

    root = _real_root(tmp_path)
    monkeypatch.setenv(COPYBACK_LOCK_TIMEOUT_ENV, "0.3")
    with copyback_batch_lock(root):
        started = time.monotonic()
        with pytest.raises(CopybackLockTimeout):
            acquire_copyback_batch_lock(root)
        assert time.monotonic() - started < 10


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "nan", "inf"])
def test_an_unusable_deadline_is_a_configuration_refusal_not_a_silent_default(raw: str) -> None:
    """A mistyped deadline that quietly became 900 s is indistinguishable from none."""

    with pytest.raises(CopybackLockError):
        resolve_copyback_lock_timeout_seconds(env={COPYBACK_LOCK_TIMEOUT_ENV: raw})


def test_an_explicit_non_positive_deadline_is_refused() -> None:
    with pytest.raises(CopybackLockError):
        resolve_copyback_lock_timeout_seconds(0)


# --- E6 / E7: the traversal widening -----------------------------------------


@pytest.mark.parametrize("umask_value", [0o027, 0o022, 0o002, 0o077])
def test_every_level_this_call_creates_lands_0o755(tmp_path: Path, umask_value: int) -> None:
    """The copyback ROOT is one of those levels -- `0o750 .` is issue #2035's first row."""

    base = tmp_path.resolve()
    target = base / "copyback" / "canonical" / "gfs" / "2026050100" / "grid"

    previous_umask = os.umask(umask_value)
    try:
        ensure_traversable_copyback_directory(target)
    finally:
        os.umask(previous_umask)

    created = [
        base / "copyback",
        base / "copyback" / "canonical",
        base / "copyback" / "canonical" / "gfs",
        base / "copyback" / "canonical" / "gfs" / "2026050100",
        target,
    ]
    landed = {str(path): oct(_mode(path)) for path in created}
    assert landed == {str(path): "0o755" for path in created}
    # No group- or other-write bit on any created level, at any umask.
    assert all(_mode(path) & 0o022 == 0 for path in created)


def test_the_copyback_root_itself_is_widened_when_the_call_creates_it(tmp_path: Path) -> None:
    """`relative.parts` is empty when the path *is* the root -- a silent no-op.

    This is why the helper probes upward for missing components instead of
    walking a containment-relative parts list the way
    `state_manager._ensure_copyback_state_parent` does.
    """

    root = tmp_path.resolve() / "brand-new-root"

    previous_umask = os.umask(0o027)
    try:
        ensure_traversable_copyback_directory(root)
    finally:
        os.umask(previous_umask)

    assert _mode(root) == 0o755


def test_a_pre_existing_intermediate_directory_keeps_its_mode(tmp_path: Path) -> None:
    """#1513: never chmod a path this call did not create."""

    root = _real_root(tmp_path)
    existing = root / "canonical"
    existing.mkdir()
    os.chmod(existing, 0o700)

    previous_umask = os.umask(0o027)
    try:
        ensure_traversable_copyback_directory(existing / "gfs", containment_root=root)
    finally:
        os.umask(previous_umask)

    assert _mode(existing) == 0o700
    assert _mode(existing / "gfs") == 0o755


def test_an_existing_leaf_is_not_widened(tmp_path: Path) -> None:
    root = _real_root(tmp_path)
    leaf = root / "canonical"
    leaf.mkdir()
    os.chmod(leaf, 0o750)

    ensure_traversable_copyback_directory(leaf, containment_root=root)

    assert _mode(leaf) == 0o750


def test_the_helper_returns_the_configured_path_and_keeps_no_follow_containment(tmp_path: Path) -> None:
    root = _real_root(tmp_path)
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    assert ensure_traversable_copyback_directory(root / "inside", containment_root=root) == root / "inside"

    from packages.common.safe_fs import SafeFilesystemError

    with pytest.raises(SafeFilesystemError):
        ensure_traversable_copyback_directory(root / "escape" / "child", containment_root=root)
    assert not (outside / "child").exists()


def test_ancestors_are_widened_before_a_later_level_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mkdir(parents=True)` + a widening loop would leave ancestors at the umask.

    `parents=True` creates the ancestors first, the loop is never reached, and a
    later run's existence probe no longer counts them as created -- so they stay
    at the umask forever. Level-by-level create-then-widen is what removes that.
    """

    from packages.common import copyback_guard
    from packages.common.safe_fs import SafeFilesystemError, ensure_directory_no_follow

    base = tmp_path.resolve()
    target = base / "copyback" / "canonical" / "gfs"
    calls: list[Path] = []

    def failing_ensure(path: Path, *, containment_root: Path | None = None) -> Path:
        calls.append(path)
        if path == target:
            raise SafeFilesystemError("injected leaf failure", kind="io")
        return ensure_directory_no_follow(path, containment_root=containment_root)

    monkeypatch.setattr(copyback_guard, "ensure_directory_no_follow", failing_ensure)

    previous_umask = os.umask(0o027)
    try:
        with pytest.raises(SafeFilesystemError):
            ensure_traversable_copyback_directory(target)
    finally:
        os.umask(previous_umask)

    assert calls == [base / "copyback", base / "copyback" / "canonical", target]
    assert _mode(base / "copyback") == 0o755
    assert _mode(base / "copyback" / "canonical") == 0o755
    assert not target.exists()


def test_the_raw_acquire_release_pair_round_trips(tmp_path: Path) -> None:
    """The canonical lane holds the fd explicitly so `finally` can outlive its rollback."""

    root = _real_root(tmp_path)

    fd = acquire_copyback_batch_lock(root, timeout_seconds=5)
    try:
        with pytest.raises(CopybackLockTimeout):
            acquire_copyback_batch_lock(root, timeout_seconds=0.2)
    finally:
        release_copyback_batch_lock(fd)

    with copyback_batch_lock(root, timeout_seconds=5):
        pass


# --- E8: the POSIX default-ACL boundary (#1631) ------------------------------


def _setfacl(path: Path, spec: str) -> None:
    subprocess.run(["setfacl", "-m", spec, str(path)], check=True, capture_output=True, text=True)


def _acl_mask(path: Path) -> str:
    output = subprocess.run(
        ["getfacl", "--omit-header", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for line in output.splitlines():
        if line.startswith("mask::"):
            return line.split("::", 1)[1].strip()
    raise AssertionError(f"no mask line in getfacl output for {path}: {output!r}")


def _require_acl_support(tmp_path: Path) -> Path:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("POSIX ACL tooling (setfacl/getfacl) is not available on this platform")
    parent = tmp_path.resolve() / "acl-parent"
    parent.mkdir()
    try:
        _setfacl(parent, f"default:user:{os.getuid()}:rwx")
        _setfacl(parent, "default:mask::rwx")
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"the filesystem backing tmp_path does not support POSIX ACLs: {error}")
    return parent


def test_the_0o755_widening_is_mask_neutral_under_an_inherited_default_acl(tmp_path: Path) -> None:
    """`safe_fs`'s explicit `mkdir` mode already clamped the mask before we run.

    Asserted **both before and after** the widening: the point of the
    unconditional rule is that an ACL probe could never change the outcome.
    """

    from packages.common.safe_fs import ensure_directory_no_follow

    parent = _require_acl_support(tmp_path)

    probe = parent / "safe-fs-created"
    ensure_directory_no_follow(probe, containment_root=parent)
    mask_before = _acl_mask(probe)
    os.chmod(probe, 0o755, follow_symlinks=False)
    mask_after = _acl_mask(probe)

    assert mask_before == "r-x"
    assert mask_after == "r-x"

    widened = parent / "through-the-helper"
    ensure_traversable_copyback_directory(widened, containment_root=parent)
    assert _acl_mask(widened) == "r-x"
    assert _mode(widened) == 0o755


def test_a_mode_less_mkdir_under_the_same_parent_keeps_mask_rwx(tmp_path: Path) -> None:
    """Pins the `run_tree_copyback.py:440` boundary this change must not move."""

    parent = _require_acl_support(tmp_path)

    interior = parent / "mode-less"
    interior.mkdir()

    assert _acl_mask(interior) == "rwx"


def test_a_0o775_widening_still_restores_mask_rwx(tmp_path: Path) -> None:
    """`state_manager._ensure_copyback_state_parent`'s mode must not be narrowed.

    The `0o755` rule of this change is mask-neutral precisely because its group
    bits are `r-x`; that must not be generalized to a claim about `chmod`.
    """

    from packages.common.safe_fs import ensure_directory_no_follow

    parent = _require_acl_support(tmp_path)

    probe = parent / "state-lane"
    ensure_directory_no_follow(probe, containment_root=parent)
    assert _acl_mask(probe) == "r-x"

    os.chmod(probe, 0o775, follow_symlinks=False)

    assert _acl_mask(probe) == "rwx"


# --- unchanged sibling consumers (T6 / E10 / AC6) ----------------------------


def test_safe_fs_still_passes_an_explicit_mode_and_never_chmods_after_creating() -> None:
    """T6: `packages/common/safe_fs.py` is untouched by this change.

    The compensation belongs to the caller because
    `filesystem-permission-determinism` says the helper "SHALL NOT `chmod` a
    directory after creating it". A widening kwarg here would contradict it.
    """

    import inspect

    from packages.common import safe_fs

    source = inspect.getsource(safe_fs.ensure_directory_no_follow)
    assert "os.mkdir(part, 0o755, dir_fd=fd)" in source
    statements = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    assert not any("chmod" in line for line in statements)


def test_the_run_tree_interior_still_uses_a_mode_less_mkdir() -> None:
    """AC6 / #1631: `run_tree_copyback.py:440` must stay exactly as it is."""

    import inspect

    from services.orchestrator import run_tree_copyback

    source = inspect.getsource(run_tree_copyback._copy_tree_no_symlinks)
    assert "destination_dir.mkdir(parents=True, exist_ok=True)" in source
    statements = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    assert not any("ensure_traversable_copyback_directory" in line for line in statements)
    assert not any("ensure_directory_no_follow" in line for line in statements)


def test_a_mode_less_mkdir_interior_is_untouched_by_the_widening(tmp_path: Path) -> None:
    """The mode-less `mkdir` keeps landing at the umask, not at `0o755`."""

    from services.orchestrator import run_tree_copyback

    source_root = tmp_path.resolve() / "source"
    (source_root / "nested").mkdir(parents=True)
    (source_root / "nested" / "file.txt").write_bytes(b"payload")

    previous_umask = os.umask(0o027)
    try:
        run_tree_copyback._copy_tree_no_symlinks(source_root, tmp_path.resolve() / "target")
    finally:
        os.umask(previous_umask)

    assert _mode(tmp_path.resolve() / "target" / "nested") == 0o750


def test_the_per_file_state_copyback_writer_succeeds_without_this_mutex(tmp_path: Path) -> None:
    """E10: the provider-atomic state lane is explicitly exempt and unaffected."""

    from packages.common import state_manager

    root = _real_root(tmp_path)
    parent = root / "scheduler" / "state-index"

    # No lock held anywhere; the exempt writer must still complete.
    state_manager._ensure_copyback_state_parent(parent, root)

    assert parent.is_dir()
    assert _mode(parent) == 0o775
    assert not (root / COPYBACK_BATCH_LOCK_NAME).exists()
