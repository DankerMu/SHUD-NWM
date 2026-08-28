from __future__ import annotations

import os
from pathlib import Path

from services.orchestrator import scheduler as _scheduler


def _expanduser_for_mode(value: Path | str, *, db_free_required: bool) -> Path:
    path = Path(value)
    try:
        return path.expanduser()
    except RuntimeError:
        if not db_free_required:
            raise
        return path


def _raw_config_path_preserve_components(value: Path | str, *, db_free_required: bool = False) -> Path:
    path = _expanduser_for_mode(value, db_free_required=db_free_required)
    if not path.is_absolute():
        return Path.cwd() / path
    return path


def _raw_config_path_relative_to_preserve_components(
    value: Path | str,
    base: Path,
    *,
    db_free_required: bool = False,
) -> Path:
    path = _expanduser_for_mode(value, db_free_required=db_free_required)
    if not path.is_absolute():
        return base / path
    return path


def _optional_raw_config_path_relative_to_preserve_components(
    value: Path | str | None,
    base: Path,
    *,
    db_free_required: bool = False,
) -> Path | None:
    if value in (None, ""):
        return None
    return _raw_config_path_relative_to_preserve_components(
        value,
        base,
        db_free_required=db_free_required,
    )


def _config_path_preserve_final_component_for_mode(value: Path | str, *, db_free_required: bool) -> Path:
    if not db_free_required:
        return _scheduler._config_path_preserve_final_component(value)
    path = _expanduser_for_mode(value, db_free_required=True)
    if not path.is_absolute():
        path = Path.cwd() / path
    return _safe_preserve_final_component(path)


def _config_path_relative_to_preserve_final_for_mode(
    value: Path | str,
    base: Path,
    *,
    db_free_required: bool,
) -> Path:
    if not db_free_required:
        return _scheduler._config_path_relative_to_preserve_final(value, base)
    path = _expanduser_for_mode(value, db_free_required=True)
    if not path.is_absolute():
        path = base / path
    return _safe_preserve_final_component(path)


def _optional_config_path_relative_to_preserve_final_for_mode(
    value: Path | str | None,
    base: Path,
    *,
    db_free_required: bool,
) -> Path | None:
    if value in (None, ""):
        return None
    return _config_path_relative_to_preserve_final_for_mode(
        value,
        base,
        db_free_required=db_free_required,
    )


def _safe_preserve_final_component(path: Path) -> Path:
    try:
        return path.parent.resolve(strict=False) / path.name
    except (OSError, RuntimeError):
        return path


def _resolve_config_path_for_mode(path: Path, *, db_free_required: bool) -> Path:
    if not db_free_required:
        try:
            return Path(os.path.realpath(path, strict=True))
        except OSError:
            # Same paradigm as _optional_config_path
            # (scheduler_runtime_roots.py:576-590): classification belongs to
            # the storage preflight, not to config construction, so hand the
            # canonicalised value down and let _storage_root_check report
            # SLURM_PREFLIGHT_<FIELD>_UNSAFE_PATH on every supported CPython
            # instead of aborting the whole process on <=3.12.
            #
            # A single non-strict os.path.realpath() covers every strict
            # failure: it never raises on 3.11-3.14 and reproduces the product
            # of the old non-strict Path.resolve() verbatim -- POSIX order,
            # symlinks first and `..` afterwards. Splitting on errno would buy
            # nothing, because both would-be lanes converge on this same
            # product (design D1).
            return Path(os.path.realpath(path))
    # The db-free arm is aligned with the arm above rather than given an errno
    # split of its own: this function has no rejection channel (it returns a
    # Path), so classification stays with the storage preflight. What the
    # alignment buys is one canonical form on both interpreter arms -- the old
    # `Path.resolve(strict=False)` returned a loop-bearing value unresolved on
    # <=3.12 (the errno-less RuntimeError landed in the except arm) and folded
    # on 3.13+, so downstream classification acted on two shapes. The except
    # tuple is deliberately OSError only: the fallback call raises ValueError
    # again for an unrepresentable path string, so catching ValueError here
    # would turn a pre-existing escape into an escape from inside the handler.
    # That escape is retained exactly as it stands today.
    #
    # The two arms are now TEXTUALLY IDENTICAL, and the split is retained
    # deliberately rather than collapsed into a single body: they rest on
    # different written bases -- the db-backed arm on issue #1423 / PR #1522
    # (whose own design D1 adopts the #1347 paradigm that was written for
    # _optional_config_path in scheduler_runtime_roots.py, a different function
    # in a different module), this one on this change's design D8 -- and either
    # may be re-decided on its own.
    # Collapsing them would erase the seam at which one of the two lanes can
    # later take an errno split without disturbing the other.
    try:
        return Path(os.path.realpath(path, strict=True))
    except OSError:
        return Path(os.path.realpath(path))


def _resolve_optional_config_path_for_mode(value: Path | None, *, db_free_required: bool) -> Path | None:
    if value is None:
        return None
    return _resolve_config_path_for_mode(value, db_free_required=db_free_required)


def _optional_config_path_for_mode(value: Path | str | None, *, db_free_required: bool) -> Path | None:
    if value in (None, ""):
        return None
    if not db_free_required:
        return _scheduler._optional_config_path(value)
    path = _expanduser_for_mode(value, db_free_required=True)
    if not path.is_absolute():
        path = Path.cwd() / path
    return _resolve_config_path_for_mode(path, db_free_required=True)


def _confined_path_for_mode(
    value: Path | str,
    workspace_root: Path,
    field_name: str,
    *,
    db_free_required: bool,
) -> Path:
    if not db_free_required:
        return _scheduler._confined_path(value, workspace_root, field_name)
    try:
        return _scheduler._confined_path(value, workspace_root, field_name)
    except (OSError, RuntimeError, ValueError):
        path = _expanduser_for_mode(value, db_free_required=True)
        if not path.is_absolute():
            path = workspace_root / path
        return _safe_preserve_final_component(path)


def _require_under_workspace_for_mode(
    path: Path,
    workspace_root: Path,
    field_name: str,
    *,
    db_free_required: bool,
) -> None:
    try:
        _scheduler._require_under_workspace(path, workspace_root, field_name)
    except (OSError, RuntimeError, ValueError):
        if not db_free_required:
            raise


def _require_safe_directory_final_component_for_mode(
    path: Path,
    workspace_root: Path,
    field_name: str,
    *,
    db_free_required: bool,
) -> None:
    try:
        _scheduler._require_safe_directory_final_component(path, workspace_root, field_name)
    except (OSError, RuntimeError, ValueError):
        if not db_free_required:
            raise
