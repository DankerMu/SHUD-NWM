from __future__ import annotations

import errno
import os
import stat
import uuid
from pathlib import Path


class SafeFilesystemError(RuntimeError):
    """Raised when a filesystem operation cannot be completed safely."""

    def __init__(self, message: str, *, kind: str = "unsafe") -> None:
        super().__init__(message)
        self.kind = kind


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def verify_directory_no_follow(path: Path) -> Path:
    """Verify an existing directory by walking every component without following symlinks."""

    target = _expand_path(path)
    fd = _open_directory_no_follow(target)
    os.close(fd)
    return target


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
                    os.mkdir(part, dir_fd=fd)
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
) -> Path:
    """Atomically replace a file without following symlinked parents or targets."""

    target = _expand_path(path)
    parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=True)
    temp_name = f".{target.name}.{uuid.uuid4().hex}.{temp_suffix}"
    file_fd: int | None = None
    try:
        _verify_fd_matches_path(parent_fd, parent_path)
        _reject_existing_symlink(parent_fd, target.name, target)
        file_fd = os.open(temp_name, _FILE_FLAGS, 0o666, dir_fd=parent_fd)
        view = memoryview(content)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        _verify_fd_matches_path(parent_fd, parent_path)
        os.replace(temp_name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
    except SafeFilesystemError:
        _close_file_fd(file_fd)
        _unlink_temp(parent_fd, temp_name)
        raise
    except OSError as error:
        _close_file_fd(file_fd)
        _unlink_temp(parent_fd, temp_name)
        raise SafeFilesystemError(f"Failed to write {target}: {error}", kind="io") from error
    finally:
        os.close(parent_fd)
    return target


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
                raise SafeFilesystemError(f"Target file changed while being opened: {target}")
            _verify_fd_matches_path(parent_fd, parent_path)
            return file_fd
        except Exception:
            os.close(file_fd)
            raise
    finally:
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
        return os.read(file_fd, max_bytes + 1)
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
    expanded = Path(path).expanduser()
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
