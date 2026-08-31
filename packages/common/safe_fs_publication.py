"""Exclusive no-follow publication primitives.

These operations build on :mod:`packages.common.safe_fs` descriptor-bound path
validation.  They deliberately live apart from the general read/write helpers:
publication needs an ownership claim, no-clobber semantics, and durable parent
proof before a caller may treat a new artifact as authoritative.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from packages.common.safe_fs import (
    _DIR_FLAGS,
    _FILE_FLAGS,
    SafeFilesystemError,
    _close_file_fd,
    _expand_path,
    _fsync_and_verify_parent,
    _open_parent_dir,
    _reject_unsafe_entry_name,
    _verify_fd_matches_path,
    open_directory_no_follow,
)


def move_regular_file_no_follow_exclusive(
    parent: Path,
    name: str,
    dest_parent: Path,
    dest_name: str,
    *,
    containment_root: Path | None = None,
) -> Path:
    """Atomically move a regular file to an absent destination without clobbering.

    POSIX ``rename`` replaces a destination.  This primitive reserves the
    destination through ``link`` first, then unlinks the source only after the
    durable destination link exists.  Both names must share one filesystem;
    cross-device publication is rejected instead of silently falling back to a
    copy with weaker atomicity.
    """

    _reject_unsafe_entry_name(name)
    _reject_unsafe_entry_name(dest_name)
    source_label = _expand_path(parent) / name
    dest_label = _expand_path(dest_parent) / dest_name
    source_fd = open_directory_no_follow(parent, containment_root=containment_root)
    try:
        dest_fd = open_directory_no_follow(dest_parent, containment_root=containment_root)
        try:
            _verify_fd_matches_path(source_fd, _expand_path(parent))
            _verify_fd_matches_path(dest_fd, _expand_path(dest_parent))
            source_stat = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
                raise SafeFilesystemError(f"Move source must be a regular non-symlink file: {source_label}")
            os.link(name, dest_name, src_dir_fd=source_fd, dst_dir_fd=dest_fd, follow_symlinks=False)
            try:
                _fsync_and_verify_parent(dest_fd, _expand_path(dest_parent), dest_label, proof="exclusive-move-link")
            except SafeFilesystemError:
                # A durable link may have landed even when its proof failed.
                # Keep the source intact so a recovery pass can re-verify the
                # immutable destination rather than treating a split move as a
                # successful archive publication.
                raise
            os.unlink(name, dir_fd=source_fd)
            _fsync_and_verify_parent(source_fd, _expand_path(parent), source_label, proof="exclusive-move-unlink")
        except FileExistsError:
            raise
        except SafeFilesystemError:
            raise
        except OSError as error:
            # Once the hard link exists the destination is already a valid
            # durable copy; failure to remove the source is a recoverable
            # duplicate, not uncertainty about the published bytes.
            raise SafeFilesystemError(f"Failed to move {source_label} to {dest_label}: {error}", kind="io") from error
        finally:
            os.close(dest_fd)
    finally:
        os.close(source_fd)
    return dest_label


def make_directory_no_follow_exclusive(
    path: Path,
    *,
    containment_root: Path | None = None,
) -> Path:
    """Create one directory through no-follow parents, failing if it exists.

    Unlike :func:`ensure_directory_no_follow`, this is an ownership claim, not
    an idempotent ensure.  Once ``mkdir`` succeeds an error while proving the
    parent durable is indeterminate: callers must not assume they can safely
    reuse or remove that directory.
    """

    target = _expand_path(path)
    parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=True)
    created = False
    try:
        _verify_fd_matches_path(parent_fd, parent_path)
        os.mkdir(target.name, 0o755, dir_fd=parent_fd)
        created = True
        child_fd = os.open(target.name, _DIR_FLAGS, dir_fd=parent_fd)
        try:
            child_stat = os.fstat(child_fd)
            if not stat.S_ISDIR(child_stat.st_mode):
                raise SafeFilesystemError(f"Created entry is not a directory: {target}")
        finally:
            os.close(child_fd)
        _fsync_and_verify_parent(parent_fd, parent_path, target, proof="exclusive-directory-create")
    except FileExistsError:
        raise
    except SafeFilesystemError as error:
        if created and error.kind != "indeterminate":
            raise SafeFilesystemError(
                f"Directory {target} may have been created but its state is indeterminate: {error}",
                kind="indeterminate",
            ) from error
        raise
    except OSError as error:
        kind = "indeterminate" if created else "io"
        raise SafeFilesystemError(f"Failed to create exclusive directory {target}: {error}", kind=kind) from error
    finally:
        os.close(parent_fd)
    return target


def write_bytes_no_follow_exclusive(
    path: Path,
    content: bytes,
    *,
    containment_root: Path | None = None,
    require_durable_create: bool = False,
) -> Path:
    """Create a file without following symlinked parents or targets, failing if it exists.

    ``require_durable_create`` is for transaction reservations and manifests:
    after the exclusive create succeeds, parent fsync or parent-identity failure
    is reported as indeterminate instead of silently treating the creation as a
    durable fact.
    """

    target = _expand_path(path)
    parent_fd, parent_path = _open_parent_dir(target, containment_root=containment_root, create=True)
    file_fd: int | None = None
    created = False
    try:
        _verify_fd_matches_path(parent_fd, parent_path)
        file_fd = os.open(target.name, _FILE_FLAGS, 0o666, dir_fd=parent_fd)
        created = True
        view = memoryview(content)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        if require_durable_create:
            _fsync_and_verify_parent(parent_fd, parent_path, target, proof="exclusive-create")
        else:
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
    except FileExistsError:
        _close_file_fd(file_fd)
        raise
    except SafeFilesystemError:
        _close_file_fd(file_fd)
        raise
    except OSError as error:
        _close_file_fd(file_fd)
        kind = "indeterminate" if require_durable_create and created else "io"
        raise SafeFilesystemError(f"Failed to create {target}: {error}", kind=kind) from error
    finally:
        os.close(parent_fd)
    return target
