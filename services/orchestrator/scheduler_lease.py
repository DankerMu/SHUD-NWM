from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import sys
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from errno import EEXIST, EISDIR, ELOOP, ENOTDIR
from pathlib import Path
from typing import Any
from uuid import uuid4

LOCK_OWNER = "production_scheduler"
LOCK_SCHEMA_VERSION = 1
MAX_LOCK_PAYLOAD_BYTES = 16_384
# Bound production scheduler DB lock connects so a misconfigured/unreachable
# database_url fails fast instead of hanging the scheduler pass.
RECONCILE_DB_CONNECT_TIMEOUT_SECONDS = 5


class UnsafeSchedulerLockError(OSError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _scheduler_compat_function(
    name: str,
    fallback: Callable[..., Any],
) -> Callable[..., Any]:
    """Resolve monkeypatch-compatible helpers from scheduler.py when present."""

    scheduler_module = sys.modules.get("services.orchestrator.scheduler")
    value = getattr(scheduler_module, name, None) if scheduler_module is not None else None
    return value if callable(value) else fallback


def _default_owner_liveness_probe(payload: Mapping[str, Any]) -> bool | None:
    """Probe whether the lock owner process is alive.

    Same-host + valid pid -> os.kill(pid, 0): True if reachable, False on
    ProcessLookupError (dead), True on PermissionError (alive, other user).
    Different host or no pid -> None (cannot probe cross-host).
    """

    host = payload.get("host")
    pid = payload.get("pid")
    if host != socket.gethostname() or not isinstance(pid, int):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _compat_owner_liveness_probe(payload: Mapping[str, Any]) -> bool | None:
    probe = _scheduler_compat_function(
        "_default_owner_liveness_probe",
        _default_owner_liveness_probe,
    )
    return probe(payload)


class _LeaseHeartbeat:
    """Background daemon thread that renews a lease until stopped or lost."""

    def __init__(self, lease: FileSchedulerLease, pass_id: str, interval_seconds: float) -> None:
        self._lease = lease
        self._pass_id = pass_id
        self._interval = max(0.001, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="lease-heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                renewed = self._lease.renew(pass_id=self._pass_id)
            except Exception:
                renewed = False
            if not renewed:
                self.lost = True
                return

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._interval + 1.0)
        self._thread = None


class FileSchedulerLease:
    def __init__(
        self,
        lock_path: Path,
        *,
        ttl_seconds: int,
        workspace_root: Path | None = None,
        owner_liveness_probe: Callable[[Mapping[str, Any]], bool | None] | None = None,
    ) -> None:
        self.lock_path = lock_path
        self.ttl_seconds = ttl_seconds
        self.workspace_root = workspace_root
        self.acquired = False
        self.lease_token: str | None = None
        self._owner_liveness_probe = owner_liveness_probe or _compat_owner_liveness_probe

    def acquire(self, *, pass_id: str, started_at: datetime) -> dict[str, Any]:
        token = uuid4().hex
        payload = {
            "owner": LOCK_OWNER,
            "schema_version": LOCK_SCHEMA_VERSION,
            "pass_id": pass_id,
            "lease_token": token,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "heartbeat_seq": 0,
            "heartbeat_at": _format_utc(started_at),
            "started_at": _format_utc(started_at),
            "lock_path": str(self.lock_path),
        }
        try:
            with self._guarded() as parent_fd:
                return self._acquire_locked(
                    pass_id=pass_id,
                    started_at=started_at,
                    payload=payload,
                    parent_fd=parent_fd,
                )
        except UnsafeSchedulerLockError as error:
            return {
                "acquired": False,
                "contention": True,
                "lock_path": str(self.lock_path),
                "lock_type": "file",
                "reason": error.reason,
                "existing_lock": {"raw": None},
            }

    def renew(self, *, pass_id: str) -> bool:
        """Refresh our lease heartbeat in place (CAS on pass_id + lease_token).

        Returns True only if we still own the lock and rewrote it. Returns
        False if the lock is gone or was taken over (token/pass_id mismatch).
        """

        if not self.acquired or self.lease_token is None:
            return False
        try:
            with self._guarded() as parent_fd:
                existing = self._read_existing_lock(parent_fd=parent_fd)
                if existing.get("pass_id") != pass_id or existing.get("lease_token") != self.lease_token:
                    return False
                now = datetime.now(UTC)
                payload = dict(existing)
                payload["heartbeat_seq"] = int(existing.get("heartbeat_seq", 0)) + 1
                payload["heartbeat_at"] = _format_utc(now)
                self._rewrite_lock_in_place(payload, parent_fd=parent_fd)
                return True
        except UnsafeSchedulerLockError:
            return False

    def _rewrite_lock_in_place(self, payload: Mapping[str, Any], *, parent_fd: int) -> None:
        # Crash-atomic renew: write the new payload to a sibling temp file then
        # rename it over the live lock. The truncate-then-write form left an
        # empty lock if write/utime raised after truncation; an atomic same-dir
        # rename never exposes a half-written or empty lock file.
        tmp_name = f"{self.lock_path.name}.renew.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(tmp_name, flags, 0o644, dir_fd=parent_fd)
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                raise UnsafeSchedulerLockError("unsafe_lock_symlink") from error
            raise
        try:
            os.write(fd, json.dumps(dict(payload), sort_keys=True).encode("utf-8"))
            # Refresh st_mtime explicitly for coarse-mtime filesystems.
            os.utime(fd)
            os.close(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        try:
            os.rename(
                tmp_name,
                self.lock_path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except OSError as error:
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
            if error.errno in {ELOOP, ENOTDIR}:
                raise UnsafeSchedulerLockError("unsafe_lock_symlink") from error
            raise

    def _acquire_locked(
        self,
        *,
        pass_id: str,
        started_at: datetime,
        payload: Mapping[str, Any],
        parent_fd: int,
    ) -> dict[str, Any]:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path.name, flags, 0o644, dir_fd=parent_fd)
        except FileExistsError:
            state = self._existing_lock_state(started_at, parent_fd=parent_fd)
            if state["unsafe"]:
                return {
                    "acquired": False,
                    "contention": True,
                    "lock_path": str(self.lock_path),
                    "lock_type": "file",
                    "reason": state["reason"],
                    "existing_lock": state["existing_lock"],
                }
            if state["stale"]:
                stale_existing = state["existing_lock"]
                token0 = stale_existing.get("lease_token")
                seq0 = stale_existing.get("heartbeat_seq")
                # CAS: re-read under the same guard; a holder that renewed
                # between the stale decision and now must never be unlinked.
                try:
                    os.stat(self.lock_path.name, dir_fd=parent_fd, follow_symlinks=False)
                    lock_present = True
                except FileNotFoundError:
                    lock_present = False
                if lock_present:
                    reread = self._read_existing_lock(parent_fd=parent_fd)
                    if (reread.get("lease_token"), reread.get("heartbeat_seq")) != (token0, seq0):
                        return {
                            "acquired": False,
                            "contention": True,
                            "lock_path": str(self.lock_path),
                            "lock_type": "file",
                            "existing_lock": reread,
                        }
                unlink_lock_file = _scheduler_compat_function("_unlink_lock_file", _unlink_lock_file)
                unlink_lock_file(self.lock_path.name, parent_fd=parent_fd)
                return self._acquire_locked(
                    pass_id=pass_id,
                    started_at=started_at,
                    payload=payload,
                    parent_fd=parent_fd,
                )
            return {
                "acquired": False,
                "contention": True,
                "lock_path": str(self.lock_path),
                "lock_type": "file",
                "existing_lock": state["existing_lock"],
            }
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                raise UnsafeSchedulerLockError("unsafe_lock_symlink") from error
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        self.acquired = True
        self.lease_token = str(payload.get("lease_token"))
        return {
            "acquired": True,
            "contention": False,
            "lock_path": str(self.lock_path),
            "lock_type": "file",
            "lease": dict(payload),
        }

    def release(self, *, pass_id: str) -> None:
        if not self.acquired:
            return
        try:
            with self._guarded() as parent_fd:
                existing = self._read_existing_lock(parent_fd=parent_fd)
                if existing.get("pass_id") == pass_id and existing.get("lease_token") == self.lease_token:
                    unlink_lock_file = _scheduler_compat_function("_unlink_lock_file", _unlink_lock_file)
                    unlink_lock_file(self.lock_path.name, parent_fd=parent_fd)
        except UnsafeSchedulerLockError:
            pass
        self.acquired = False
        self.lease_token = None

    @contextmanager
    def _guarded(self) -> Any:
        import fcntl

        open_lock_parent_directory = _scheduler_compat_function(
            "_open_lock_parent_directory",
            _open_lock_parent_directory,
        )
        open_regular_guard_file = _scheduler_compat_function(
            "_open_regular_guard_file",
            _open_regular_guard_file,
        )
        parent_fd = open_lock_parent_directory(self.lock_path.parent, self.workspace_root)
        try:
            guard_fd = open_regular_guard_file(f"{self.lock_path.name}.guard", dir_fd=parent_fd)
        except Exception:
            os.close(parent_fd)
            raise
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            yield parent_fd
        finally:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
            os.close(guard_fd)
            os.close(parent_fd)

    def _existing_lock_state(self, now: datetime | None = None, *, parent_fd: int) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        try:
            lock_stat = os.stat(self.lock_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"unsafe": False, "stale": False, "reason": None, "existing_lock": {}}
        if stat.S_ISLNK(lock_stat.st_mode):
            return {
                "unsafe": True,
                "stale": False,
                "reason": "unsafe_lock_symlink",
                "existing_lock": {"raw": None},
            }
        if not stat.S_ISREG(lock_stat.st_mode):
            return {
                "unsafe": True,
                "stale": False,
                "reason": "unsafe_lock_not_regular_file",
                "existing_lock": {"raw": None},
            }
        if lock_stat.st_size > MAX_LOCK_PAYLOAD_BYTES:
            return {
                "unsafe": True,
                "stale": False,
                "reason": "unsafe_lock_too_large",
                "existing_lock": {
                    "raw": None,
                    "size_bytes": lock_stat.st_size,
                    "max_bytes": MAX_LOCK_PAYLOAD_BYTES,
                },
            }
        existing = self._read_existing_lock(parent_fd=parent_fd)
        scheduler_owned = (
            existing.get("owner") == LOCK_OWNER
            and existing.get("schema_version") == LOCK_SCHEMA_VERSION
            and existing.get("lease_token") not in (None, "")
            and existing.get("pass_id") not in (None, "")
        )
        mtime = datetime.fromtimestamp(lock_stat.st_mtime, tz=UTC)
        mtime_age = (now - mtime).total_seconds()
        if mtime_age <= self.ttl_seconds:
            stale = False  # fresh heartbeat / recent lock
        else:
            liveness = self._owner_liveness_probe(existing)
            if liveness is True:
                stale = False  # owner provably alive: live contention, never reclaim
            elif liveness is False:
                stale = True  # owner provably dead
            else:
                # Cross-host unknown: require 2x TTL silence before reclaiming.
                stale = mtime_age > 2 * self.ttl_seconds
        if stale and not scheduler_owned:
            return {
                "unsafe": True,
                "stale": True,
                "reason": "unsafe_lock_not_scheduler_owned",
                "existing_lock": existing,
            }
        return {"unsafe": False, "stale": stale, "reason": None, "existing_lock": existing}

    def _read_existing_lock(self, *, parent_fd: int) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path.name, flags, dir_fd=parent_fd)
        except OSError:
            return {"raw": None}
        try:
            lock_stat = os.fstat(fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                return {"raw": None}
            if lock_stat.st_size > MAX_LOCK_PAYLOAD_BYTES:
                raise UnsafeSchedulerLockError("unsafe_lock_too_large")
            raw = os.read(fd, MAX_LOCK_PAYLOAD_BYTES + 1)
            if len(raw) > MAX_LOCK_PAYLOAD_BYTES:
                raise UnsafeSchedulerLockError("unsafe_lock_too_large")
            value = json.loads(raw.decode("utf-8"))
        except UnsafeSchedulerLockError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return {"raw": None}
        finally:
            os.close(fd)
        return dict(value) if isinstance(value, Mapping) else {"raw": value}


class PostgresSchedulerLease:
    def __init__(
        self,
        database_url: str,
        *,
        lock_name: str,
        display_lock_path: str,
    ) -> None:
        self.database_url = database_url
        self.lock_name = lock_name
        self.display_lock_path = display_lock_path
        self.connection: Any | None = None
        self.acquired = False
        advisory_lock_key = _scheduler_compat_function(
            "_postgres_advisory_lock_key",
            _postgres_advisory_lock_key,
        )
        self.lock_key = advisory_lock_key(lock_name)

    def acquire(self, *, pass_id: str, started_at: datetime) -> dict[str, Any]:
        import psycopg2

        del started_at
        try:
            connection = psycopg2.connect(
                self.database_url,
                connect_timeout=RECONCILE_DB_CONNECT_TIMEOUT_SECONDS,
            )
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (self.lock_key,))
                row = cursor.fetchone()
            if not row or row[0] is not True:
                connection.close()
                return {
                    "acquired": False,
                    "contention": True,
                    "lock_path": self.display_lock_path,
                    "lock_type": "postgres_advisory",
                    "reason": "postgres_advisory_lock_contended",
                    "existing_lock": {"raw": None},
                }
        except Exception as error:
            try:
                connection.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            return {
                "acquired": False,
                "contention": True,
                "lock_path": self.display_lock_path,
                "lock_type": "postgres_advisory",
                "reason": "postgres_advisory_lock_unavailable",
                "error_type": type(error).__name__,
                "existing_lock": {"raw": None},
            }
        self.connection = connection
        self.acquired = True
        return {
            "acquired": True,
            "contention": False,
            "lock_path": self.display_lock_path,
            "lock_type": "postgres_advisory",
            "lease": {
                "pass_id": pass_id,
                "lock_key": self.lock_key,
                "lock_name": self.lock_name,
                "owner": LOCK_OWNER,
                "schema_version": LOCK_SCHEMA_VERSION,
            },
        }

    def renew(self, *, pass_id: str) -> bool:
        del pass_id
        return self.acquired and self.connection is not None

    def release(self, *, pass_id: str) -> None:
        del pass_id
        connection = self.connection
        self.connection = None
        self.acquired = False
        if connection is None:
            return
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (self.lock_key,))
        except Exception:
            pass
        finally:
            try:
                connection.close()
            except Exception:
                pass


def _postgres_advisory_lock_key(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    unsigned = int.from_bytes(digest, byteorder="big", signed=False)
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def _open_lock_parent_directory(lock_parent: Path, workspace_root: Path | None) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    if workspace_root is None:
        lock_parent.mkdir(parents=True, exist_ok=True)
        try:
            return os.open(lock_parent, directory_flags)
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
            raise

    workspace_root = workspace_root.resolve()
    _ensure_workspace_directory(workspace_root)
    try:
        relative_parent = lock_parent.relative_to(workspace_root)
    except ValueError as error:
        raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error

    try:
        root_fd = os.open(workspace_root, directory_flags)
    except OSError as error:
        if error.errno in {ELOOP, ENOTDIR}:
            raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
        raise

    parent_fd = root_fd
    try:
        for component in relative_parent.parts:
            if component in ("", "."):
                continue
            if component == "..":
                raise UnsafeSchedulerLockError("unsafe_lock_parent_directory")
            try:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o755, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except OSError as error:
                if error.errno in {ELOOP, ENOTDIR}:
                    raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
                raise
            os.close(parent_fd)
            parent_fd = child_fd
    except Exception:
        os.close(parent_fd)
        raise
    return parent_fd


def _ensure_workspace_directory(workspace_root: Path) -> None:
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        if error.errno in {ELOOP, ENOTDIR}:
            raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
        raise
    try:
        root_stat = workspace_root.lstat()
    except FileNotFoundError as error:
        raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise UnsafeSchedulerLockError("unsafe_lock_parent_directory")


def _open_regular_guard_file(guard_name: str, *, dir_fd: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(guard_name, os.O_CREAT | os.O_RDWR | nofollow, 0o644, dir_fd=dir_fd)
    except OSError as error:
        if error.errno in {EEXIST, EISDIR, ELOOP, ENOTDIR}:
            raise UnsafeSchedulerLockError("unsafe_lock_guard_not_regular_file") from error
        raise
    try:
        guard_stat = os.fstat(fd)
        if not stat.S_ISREG(guard_stat.st_mode):
            raise UnsafeSchedulerLockError("unsafe_lock_guard_not_regular_file")
    except Exception:
        os.close(fd)
        raise
    return fd


def _unlink_lock_file(lock_name: str, *, parent_fd: int) -> None:
    try:
        os.unlink(lock_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


__all__ = [
    "FileSchedulerLease",
    "LOCK_OWNER",
    "LOCK_SCHEMA_VERSION",
    "MAX_LOCK_PAYLOAD_BYTES",
    "PostgresSchedulerLease",
    "RECONCILE_DB_CONNECT_TIMEOUT_SECONDS",
    "UnsafeSchedulerLockError",
    "_LeaseHeartbeat",
    "_default_owner_liveness_probe",
    "_open_lock_parent_directory",
    "_open_regular_guard_file",
    "_postgres_advisory_lock_key",
    "_unlink_lock_file",
]
