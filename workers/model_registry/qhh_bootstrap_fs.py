"""QHH bootstrap trusted-root and bounded file primitives: source-root
resolution, trusted directory bindings, contained/standalone bounded reads and
generated-JSON writes (#2490 split of ``qhh_production_bootstrap``).

``workers.model_registry.qhh_production_bootstrap`` stays the stable import
path and re-exports every name defined here.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
    verify_directory_no_follow,
)

from .basins_geometry import BasinsGeometryError, TrustedBasinsRoot, trusted_basins_root
from .qhh_bootstrap_contracts import QhhProductionBootstrapError


def _resolve_qhh_source_root(
    basins_root: Path,
    *,
    qhh_basin_slug: str,
    qhh_project_name: str,
    model_id: str,
) -> Path:
    if Path(qhh_basin_slug).is_absolute() or ".." in Path(qhh_basin_slug).parts:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PROJECT_PATH_UNSAFE",
            "QHH basin/project path must be relative and contained by NHMS_BASINS_ROOT.",
            model_id=model_id,
            details={"qhh_basin_slug": qhh_basin_slug, "no_mutation_expected": True},
        )
    source_root = basins_root / qhh_basin_slug
    _trusted_child_dir(source_root, basins_root, model_id=model_id, role="qhh_source_root")
    input_dir = source_root / "input" / qhh_project_name
    if not input_dir.exists():
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PROJECT_FILE_MISSING",
            "QHH project input directory is missing.",
            model_id=model_id,
            path=str(input_dir),
            details={"missing": [f"{qhh_basin_slug}/input/{qhh_project_name}"], "no_mutation_expected": True},
        )
    return source_root


def _trusted_basins_root(path: Path) -> Path:
    root = Path(path).expanduser()
    root = root if root.is_absolute() else Path(os.path.abspath(root))
    try:
        verify_directory_no_follow(root)
    except (OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_BASINS_ROOT_UNSAFE",
            "QHH Basins root must be an existing no-symlink directory.",
            path=str(root),
            details={"no_mutation_expected": True},
        ) from error
    return root


def _trusted_child_dir(path: Path, root: Path, *, model_id: str, role: str) -> TrustedBasinsRoot:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package path escapes the configured Basins root.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        ) from error
    try:
        verify_directory_no_follow(path)
        return trusted_basins_root(path, role=role)
    except (BasinsGeometryError, OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package directory must be a no-symlink contained directory.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        ) from error


def _safe_directory_binding(path: Path, *, model_id: str, role: str) -> dict[str, Any]:
    try:
        verified_path = verify_directory_no_follow(path)
        root = trusted_basins_root(verified_path, role=role)
    except (BasinsGeometryError, OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package directory cannot be safely bound.",
            model_id=model_id,
            path=getattr(error, "path", str(path)),
            details={"role": role, **getattr(error, "details", {}), "no_mutation_expected": True},
        ) from error
    return _safe_trusted_root_binding(root, model_id=model_id, role=role)


def _safe_trusted_root_binding(root: TrustedBasinsRoot, *, model_id: str, role: str) -> dict[str, Any]:
    try:
        verify_directory_no_follow(root.path)
        verified = trusted_basins_root(root.path, role=role)
    except (BasinsGeometryError, OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package directory cannot be safely rebound.",
            model_id=model_id,
            path=getattr(error, "path", str(root.path)),
            details={"role": role, **getattr(error, "details", {}), "no_mutation_expected": True},
        ) from error
    if verified != root:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH package directory binding changed during bootstrap preflight.",
            model_id=model_id,
            path=str(root.path),
            details={"role": role, "no_mutation_expected": True},
        )
    return {"path": root.resolved_path, "identity": root.identity}


def _require_contained_regular_file(path: Path, root: TrustedBasinsRoot, *, model_id: str, role: str) -> None:
    try:
        st = stat_no_follow(path, containment_root=root.path)
    except FileNotFoundError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PROJECT_FILE_MISSING",
            "QHH required project file is missing.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "missing": [path.name], "no_mutation_expected": True},
        ) from error
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH required project file path is unsafe.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if not stat.S_ISREG(st.st_mode):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PROJECT_FILE_UNSAFE",
            "QHH required project file must be a regular file.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        )


def _read_contained_file_limited(
    path: Path,
    root: TrustedBasinsRoot,
    *,
    max_bytes: int,
    error_code: str,
    model_id: str,
    role: str,
) -> bytes:
    _require_contained_regular_file(path, root, model_id=model_id, role=role)
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=root.path)
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH project file cannot be safely read.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if len(content) > max_bytes:
        raise QhhProductionBootstrapError(
            error_code,
            "QHH project file exceeds the bounded read limit.",
            model_id=model_id,
            path=str(path),
            details={"max_bytes": max_bytes, "observed_more_than": max_bytes, "no_mutation_expected": True},
        )
    return content


def _read_standalone_file_limited(
    path: Path,
    *,
    max_bytes: int,
    error_code: str,
    model_id: str,
    role: str,
) -> bytes:
    path = Path(path).expanduser()
    root = path.parent if path.parent != Path("") else Path(".")
    try:
        st = stat_no_follow(path, containment_root=root)
    except FileNotFoundError:
        raise
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input path is unsafe.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if not stat.S_ISREG(st.st_mode):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input path must be a regular file.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        )
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=root)
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input path cannot be safely read.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if len(content) > max_bytes:
        raise QhhProductionBootstrapError(
            error_code,
            "QHH bootstrap input exceeds the bounded read limit.",
            model_id=model_id,
            path=str(path),
            details={
                "role": role,
                "max_bytes": max_bytes,
                "observed_more_than": max_bytes,
                "no_mutation_expected": True,
            },
        )
    return content


def _bootstrap_work_dir(work_dir: str | Path | None, *, model_id: str) -> Path:
    raw = work_dir or os.getenv("NHMS_QHH_BOOTSTRAP_WORK_DIR", ".nhms-qhh-bootstrap")
    path = Path(raw).expanduser()
    path = path if path.is_absolute() else Path.cwd() / path
    try:
        ensure_directory_no_follow(path)
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_WORK_DIR_UNSAFE",
            "QHH bootstrap work directory is unsafe.",
            model_id=model_id,
            path=str(path),
            details={"reason": error.kind, "no_mutation_expected": True},
        ) from error
    return path


def _write_generated_json(path: Path, payload: dict[str, Any], *, model_id: str, role: str) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        ensure_directory_no_follow(path.parent)
        atomic_write_bytes_no_follow(path, content, containment_root=path.parent)
    except (OSError, SafeFilesystemError) as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_WORK_FILE_WRITE_FAILED",
            "QHH bootstrap generated file cannot be safely written.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        ) from error


def _require_contained_optional_existing_json(
    path: Path,
    *,
    containment_root: Path,
    model_id: str,
    role: str,
) -> None:
    try:
        st = stat_no_follow(path, containment_root=containment_root)
    except FileNotFoundError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_NOT_FOUND",
            "QHH bootstrap input file does not exist.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        ) from error
    except SafeFilesystemError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input file is unsafe.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "reason": error.kind, "no_mutation_expected": True},
        ) from error
    if not stat.S_ISREG(st.st_mode):
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_INPUT_PATH_UNSAFE",
            "QHH bootstrap input file must be regular.",
            model_id=model_id,
            path=str(path),
            details={"role": role, "no_mutation_expected": True},
        )


def _coerce_trusted_root(root: str | Path | TrustedBasinsRoot, *, role: str) -> TrustedBasinsRoot:
    if isinstance(root, TrustedBasinsRoot):
        return root
    try:
        return trusted_basins_root(Path(root), role=role)
    except BasinsGeometryError as error:
        raise QhhProductionBootstrapError(
            "QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE",
            "QHH containment root is unsafe.",
            path=error.path,
            details={"role": role, **error.details, "no_mutation_expected": True},
        ) from error
