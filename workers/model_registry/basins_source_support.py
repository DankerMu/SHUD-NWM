"""Dependency-free Basins package source helpers: source-root containment guards,
relative-path normalization, ignored-path predicate, object-key binding and JSON
output writers (#2460 split of ``basins_package_source_io``).

``workers.model_registry.basins_package_source_io`` stays the stable import path
and re-exports every name defined here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .basins_package_contracts import BasinsPackageError, SourceFile, _json_bytes


def _bind_mapping_source_files(
    source_files: Sequence[SourceFile],
    *,
    object_store: Any,
    package_key: str,
) -> list[SourceFile]:
    """Bind package object locations while preserving validated mapping snapshots."""

    return [
        replace(
            source_file,
            object_key=f"{package_key}/{source_file.relative_path}",
            object_uri=object_store.uri_for_key(f"{package_key}/{source_file.relative_path}"),
        )
        for source_file in source_files
    ]


def _ensure_under_source_root(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    _ensure_under_root(
        path,
        source_root,
        error_code="BASINS_PACKAGE_PATH_UNSAFE",
        message="Basins package source path resolves outside the model source directory.",
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )


def _ensure_under_root(
    path: Path,
    root: Path,
    *,
    error_code: str,
    message: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise BasinsPackageError(
            error_code,
            message,
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        ) from error


def _normalize_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BasinsPackageError("BASINS_PACKAGE_PATH_UNSAFE", f"Unsafe package relative path: {value}", path=value)
    normalized = path.as_posix().strip("/")
    if not normalized:
        raise BasinsPackageError("BASINS_PACKAGE_PATH_UNSAFE", "Package relative path is empty.")
    return normalized


def _is_ignored_source_path(path: Path) -> bool:
    return any(part == ".DS_Store" or part == "@eaDir" or part.endswith("@SynoEAStream") for part in path.parts)


def _write_json_file(
    path: str | Path,
    payload: dict[str, Any],
    *,
    error_code: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
    before_write: Callable[[Path, int], None] | None = None,
) -> None:
    output = Path(path).expanduser()
    try:
        content = _json_bytes(payload)
        if before_write is not None:
            before_write(output, len(content))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    except OSError as error:
        raise BasinsPackageError(
            error_code,
            f"Failed to write Basins output JSON: {output}: {error}",
            model_id=model_id,
            version=version,
            path=str(output),
            manifest_uri=manifest_uri,
        ) from error


def _preflight_json_output_path(
    path: str | Path,
    *,
    error_code: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    output = Path(path).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BasinsPackageError(
            error_code,
            f"Failed to prepare Basins output JSON path: {output}: {error}",
            model_id=model_id,
            version=version,
            path=str(output),
            manifest_uri=manifest_uri,
        ) from error
