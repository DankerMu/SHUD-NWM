"""Cross-process mutex and traversal widening for object-store copyback (#2035).

Two concerns, one home, because both are properties of the *shared transport*
that the ``runs/``, ``forcing/`` and ``canonical/`` copyback lanes all use.

**Weakness A -- the batch mutex.** Every directory-tree promote-and-commit batch
under one copyback root has to be serialized across processes. A per-tree lock is
provably insufficient: ``publisher._rollback_qdown_copyback_batch``'s
``backup_dir is None`` branch removes whatever now sits at the target, which is
only a restore while no other writer can have committed into that slot in the
meantime. So the mutex spans copy -> every promote -> commit-or-rollback, and is
released only after the batch's commit or rollback has returned.

**Weakness B -- traversal.** ``safe_fs.ensure_directory_no_follow`` passes an
explicit ``0o755`` to ``os.mkdir``, which the ambient umask masks, and it
deliberately never ``chmod``s afterwards (#1513). Under ``umask 027`` every
copyback level -- the copyback root itself included -- lands ``0o750`` and the
consuming account on the other node loses traversal. The
``filesystem-permission-determinism`` capability assigns that widening to the
caller, so it lives here rather than in ``safe_fs``.

Standard library plus ``packages.common.safe_fs`` only, deliberately:
``scripts/canonical_precip_copyback_backfill.py`` must import the mutex while
node-22's checkout is venv-frozen ahead of its maintenance window, so nothing on
this module's import closure may pull in a third-party package.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from packages.common.safe_fs import SafeFilesystemError, ensure_directory_no_follow

_LOGGER = logging.getLogger(__name__)

# Fixed name under the copyback root, with no environment override. The root is
# the one path all six writers have already resolved by construction, so they
# provably reach one inode; a `/tmp` path would be split by a private-`/tmp`
# mount namespace (systemd `PrivateTmp=true`, Slurm `job_container/tmpfs`) into
# two inodes and the mutex would silently do nothing.
COPYBACK_BATCH_LOCK_NAME = ".nhms-copyback-batch.lock"
COPYBACK_LOCK_TIMEOUT_ENV = "NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS"
# 900 s, sized against a measured hold: ~2.2 GB per cycle cohort at the export's
# ~62 MB/s is ~36 s.
#
# The single in-code home of the acquisition-count claim behind this budget.
# The run-tree lane acquires at most once per cycle per scheduler pass. A
# post-acquire failure leaves the stage un-advanced, so the next pass
# acquires again for that same cycle -- sequentially, after the first
# was released, so it adds no concurrent waiter this budget must cover. The
# budget was derived from two 36 s acquisitions per cycle: 900 s covers ~24
# queued acquisitions ~= 12 concurrent execution units against the 2 of live
# steady state. Left unretuned, conservative in the safe direction.
#
# `flock` is per open file description, so the scheduler's same-process
# execution-unit threads contend exactly as separate hosts would. Exceeding the
# deadline is a bounded, loud failure -- never a hang, never an unlocked
# promote. See `design.md` "Cost accepted, deliberately".
DEFAULT_COPYBACK_LOCK_TIMEOUT_SECONDS = 900.0
COPYBACK_DIRECTORY_MODE = 0o755

_LOCK_MODE = 0o600
_POLL_SECONDS = 0.01


class CopybackLockError(RuntimeError):
    """The copyback batch mutex is unsafe, misconfigured, or cannot be opened."""


class CopybackLockTimeout(CopybackLockError):
    """The copyback batch mutex was held past this writer's deadline.

    Distinct from every other lock failure so each lane can map it onto its own
    error type without swallowing a genuine tamper refusal.
    """


def copyback_batch_lock_path(copyback_root: Path | str) -> Path:
    """The fixed lock path for one copyback root."""

    return Path(copyback_root).expanduser() / COPYBACK_BATCH_LOCK_NAME


def resolve_copyback_lock_timeout_seconds(
    timeout_seconds: float | None = None,
    *,
    env: dict[str, str] | None = None,
) -> float:
    """Explicit argument wins, then the env override, then the 900 s default.

    Read at acquire time rather than import time so an operator (and a test)
    can change it without reloading the process. A present-but-unusable value
    is a configuration refusal, never a silent fallback to the default: a
    mistyped deadline that quietly became 900 s would be indistinguishable from
    a deadline that was never set.
    """

    if timeout_seconds is not None:
        value = float(timeout_seconds)
        if value <= 0:
            raise CopybackLockError("copyback batch lock timeout must be positive")
        return value
    mapping = os.environ if env is None else env
    raw = str(mapping.get(COPYBACK_LOCK_TIMEOUT_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_COPYBACK_LOCK_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as error:
        raise CopybackLockError(
            f"{COPYBACK_LOCK_TIMEOUT_ENV} must be a positive number of seconds, got {raw!r}"
        ) from error
    if value <= 0 or value != value or value == float("inf"):
        raise CopybackLockError(f"{COPYBACK_LOCK_TIMEOUT_ENV} must be a positive number of seconds, got {raw!r}")
    return value


def _open_flags() -> int:
    return os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def copyback_root_owner_uid(copyback_root: Path | str) -> int:
    """The owner uid of the copyback root, the anchor every writer shares.

    Its own function rather than an inline ``os.stat`` because it is the seam
    the identity assertion is built on: comparing the lock file's owner to the
    *current* euid alone only closes the pre-existing-file direction (a foreign
    writer that finds someone else's lock file is refused). The **create**
    direction is wide open -- a foreign-uid writer that gets there first creates
    the lock file, passes its own euid check, and poisons the lock for the real
    writers permanently, because the file is never unlinked. The root is owned by
    the writer account by construction on the live export, so anchoring the
    assertion to it closes both directions with one comparison.
    """

    root = Path(copyback_root).expanduser()
    try:
        return os.stat(root).st_uid
    except OSError as error:
        raise CopybackLockError(f"copyback root owner is unavailable for {root}: {error}") from error


def _require_lock_identity(path: Path, fd: int, *, root_uid: int) -> None:
    """Re-assert that the fd and the name still describe one safe, owned file."""

    try:
        info = os.fstat(fd)
        named = os.lstat(path)
    except OSError as error:
        raise CopybackLockError(f"copyback batch lock identity is unavailable: {error}") from error
    if stat.S_ISLNK(named.st_mode):
        raise CopybackLockError("copyback batch lock must not be a symlink")
    if not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(named.st_mode):
        raise CopybackLockError("copyback batch lock must be a regular file")
    if stat.S_IMODE(info.st_mode) != _LOCK_MODE or stat.S_IMODE(named.st_mode) != _LOCK_MODE:
        raise CopybackLockError("copyback batch lock must have mode 0600")
    euid = os.geteuid()
    if info.st_uid != euid or named.st_uid != euid:
        # Reachable in the pre-existing-file direction only for euid 0 or under
        # a test that patches `os.geteuid`: a non-root writer meeting a `0o600`
        # lock file owned by someone else never gets here, because the
        # `O_RDWR|O_NOFOLLOW` reopen after `O_CREAT|O_EXCL` fails `EACCES` first
        # and surfaces through the generic `except OSError` below as
        # `cannot acquire copyback batch lock <path>: [Errno 13] Permission
        # denied` -- path, no uid. That `Permission denied` on the lock path is
        # the real-world foreign-owner signal there, and the runbooks say so.
        # When this branch *is* reached it still names all three facts the
        # root-owner branch below does: both uids and the lock path.
        raise CopybackLockError(
            "copyback batch lock must be owned by the effective user: "
            f"effective uid {euid} does not own lock file uid {info.st_uid} at {path}"
        )
    if info.st_uid != root_uid or named.st_uid != root_uid:
        # The operator has to act on this without a second round trip, so the
        # message names both uids and the path: a lock file owned by anyone but
        # the copyback root's owner is a poisoned lock, not a busy one.
        raise CopybackLockError(
            "copyback batch lock owner uid "
            f"{info.st_uid} does not match copyback root owner uid {root_uid} at {path}"
        )
    if info.st_nlink != 1 or named.st_nlink != 1:
        raise CopybackLockError("copyback batch lock must have exactly one hard link")
    if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
        raise CopybackLockError("copyback batch lock path/fd identity drifted")


def acquire_copyback_batch_lock(
    copyback_root: Path | str,
    *,
    timeout_seconds: float | None = None,
) -> int:
    """Block up to the deadline for the copyback root's exclusive batch flock.

    Returns the held descriptor. Contention **waits**: refusing outright would
    turn a race into a dropped mirror. The wait is a bounded ``LOCK_EX|LOCK_NB``
    poll loop; exceeding it raises ``CopybackLockTimeout`` and never degrades to
    an unlocked promote.

    The lock file is created ``0o600`` and is **never unlinked** by this module:
    unlinking would let a second writer create a fresh inode and lock that
    instead. A killed holder's flock is released by the kernel on process exit,
    so a stale file is not a stale lock.

    That release guarantee is the *local* kernel's, and the production copyback
    root is not local: ``/ghdc/data/nwm/object-store`` on node-22 is an NFSv4.2
    mount of ``ghdc:/home/ghdc`` (measured 2026-09-10). A process death there
    still releases at exit, but a **host** death does not -- the server holds
    the lock open until the client's lease expires, so on that root there is a
    third state beyond "held" and "orphaned": correctly owned, no local holder,
    and still locked. The only correct response is to wait it out; unlinking
    splits the mutex onto a fresh inode exactly as it would with a live holder.

    Not reentrant. ``flock`` is per open file description, so a second
    acquisition against the same root from the same process contends with the
    first and blocks itself until the deadline. Acquire at batch-owner level
    only.
    """

    deadline = time.monotonic() + resolve_copyback_lock_timeout_seconds(timeout_seconds)
    path = copyback_batch_lock_path(copyback_root)
    if not path.is_absolute():
        raise CopybackLockError(f"copyback batch lock path must be absolute: {path}")
    root_uid = copyback_root_owner_uid(copyback_root)
    flags = _open_flags()
    fd: int | None = None
    try:
        try:
            named = os.lstat(path)
        except FileNotFoundError:
            named = None
        if named is not None and stat.S_ISLNK(named.st_mode):
            raise CopybackLockError("copyback batch lock must not be a symlink")
        if named is None and os.geteuid() != root_uid:
            # Refuse *before* creating. The assertions below would catch this
            # writer too, but only after `O_CREAT|O_EXCL` had already left a
            # foreign-uid `0o600` file behind -- and the lock file is never
            # unlinked, so that orphan is exactly the poisoned lock an operator
            # then has to clean up by hand. Only the create branch: a
            # pre-existing file still goes through the identity assertions
            # unchanged.
            raise CopybackLockError(
                "refusing to create copyback batch lock as uid "
                f"{os.geteuid()}: copyback root owner uid is {root_uid} at {path}"
            )
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, _LOCK_MODE)
        except FileExistsError:
            fd = os.open(path, flags)
        else:
            # This process created that inode under `O_EXCL`, so widening it back
            # to `0o600` cannot touch anyone else's file -- and it must happen
            # before the identity assertion, because `O_CREAT`'s mode argument is
            # masked by the umask exactly as `mkdir`'s is and a restrictive umask
            # would otherwise make this writer fail closed against its own file.
            # A *pre-existing* wrong-mode lock file keeps failing closed: it is
            # refused by the assertion below before any `fchmod` reaches it.
            os.fchmod(fd, _LOCK_MODE)
        _require_lock_identity(path, fd, root_uid=root_uid)
        os.fchmod(fd, _LOCK_MODE)
        _require_lock_identity(path, fd, root_uid=root_uid)
        _flock_until_deadline(fd, deadline=deadline, path=path)
        _require_lock_identity(path, fd, root_uid=root_uid)
        held = fd
        fd = None
        return held
    except CopybackLockError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        if getattr(error, "errno", None) in {errno.ELOOP, errno.EMLINK}:
            raise CopybackLockError("copyback batch lock must not be a symlink") from error
        raise CopybackLockError(f"cannot acquire copyback batch lock {path}: {error}") from error


def _flock_until_deadline(fd: int, *, deadline: float, path: Path) -> None:
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CopybackLockTimeout(
                    f"copyback batch lock {path} was still held after the configured deadline"
                ) from error
            time.sleep(min(_POLL_SECONDS, remaining))


def release_copyback_batch_lock(fd: int) -> None:
    """Drop the flock and close the fd, both attempted and neither raising. The lock file is never unlinked."""

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as error:
        _LOGGER.warning("copyback batch lock unlock failed for fd %s: %s", fd, error)
    try:
        os.close(fd)
    except OSError as error:
        _LOGGER.warning("copyback batch lock close failed for fd %s: %s", fd, error)


@contextmanager
def copyback_batch_lock(
    copyback_root: Path | str,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[int]:
    """Hold the copyback root's batch mutex for the whole of the ``with`` body.

    Callers must enter this **after** their copyback root's identity and overlap
    guards, so a lane that returns ``skipped`` without writing creates no lock
    file, and must not leave it before their batch commit or rollback has
    returned.
    """

    fd = acquire_copyback_batch_lock(copyback_root, timeout_seconds=timeout_seconds)
    try:
        yield fd
    finally:
        release_copyback_batch_lock(fd)


def ensure_traversable_copyback_directory(
    path: Path,
    *,
    containment_root: Path | None = None,
) -> Path:
    """Create a copyback directory chain and widen the levels *this call* created.

    ``safe_fs.ensure_directory_no_follow`` does the creating, so the no-follow
    and containment guarantees are unchanged; this adds the caller-side
    ``chmod 0o755`` that ``filesystem-permission-determinism`` assigns to the
    caller. Levels that already existed keep their mode verbatim (#1513).

    The missing levels are found by probing **upward before creating anything**,
    not by walking ``path.relative_to(containment_root).parts``: that parts list
    is empty when ``path`` *is* the root, so the root -- the level issue #2035's
    own ``umask 027`` measurement lists first, and the one whose ``0o750``
    defeats traversal no matter what sits below it -- would never be widened.
    ``containment_root`` therefore stays optional and is passed through to
    ``safe_fs`` for symlink containment only.

    Each level is created and widened before the next one is attempted. A single
    ``mkdir(parents=True)`` followed by a widening loop would leave the ancestors
    at the process umask forever whenever the leaf fails, and a later run's
    existence probe would no longer count them as created.

    ``0o755``'s group bits are ``r-x``, which is what makes this widening
    mask-neutral under an inherited POSIX default ACL: ``safe_fs``'s explicit
    ``mkdir`` mode already clamped ``mask::rwx`` to ``mask::r-x`` before this
    runs. Do not generalize that to other modes --
    ``state_manager._ensure_copyback_state_parent``'s ``0o775`` genuinely
    restores ``mask::rwx`` and must not be narrowed to this one.

    Accepted limit: ``ensure_directory_no_follow`` absorbs ``FileExistsError``,
    so "created by this call" is decided by the pre-probe and a level lost to a
    concurrent creator between probe and create is still widened. Every
    directory-tree copyback writer now holds the batch mutex, the per-file state
    lane writes a disjoint subtree, and the one level all of them share is the
    copyback root -- where ``0o755`` is the intended mode anyway.
    """

    target = Path(path).expanduser()
    missing: list[Path] = []
    probe = target
    while True:
        try:
            os.lstat(probe)
        except FileNotFoundError:
            missing.append(probe)
            parent = probe.parent
            if parent == probe:
                break
            probe = parent
            continue
        except OSError as error:
            raise SafeFilesystemError(
                f"Failed to probe copyback directory component {probe}: {error}",
                kind="io",
            ) from error
        break

    for level in reversed(missing):
        ensure_directory_no_follow(level, containment_root=containment_root)
        os.chmod(level, COPYBACK_DIRECTORY_MODE, follow_symlinks=False)
    # Also covers `missing == []`, where this is the plain existing-path verify
    # `ensure_directory_no_follow` already performed for every caller replaced
    # by this helper, and returns the configured (unresolved) path they expect.
    return ensure_directory_no_follow(target, containment_root=containment_root)
