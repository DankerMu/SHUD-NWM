"""QHH bootstrap evidence reservation: exclusive no-follow reservation, write,
identity check, close and unlink of the evidence file (#2490 split of
``qhh_production_bootstrap``). The post-commit finalizer stays in the facade.

``workers.model_registry.qhh_production_bootstrap`` stays the stable import
path and re-exports every name defined here.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

from packages.common.safe_fs import (
    SafeFilesystemError,
    ensure_directory_no_follow,
    stat_no_follow,
    unlink_no_follow,
    verify_directory_no_follow,
)

from .qhh_bootstrap_contracts import (
    _EVIDENCE_DIR_FLAGS,
    _EVIDENCE_FILE_FLAGS,
    QhhEvidenceReservation,
    QhhProductionBootstrapError,
)


def _reserve_evidence_path(
    evidence_path: str | Path,
    *,
    evidence_dir: str | Path | None,
    model_id: str,
) -> QhhEvidenceReservation:
    if evidence_dir is None:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_ROOT_REQUIRED",
            "--evidence-dir is required when --evidence-path is provided.",
            model_id=model_id,
            path=str(evidence_path),
            details={"no_mutation_expected": True},
        )
    root = Path(evidence_dir).expanduser()
    root = root if root.is_absolute() else Path.cwd() / root
    target = Path(evidence_path).expanduser()
    target = target if target.is_absolute() else root / target
    try:
        verify_directory_no_follow(root)
        target.relative_to(root)
    except (ValueError, OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
            "QHH bootstrap evidence path must stay under evidence dir.",
            model_id=model_id,
            path=str(target),
            details={"evidence_dir": str(root), "no_mutation_expected": True},
        ) from error
    try:
        parent_fd = _open_evidence_parent_dir(target, root)
    except (OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
            "QHH bootstrap evidence parent cannot be safely prepared.",
            model_id=model_id,
            path=str(target),
            details={"reason": getattr(error, "kind", "io"), "no_mutation_expected": True},
        ) from error
    try:
        try:
            fd = os.open(target.name, _EVIDENCE_FILE_FLAGS, 0o666, dir_fd=parent_fd)
        except FileExistsError as error:
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_EVIDENCE_NO_CLOBBER",
                "QHH bootstrap evidence path already exists.",
                model_id=model_id,
                path=str(target),
                details={"no_mutation_expected": True},
            ) from error
        except OSError as error:
            reason = "unsafe" if error.errno == errno.ELOOP else "io"
            raise QhhProductionBootstrapError(
                "QHH_BOOTSTRAP_EVIDENCE_WRITE_FAILED",
                "QHH bootstrap evidence cannot be safely reserved.",
                model_id=model_id,
                path=str(target),
                details={"reason": reason, "no_mutation_expected": True},
            ) from error
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise QhhProductionBootstrapError(
                    "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
                    "QHH bootstrap evidence path must be a regular file.",
                    model_id=model_id,
                    path=str(target),
                    details={"no_mutation_expected": True},
                )
            identity = (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            return QhhEvidenceReservation(root=root, target=target, fd=fd, identity=identity)
        except Exception:
            try:
                os.close(fd)
            finally:
                _unlink_reserved_evidence_path(target, root, model_id=model_id)
            raise
    finally:
        os.close(parent_fd)


def _write_reserved_evidence_path(
    reservation: QhhEvidenceReservation,
    report: dict[str, Any],
    *,
    model_id: str,
) -> None:
    content = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        _require_reserved_evidence_identity(reservation, model_id=model_id)
        os.ftruncate(reservation.fd, 0)
        os.lseek(reservation.fd, 0, os.SEEK_SET)
        view = memoryview(content)
        while view:
            written = os.write(reservation.fd, view)
            view = view[written:]
        os.fsync(reservation.fd)
        os.close(reservation.fd)
        reservation.closed = True
    except QhhProductionBootstrapError:
        _close_reserved_evidence_fd(reservation)
        raise
    except OSError as error:
        _close_reserved_evidence_fd(reservation)
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_WRITE_FAILED",
            "QHH bootstrap evidence cannot be safely written to the reserved file.",
            model_id=model_id,
            path=str(reservation.target),
        ) from error


def _cleanup_reserved_evidence_path(reservation: QhhEvidenceReservation, *, model_id: str) -> None:
    _close_reserved_evidence_fd(reservation)
    _unlink_reserved_evidence_path(
        reservation.target,
        reservation.root,
        model_id=model_id,
        expected_identity=reservation.identity,
    )


def _close_reserved_evidence_fd(reservation: QhhEvidenceReservation) -> None:
    if reservation.closed:
        return
    reservation.closed = True
    try:
        os.close(reservation.fd)
    except OSError:
        pass


def _unlink_reserved_evidence_path(
    target: Path,
    root: Path,
    *,
    model_id: str,
    expected_identity: tuple[int, int, int] | None = None,
) -> None:
    try:
        if expected_identity is not None:
            try:
                path_stat = stat_no_follow(target, containment_root=root)
            except FileNotFoundError:
                return
            actual_identity = (path_stat.st_dev, path_stat.st_ino, stat.S_IFMT(path_stat.st_mode))
            if actual_identity != expected_identity:
                raise QhhProductionBootstrapError(
                    "QHH_BOOTSTRAP_EVIDENCE_CLEANUP_FAILED",
                    "QHH bootstrap evidence reservation changed before cleanup.",
                    model_id=model_id,
                    path=str(target),
                )
        unlink_no_follow(target, containment_root=root, missing_ok=True)
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_CLEANUP_FAILED",
            "QHH bootstrap evidence reservation could not be safely cleaned up.",
            model_id=model_id,
            path=str(target),
            details={"reason": error.kind},
        ) from error


def _require_reserved_evidence_identity(reservation: QhhEvidenceReservation, *, model_id: str) -> None:
    try:
        path_stat = stat_no_follow(reservation.target, containment_root=reservation.root)
        fd_stat = os.fstat(reservation.fd)
    except (OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
            "QHH bootstrap evidence reservation cannot be safely rebound.",
            model_id=model_id,
            path=str(reservation.target),
        ) from error
    path_identity = (path_stat.st_dev, path_stat.st_ino, stat.S_IFMT(path_stat.st_mode))
    fd_identity = (fd_stat.st_dev, fd_stat.st_ino, stat.S_IFMT(fd_stat.st_mode))
    if path_identity != reservation.identity or fd_identity != reservation.identity:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_EVIDENCE_PATH_UNSAFE",
            "QHH bootstrap evidence target changed after reservation.",
            model_id=model_id,
            path=str(reservation.target),
        )


def _open_evidence_parent_dir(target: Path, root: Path) -> int:
    target.relative_to(root)
    ensure_directory_no_follow(target.parent, containment_root=root)
    root_fd = os.open(root, _EVIDENCE_DIR_FLAGS)
    fd = root_fd
    try:
        relative_parent = target.parent.relative_to(root)
        for part in relative_parent.parts:
            if part in {"", ".", ".."}:
                raise SafeFilesystemError("Unsafe evidence path component.")
            next_fd = os.open(part, _EVIDENCE_DIR_FLAGS, dir_fd=fd)
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        if fd == root_fd:
            return os.dup(root_fd)
        parent_fd = fd
        fd = -1
        return parent_fd
    except OSError as error:
        raise SafeFilesystemError(f"Failed to open evidence parent directory: {error}", kind="io") from error
    finally:
        if fd != -1 and fd != root_fd:
            os.close(fd)
        os.close(root_fd)
