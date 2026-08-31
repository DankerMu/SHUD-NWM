from __future__ import annotations

import errno
import os
import stat
import uuid
from pathlib import Path


class SafeFilesystemError(RuntimeError):
    """Raised when a filesystem operation cannot be completed safely.

    ``kind`` discriminates the refusal structurally so callers can branch on
    the field rather than on message text:

    - ``"unsafe"`` (default) -- tamper-shaped refusal: symlinked target or
      path component, non-regular target, containment violation.
    - ``"io"`` -- an ``OSError`` wrapped into this module's contract.
    - ``"indeterminate"`` -- an operation that may or may not have taken
      effect (durable-replace dir-fsync / parent-identity uncertainty).
    - ``"identity_changed"`` -- the target's inode was replaced between the
      pre-open stat and the post-open fstat: the target was swapped for a
      DIFFERENT REGULAR FILE while it was being opened.  An ordinary atomic
      ``os.replace`` and a hostile swap are indistinguishable at this layer,
      so this is a CONSISTENCY refusal, not the symlink defense: a symlink
      appearing in that window is refused by ``O_NOFOLLOW`` (ELOOP) and the
      symlink mode checks and never reaches the identity comparison.  Callers
      that own a concurrent-replace relationship may absorb this kind with a
      bounded retry; callers that must reject any inode movement keep
      rejecting it.  The primitive itself never retries.
    """

    def __init__(self, message: str, *, kind: str = "unsafe") -> None:
        super().__init__(message)
        self.kind = kind


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)


def verify_directory_no_follow(path: Path) -> Path:
    """Verify an existing directory by walking every component without following symlinks."""

    target = _expand_path(path)
    fd = _open_directory_no_follow(target)
    os.close(fd)
    return target


def directory_identity_no_follow(path: Path) -> tuple[int, int]:
    """Return ``(st_dev, st_ino)`` for an existing directory, opened without following symlinks.

    Only **already-resolved** paths may be passed.  The per-component walk
    rejects every symlink component *including the final one* (ENOTDIR on
    macOS, ELOOP on Linux), so a raw path whose last component is a symlink
    raises instead of reporting the link target's identity.

    The pair is deliberately a bare tuple rather than an ``os.stat_result`` so
    callers cannot compare on any other field.  The identity it carries is
    limited to aliases sharing a filesystem superblock.
    """

    fd = _open_directory_no_follow(path)
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    return info.st_dev, info.st_ino


def ensure_directory_no_follow(path: Path, *, containment_root: Path | None = None) -> Path:
    """Create a directory via no-follow directory descriptors and return its configured path."""

    target = _expand_path(path)
    root, parts = _anchor_for(target, containment_root=containment_root)
    root_fd = _open_directory_no_follow(root)
    try:
        fd = root_fd
        for part in parts:
            if part in {"", ".", ".."}:
                raise SafeFilesystemError(f"Unsafe directory component: {part!r}")
            try:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                try:
                    # Explicit 0o755 lets umask restrict but never widen a
                    # safe_fs-created directory.  Never fchmod an existing path:
                    # that would override a restrictive umask or inherited ACL.
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise SafeFilesystemError(
                        f"Failed to create directory component {part!r} under {target}: {error}",
                        kind="io",
                    ) from error
                next_fd = _open_child_dir(fd, part, target)
            except NotADirectoryError as error:
                raise SafeFilesystemError(f"Path component is not a directory: {target}") from error
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise SafeFilesystemError(f"Path component must not be a symlink: {target}") from error
                raise SafeFilesystemError(f"Failed to open directory component {target}: {error}", kind="io") from error
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        if fd != root_fd:
            os.close(fd)
    finally:
        os.close(root_fd)
    return target


def atomic_write_bytes_no_follow(
    path: Path,
    content: bytes,
    *,
    containment_root: Path | None = None,
    temp_suffix: str = "tmp",
    mode: int | None = None,
    require_durable_replace: bool = False,
) -> Path:
    """Atomically replace a file without following symlinked parents or targets.

    ``require_durable_replace`` opts callers into a strict post-replace directory
    fsync and parent-identity check.  The default retains the helper's historical
    best-effort directory-fsync error model for existing callers.
    """

    target = _expand_path(path)
    parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=True)
    temp_name = f".{target.name}.{uuid.uuid4().hex}.{temp_suffix}"
    file_fd: int | None = None
    replaced = False
    try:
        _verify_fd_matches_path(parent_fd, parent_path)
        _reject_existing_symlink(parent_fd, target.name, target)
        file_fd = os.open(temp_name, _FILE_FLAGS, 0o666 if mode is None else mode, dir_fd=parent_fd)
        if mode is not None:
            os.fchmod(file_fd, mode)
        view = memoryview(content)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        _verify_fd_matches_path(parent_fd, parent_path)
        os.replace(temp_name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        replaced = True
        if require_durable_replace:
            try:
                os.fsync(parent_fd)
            except OSError as error:
                raise SafeFilesystemError(
                    f"Atomic replacement for {target} completed but directory fsync failed: {error}",
                    kind="indeterminate",
                ) from error
            try:
                _verify_fd_matches_path(parent_fd, parent_path)
            except SafeFilesystemError as error:
                raise SafeFilesystemError(
                    f"Atomic replacement for {target} completed but parent identity changed: {error}",
                    kind="indeterminate",
                ) from error
        else:
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
    except SafeFilesystemError:
        _close_file_fd(file_fd)
        if not replaced:
            _unlink_temp(parent_fd, temp_name)
        raise
    except OSError as error:
        _close_file_fd(file_fd)
        if not replaced:
            _unlink_temp(parent_fd, temp_name)
        kind = "indeterminate" if replaced else "io"
        raise SafeFilesystemError(f"Failed to write {target}: {error}", kind=kind) from error
    finally:
        os.close(parent_fd)
    return target


def open_directory_no_follow(path: Path, *, containment_root: Path | None = None) -> int:
    """Open and pin a directory beneath an optional containment root without following symlinks."""

    target = _expand_path(path)
    root, parts = _anchor_for(target, containment_root=containment_root)
    root_fd = _open_directory_no_follow(root)
    fd = root_fd
    try:
        for part in parts:
            next_fd = _open_child_dir(fd, part, target)
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        directory_fd = os.dup(root_fd) if fd == root_fd else fd
        if fd != root_fd:
            fd = -1
        try:
            _verify_fd_matches_path(directory_fd, target)
            return directory_fd
        except Exception:
            os.close(directory_fd)
            raise
    except Exception:
        if fd != -1 and fd != root_fd:
            os.close(fd)
        raise
    finally:
        os.close(root_fd)


def write_bytes_no_follow_exclusive(
    path: Path,
    content: bytes,
    *,
    containment_root: Path | None = None,
) -> Path:
    """Create a file without following symlinked parents or targets, failing if it exists."""

    target = _expand_path(path)
    parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=True)
    file_fd: int | None = None
    try:
        _verify_fd_matches_path(parent_fd, parent_path)
        file_fd = os.open(target.name, _FILE_FLAGS, 0o666, dir_fd=parent_fd)
        view = memoryview(content)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
    except FileExistsError:
        _close_file_fd(file_fd)
        raise
    except OSError as error:
        _close_file_fd(file_fd)
        raise SafeFilesystemError(f"Failed to create {target}: {error}", kind="io") from error
    finally:
        os.close(parent_fd)
    return target


def open_file_no_follow(path: Path, *, containment_root: Path | None = None) -> int:
    """Open a file for reading without following symlinked parents or target."""

    target = _expand_path(path)
    parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=False)
    try:
        _verify_fd_matches_path(parent_fd, parent_path)
        try:
            expected = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise SafeFilesystemError(f"Failed to stat {target}: {error}", kind="io") from error
        if stat.S_ISLNK(expected.st_mode):
            raise SafeFilesystemError(f"Target file must not be a symlink: {target}")
        if not stat.S_ISREG(expected.st_mode):
            raise SafeFilesystemError(f"Target file must be a regular file: {target}")
        try:
            file_fd = os.open(target.name, _READ_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise SafeFilesystemError(f"Target file must not be a symlink: {target}") from error
            raise
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode):
                raise SafeFilesystemError(f"Target file must be a regular file: {target}")
            if expected.st_dev != opened.st_dev or expected.st_ino != opened.st_ino:
                raise SafeFilesystemError(f"Target file changed while being opened: {target}", kind="identity_changed")
            _verify_fd_matches_path(parent_fd, parent_path)
            return file_fd
        except Exception:
            os.close(file_fd)
            raise
    finally:
        os.close(parent_fd)


def prove_named_entry_absent_durable(path: Path, *, containment_root: Path | None = None) -> None:
    """Durably prove a named entry is absent through a pinned no-follow parent."""

    target = _expand_path(path)
    parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=False)
    try:
        _verify_fd_matches_path(parent_fd, parent_path)
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise SafeFilesystemError(f"Failed to stat {target}: {error}", kind="io") from error
        else:
            raise SafeFilesystemError(f"Named entry is still present: {target}")
        _fsync_and_verify_parent(parent_fd, parent_path, target, proof="absence")
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as error:
            raise _indeterminate_proof_error(target, "absence", error) from error
        raise SafeFilesystemError(f"Named entry reappeared after fsync: {target}", kind="indeterminate")
    finally:
        os.close(parent_fd)


def read_bytes_durable_no_follow(
    path: Path,
    *,
    max_bytes: int,
    containment_root: Path | None = None,
) -> bytes:
    """Read one bounded regular file and durably prove that exact pathname entry."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    target = _expand_path(path)
    parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=False)
    file_fd: int | None = None
    try:
        file_fd, before = _open_pinned_regular_entry(parent_fd, parent_path, target)
        if before.st_size > max_bytes:
            raise SafeFilesystemError(f"Target file exceeds byte ceiling: {target}")
        raw = bytearray()
        try:
            while len(raw) <= max_bytes:
                chunk = os.read(file_fd, min(1024 * 1024, max_bytes + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(file_fd)
        except OSError as error:
            raise SafeFilesystemError(f"Failed to read {target}: {error}", kind="io") from error
        if (
            len(raw) != before.st_size
            or os.read(file_fd, 1)
            or (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise SafeFilesystemError(f"Target file changed while being read: {target}", kind="identity_changed")
        _fsync_and_verify_parent(parent_fd, parent_path, target, proof="durable-read")
        try:
            current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise _indeterminate_proof_error(target, "durable-read", error) from error
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise SafeFilesystemError(f"Target file changed after fsync: {target}", kind="indeterminate")
        return bytes(raw)
    finally:
        _close_file_fd(file_fd)
        os.close(parent_fd)


def stat_no_follow(path: Path, *, containment_root: Path | None = None) -> os.stat_result:
    """Stat a filesystem entry without following symlinked parents or target."""

    target = _expand_path(path)
    parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=False)
    try:
        _verify_fd_matches_path(parent_fd, parent_path)
        result = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(result.st_mode):
            raise SafeFilesystemError(f"Target file must not be a symlink: {target}")
        _verify_fd_matches_path(parent_fd, parent_path)
        return result
    except FileNotFoundError:
        raise
    except SafeFilesystemError:
        raise
    except OSError as error:
        raise SafeFilesystemError(f"Failed to stat {target}: {error}", kind="io") from error
    finally:
        os.close(parent_fd)


def read_bytes_no_follow(path: Path, *, containment_root: Path | None = None) -> bytes:
    """Read a file through a no-follow descriptor-bound open."""

    file_fd = open_file_no_follow(path, containment_root=containment_root)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(file_fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise SafeFilesystemError(f"Failed to read {path}: {error}", kind="io") from error
    finally:
        os.close(file_fd)


def read_bytes_limited_no_follow(path: Path, *, max_bytes: int, containment_root: Path | None = None) -> bytes:
    """Read at most max_bytes plus one sentinel byte through a no-follow open."""

    file_fd = open_file_no_follow(path, containment_root=containment_root)
    try:
        content = bytearray()
        limit = max_bytes + 1
        while len(content) < limit:
            chunk = os.read(file_fd, limit - len(content))
            if not chunk:
                break
            content.extend(chunk)
        return bytes(content)
    except OSError as error:
        raise SafeFilesystemError(f"Failed to read {path}: {error}", kind="io") from error
    finally:
        os.close(file_fd)


def read_tail_bytes_limited_no_follow(
    path: Path,
    *,
    max_bytes: int,
    containment_root: Path | None = None,
) -> bytes:
    """Read at most the final max_bytes through a descriptor-bound no-follow open."""

    file_fd = open_file_no_follow(path, containment_root=containment_root)
    try:
        size = os.fstat(file_fd).st_size
        if size > max_bytes:
            os.lseek(file_fd, size - max_bytes, os.SEEK_SET)
        return os.read(file_fd, max_bytes)
    except OSError as error:
        raise SafeFilesystemError(f"Failed to read {path}: {error}", kind="io") from error
    finally:
        os.close(file_fd)


def list_directory_no_follow(path: Path, *, containment_root: Path | None = None) -> list[str]:
    """List a directory through no-follow directory descriptors."""

    return _list_directory_no_follow(path, containment_root=containment_root, max_entries=None)


def list_directory_no_follow_limited(
    path: Path,
    *,
    max_entries: int,
    containment_root: Path | None = None,
) -> list[str]:
    """List at most max_entries plus one sentinel entry through no-follow directory descriptors."""

    if max_entries < 0:
        raise ValueError("max_entries must be non-negative")
    return _list_directory_no_follow(path, containment_root=containment_root, max_entries=max_entries)


def _list_directory_no_follow(
    path: Path,
    *,
    containment_root: Path | None,
    max_entries: int | None,
) -> list[str]:
    target = _expand_path(path)
    root, parts = _anchor_for(target, containment_root=containment_root)
    root_fd = _open_directory_no_follow(root)
    fd = root_fd
    try:
        for part in parts:
            next_fd = _open_child_dir(fd, part, target)
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        names: list[str] = []
        with os.scandir(fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if max_entries is not None and len(names) > max_entries:
                    break
        return names
    except FileNotFoundError:
        raise
    except OSError as error:
        raise SafeFilesystemError(f"Failed to list directory {target}: {error}", kind="io") from error
    finally:
        if fd != root_fd:
            os.close(fd)
        os.close(root_fd)


def unlink_no_follow(path: Path, *, containment_root: Path | None = None, missing_ok: bool = False) -> None:
    """Unlink a non-directory path relative to a no-follow parent directory descriptor."""

    target = _expand_path(path)
    try:
        parent_fd, _parent_path = _open_parent_dir(target, containment_root=containment_root, create=False)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    try:
        try:
            entry_stat = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if stat.S_ISLNK(entry_stat.st_mode):
            raise SafeFilesystemError(f"Refusing to unlink symlink: {target}")
        if stat.S_ISDIR(entry_stat.st_mode):
            raise SafeFilesystemError(f"Refusing to unlink directory with file unlink helper: {target}")
        os.unlink(target.name, dir_fd=parent_fd)
    except SafeFilesystemError:
        raise
    except OSError as error:
        raise SafeFilesystemError(f"Failed to unlink {target}: {error}", kind="io") from error
    finally:
        os.close(parent_fd)


def unlink_no_follow_durable(path: Path, *, containment_root: Path | None = None, missing_ok: bool = False) -> None:
    """Unlink a file, then fsync and re-verify the no-follow parent directory.

    Parent fsync/identity failure after unlink is indeterminate because the
    pathname may already be gone while durability remains unproven.
    """

    target = _expand_path(path)
    try:
        parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=False)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    unlinked = False
    try:
        try:
            entry_stat = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if stat.S_ISLNK(entry_stat.st_mode):
            raise SafeFilesystemError(f"Refusing to unlink symlink: {target}")
        if stat.S_ISDIR(entry_stat.st_mode):
            raise SafeFilesystemError(f"Refusing to unlink directory with file unlink helper: {target}")
        os.unlink(target.name, dir_fd=parent_fd)
        unlinked = True
        try:
            os.fsync(parent_fd)
        except OSError as error:
            raise SafeFilesystemError(
                f"Unlink of {target} completed but directory fsync failed: {error}",
                kind="indeterminate",
            ) from error
        try:
            _verify_fd_matches_path(parent_fd, parent_path)
        except SafeFilesystemError as error:
            raise SafeFilesystemError(
                f"Unlink of {target} completed but parent identity changed: {error}",
                kind="indeterminate",
            ) from error
    except SafeFilesystemError:
        raise
    except OSError as error:
        kind = "indeterminate" if unlinked else "io"
        raise SafeFilesystemError(f"Failed to unlink {target}: {error}", kind=kind) from error
    finally:
        os.close(parent_fd)


def rmtree_no_follow(path: Path, *, containment_root: Path | None = None, missing_ok: bool = False) -> None:
    """Remove a directory tree without following symlinks in the path or descendants."""

    target = _expand_path(path)
    try:
        parent_fd, _parent_path = _open_parent_dir(target, containment_root=containment_root, create=False)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    try:
        try:
            entry_stat = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if stat.S_ISLNK(entry_stat.st_mode):
            raise SafeFilesystemError(f"Refusing to remove symlink tree root: {target}")
        if not stat.S_ISDIR(entry_stat.st_mode):
            os.unlink(target.name, dir_fd=parent_fd)
            return
        child_fd = _open_child_dir(parent_fd, target.name, target)
        try:
            _rmtree_contents_fd(child_fd, target)
        finally:
            os.close(child_fd)
        os.rmdir(target.name, dir_fd=parent_fd)
    except SafeFilesystemError:
        raise
    except OSError as error:
        raise SafeFilesystemError(f"Failed to remove tree {target}: {error}", kind="io") from error
    finally:
        os.close(parent_fd)


def _rmtree_contents_fd(dir_fd: int, path_label: Path) -> None:
    for name in os.listdir(dir_fd):
        entry_path = path_label / name
        try:
            entry_stat = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as error:
            raise SafeFilesystemError(f"Failed to stat tree entry {entry_path}: {error}", kind="io") from error
        if stat.S_ISLNK(entry_stat.st_mode):
            raise SafeFilesystemError(f"Refusing to remove symlink tree entry: {entry_path}")
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_child_dir(dir_fd, name, entry_path)
            try:
                _rmtree_contents_fd(child_fd, entry_path)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=dir_fd)
        else:
            os.unlink(name, dir_fd=dir_fd)


def rename_entry_no_follow(
    parent: Path,
    name: str,
    dest_parent: Path,
    dest_name: str,
    *,
    containment_root: Path | None = None,
) -> Path:
    """Rename a named entry through no-follow parent descriptors.

    Walk refusals preserve their safety kind; rename I/O is ``kind="io"``.
    """

    _reject_unsafe_entry_name(name)
    _reject_unsafe_entry_name(dest_name)
    source_label = _expand_path(parent) / name
    dest_label = _expand_path(dest_parent) / dest_name
    source_fd = open_directory_no_follow(parent, containment_root=containment_root)
    try:
        dest_fd = open_directory_no_follow(dest_parent, containment_root=containment_root)
        try:
            os.rename(name, dest_name, src_dir_fd=source_fd, dst_dir_fd=dest_fd)
        except OSError as error:
            raise SafeFilesystemError(
                f"Failed to rename {source_label} to {dest_label}: {error}",
                kind="io",
            ) from error
        finally:
            os.close(dest_fd)
    finally:
        os.close(source_fd)
    return dest_label


def remove_tree_allow_symlinks(
    parent: Path,
    name: str,
    *,
    containment_root: Path | None = None,
    missing_ok: bool = True,
) -> None:
    """Remove a disposable residue tree without following symlinks.

    Symlink entries are unlinked rather than treated as tampering; traversal and
    removal I/O is ``kind="io"``, while unsafe path walks retain their kind.
    """

    _reject_unsafe_entry_name(name)
    target = _expand_path(parent) / name
    try:
        parent_fd = open_directory_no_follow(parent, containment_root=containment_root)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    try:
        try:
            entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if not stat.S_ISDIR(entry_stat.st_mode):
            # Symlink, regular file or device node at the root: unlink the entry
            # itself.  ``S_ISDIR`` is false for a symlink under ``lstat`` even
            # when it points at a directory, so this is the no-follow branch.
            os.unlink(name, dir_fd=parent_fd)
            return
        child_fd = _open_child_dir(parent_fd, name, target)
        try:
            _remove_tree_contents_allow_symlinks_fd(child_fd, target)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    except SafeFilesystemError:
        raise
    except OSError as error:
        raise SafeFilesystemError(f"Failed to remove tree {target}: {error}", kind="io") from error
    finally:
        os.close(parent_fd)


def _remove_tree_contents_allow_symlinks_fd(dir_fd: int, path_label: Path) -> None:
    for name in os.listdir(dir_fd):
        entry_path = path_label / name
        try:
            entry_stat = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as error:
            raise SafeFilesystemError(f"Failed to stat tree entry {entry_path}: {error}", kind="io") from error
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_child_dir(dir_fd, name, entry_path)
            try:
                _remove_tree_contents_allow_symlinks_fd(child_fd, entry_path)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=dir_fd)
        else:
            os.unlink(name, dir_fd=dir_fd)


def _reject_unsafe_entry_name(name: str) -> None:
    if name in {"", ".", ".."} or "/" in name or os.sep in name:
        raise SafeFilesystemError(f"Unsafe directory entry name: {name!r}")


def _open_pinned_regular_entry(
    parent_fd: int,
    parent_path: Path,
    target: Path,
) -> tuple[int, os.stat_result]:
    try:
        _verify_fd_matches_path(parent_fd, parent_path)
        expected = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(expected.st_mode):
            raise SafeFilesystemError(f"Target file must not be a symlink: {target}")
        if not stat.S_ISREG(expected.st_mode):
            raise SafeFilesystemError(f"Target file must be a regular file: {target}")
        file_fd = os.open(target.name, _READ_FLAGS, dir_fd=parent_fd)
    except SafeFilesystemError:
        raise
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SafeFilesystemError(f"Target file must not be a symlink: {target}") from error
        raise SafeFilesystemError(f"Failed to open {target}: {error}", kind="io") from error
    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SafeFilesystemError(f"Target file must be a regular file: {target}")
        if (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino):
            raise SafeFilesystemError(f"Target file changed while being opened: {target}", kind="identity_changed")
        _verify_fd_matches_path(parent_fd, parent_path)
        return file_fd, opened
    except Exception:
        os.close(file_fd)
        raise


def _fsync_and_verify_parent(parent_fd: int, parent_path: Path, target: Path, *, proof: str) -> None:
    try:
        os.fsync(parent_fd)
        _verify_fd_matches_path(parent_fd, parent_path)
    except SafeFilesystemError as error:
        raise _indeterminate_proof_error(target, proof, error) from error
    except OSError as error:
        raise _indeterminate_proof_error(target, proof, error) from error


def _indeterminate_proof_error(target: Path, proof: str, error: BaseException) -> SafeFilesystemError:
    return SafeFilesystemError(
        f"{proof} proof for {target} is indeterminate after directory fsync: {error}", kind="indeterminate"
    )


def _open_parent_dir(
    path: Path,
    *,
    containment_root: Path | None,
    create: bool,
) -> tuple[int, Path]:
    if containment_root is not None:
        _relative_parts_under_root(path, containment_root)
    parent = path.parent
    if create:
        ensure_directory_no_follow(parent, containment_root=containment_root)
    root, parts = _anchor_for(parent, containment_root=containment_root)
    root_fd = _open_directory_no_follow(root)
    fd = root_fd
    try:
        for part in parts:
            next_fd = _open_child_dir(fd, part, parent)
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        if fd == root_fd:
            return os.dup(root_fd), parent
        parent_fd = fd
        fd = -1
        return parent_fd, parent
    finally:
        if fd != -1 and fd != root_fd:
            os.close(fd)
        os.close(root_fd)


def _open_child_dir(parent_fd: int, name: str, path_label: Path) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except NotADirectoryError as error:
        raise SafeFilesystemError(f"Path component is not a directory: {path_label}") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SafeFilesystemError(f"Path component must not be a symlink: {path_label}") from error
        raise SafeFilesystemError(f"Failed to open directory component {path_label}: {error}", kind="io") from error


def _open_verified_dir(path: Path) -> int:
    expected = _lstat_dir(path)
    try:
        fd = os.open(path, _DIR_FLAGS)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SafeFilesystemError(f"Directory must not be a symlink: {path}") from error
        raise SafeFilesystemError(f"Failed to open directory {path}: {error}", kind="io") from error
    try:
        opened = os.fstat(fd)
        if not _same_directory(expected, opened):
            raise SafeFilesystemError(f"Directory changed while being opened: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_directory_no_follow(path: Path) -> int:
    target = _expand_path(path)
    root, parts = _anchor_for(target, containment_root=None)
    root_fd = _open_verified_dir(root)
    fd = root_fd
    try:
        for part in parts:
            next_fd = _open_child_dir(fd, part, target)
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        if fd == root_fd:
            return os.dup(root_fd)
        directory_fd = fd
        fd = -1
        return directory_fd
    finally:
        if fd != -1 and fd != root_fd:
            os.close(fd)
        os.close(root_fd)


def _lstat_dir(path: Path) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as error:
        raise SafeFilesystemError(f"Failed to stat directory {path}: {error}", kind="io") from error
    if stat.S_ISLNK(result.st_mode):
        raise SafeFilesystemError(f"Directory must not be a symlink: {path}")
    if not stat.S_ISDIR(result.st_mode):
        raise SafeFilesystemError(f"Path must be a directory: {path}")
    return result


def _verify_fd_matches_path(fd: int, path: Path) -> None:
    current = _lstat_dir(path)
    opened = os.fstat(fd)
    if not _same_directory(current, opened):
        raise SafeFilesystemError(f"Directory changed while bound for operation: {path}")


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return stat.S_ISDIR(right.st_mode) and left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _reject_existing_symlink(parent_fd: int, name: str, path_label: Path) -> None:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise SafeFilesystemError(f"Failed to stat target {path_label}: {error}", kind="io") from error
    if stat.S_ISLNK(entry_stat.st_mode):
        raise SafeFilesystemError(f"Target file must not be a symlink: {path_label}")


def _anchor_for(path: Path, *, containment_root: Path | None) -> tuple[Path, tuple[str, ...]]:
    target = _expand_path(path)
    if containment_root is not None:
        root = _expand_path(containment_root)
        return root, _relative_parts_under_root(target, root)
    root = Path(target.anchor)
    if not root.anchor:
        raise SafeFilesystemError(f"No filesystem anchor for path: {target}")
    return root, _absolute_parts(target)


def _expand_path(path: Path) -> Path:
    """Expand a leading ``~`` and anchor the result at the current directory.

    ``Path.expanduser()`` throws a bare, errno-less ``RuntimeError`` when no home
    directory can be determined -- ``~<unknown user>``, or no passwd entry with
    ``HOME`` unset.  This prelude is shared by every public entry point of this
    module, so letting that throw out would defeat the module's advertised
    contract on all of them at once; it is converted here into the module's own
    structured error instead.

    Mind the direction: ``SafeFilesystemError`` **is** a ``RuntimeError``
    subclass, not the reverse.  Callers that already write
    ``except SafeFilesystemError`` therefore catch this with no change of their
    own, while the bare throw sailed past them -- and past every caller reading
    ``error.kind``, which a bare ``RuntimeError`` does not carry.

    The failure reuses the existing ``unsafe`` classification rather than adding
    a new one, so callers branching on the existing values keep a total set of
    branches.  Degrading to a permissive pass that keeps the literal ``~``
    component is not an option: the write-side primitives would then create or
    delete a path the operator never named.
    """

    try:
        expanded = Path(path).expanduser()
    except RuntimeError as error:
        raise SafeFilesystemError(
            f"Cannot determine the home directory for path: {path}",
            kind="unsafe",
        ) from error
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _absolute_parts(path: Path) -> tuple[str, ...]:
    parts = tuple(part for part in path.parts if part not in {path.anchor, ""})
    for part in parts:
        if part in {".", ".."}:
            raise SafeFilesystemError(f"Unsafe path component: {part!r}")
    return parts


def _relative_parts_under_root(path: Path, root: Path) -> tuple[str, ...]:
    target = _expand_path(path)
    containment_root = _expand_path(root)
    try:
        relative = target.relative_to(containment_root)
    except ValueError as error:
        raise SafeFilesystemError(f"Path must stay under containment root: {target}") from error
    parts = tuple(relative.parts)
    for part in parts:
        if part in {"", ".", ".."}:
            raise SafeFilesystemError(f"Unsafe path component under containment root: {part!r}")
    return parts


def _close_file_fd(file_fd: int | None) -> None:
    if file_fd is not None:
        try:
            os.close(file_fd)
        except OSError:
            pass


def _unlink_temp(parent_fd: int, temp_name: str) -> None:
    try:
        os.unlink(temp_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError:
        pass
