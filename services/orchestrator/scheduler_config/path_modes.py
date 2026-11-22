from __future__ import annotations

import os
from pathlib import Path

from services.orchestrator import scheduler as _scheduler


def _expanduser_for_mode(value: Path | str) -> Path:
    path = Path(value)
    try:
        return path.expanduser()
    except RuntimeError:
        return path


def _raw_config_path_preserve_components(value: Path | str) -> Path:
    path = _expanduser_for_mode(value)
    if not path.is_absolute():
        return Path.cwd() / path
    return path


def _raw_config_path_relative_to_preserve_components(
    value: Path | str,
    base: Path,
) -> Path:
    path = _expanduser_for_mode(value)
    if not path.is_absolute():
        return base / path
    return path


def _optional_raw_config_path_relative_to_preserve_components(
    value: Path | str | None,
    base: Path,
) -> Path | None:
    if value in (None, ""):
        return None
    return _raw_config_path_relative_to_preserve_components(value, base)


def _config_path_preserve_final_component_for_mode(value: Path | str) -> Path:
    path = _expanduser_for_mode(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return _safe_preserve_final_component(path)


def _config_path_relative_to_preserve_final_for_mode(
    value: Path | str,
    base: Path,
) -> Path:
    path = _expanduser_for_mode(value)
    if not path.is_absolute():
        path = base / path
    return _safe_preserve_final_component(path)


def _optional_config_path_relative_to_preserve_final_for_mode(
    value: Path | str | None,
    base: Path,
) -> Path | None:
    if value in (None, ""):
        return None
    return _config_path_relative_to_preserve_final_for_mode(value, base)


def _safe_preserve_final_component(path: Path) -> Path:
    try:
        return path.parent.resolve(strict=False) / path.name
    except (OSError, RuntimeError):
        return path


def _resolve_config_path_for_mode(path: Path) -> Path:
    # ADR 0009 clause 1: the eight root fields this feeds are dereferenced at their
    # use sites (open()/lstat()), so the non-strict fallback below is tolerated. This
    # is the single arm left once the database-backed one was deleted; see the
    # "修订（2026-11-22，#3311）" amendment section of
    # `docs/adr/0009-path-canonicalization-dereference-doctrine.md`.
    #
    # No errno split: this function has no rejection channel (it returns a Path),
    # so classification stays with the storage and root preflights. That is the
    # family ruling, not a local shortcut -- clause 1 hangs the non-strict
    # fallback's legitimacy on the CONSUMER's kernel dereference, and so does not
    # require a function without a rejection channel to manufacture a verdict it
    # has nowhere to report. What the strict-then-non-strict pair buys is one
    # canonical form on every interpreter -- the old `Path.resolve(strict=False)`
    # returned a loop-bearing value unresolved on <=3.12 (the errno-less
    # RuntimeError landed in the except arm) and folded it on 3.13+, so downstream
    # classification acted on two shapes. The except tuple is deliberately OSError
    # only: the fallback call raises ValueError again for an unrepresentable path
    # string, so catching ValueError here would turn a pre-existing escape into an
    # escape from inside the handler. That escape is retained exactly as it stands.
    try:
        return Path(os.path.realpath(path, strict=True))
    except OSError:
        return Path(os.path.realpath(path))


def _resolve_optional_config_path_for_mode(value: Path | None) -> Path | None:
    if value is None:
        return None
    return _resolve_config_path_for_mode(value)


def _optional_config_path_for_mode(value: Path | str | None) -> Path | None:
    if value in (None, ""):
        return None
    path = _expanduser_for_mode(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return _resolve_config_path_for_mode(path)


def _confined_path_for_mode(
    value: Path | str,
    workspace_root: Path,
    field_name: str,
) -> Path:
    try:
        return _scheduler._confined_path(value, workspace_root, field_name)
    except (OSError, RuntimeError, ValueError):
        path = _expanduser_for_mode(value)
        if not path.is_absolute():
            path = workspace_root / path
        return _safe_preserve_final_component(path)


def _require_under_workspace_for_mode(
    path: Path,
    workspace_root: Path,
    field_name: str,
) -> None:
    try:
        _scheduler._require_under_workspace(path, workspace_root, field_name)
    except (OSError, RuntimeError, ValueError):
        return


def _require_safe_directory_final_component_for_mode(
    path: Path,
    workspace_root: Path,
    field_name: str,
) -> None:
    try:
        _scheduler._require_safe_directory_final_component(path, workspace_root, field_name)
    except (OSError, RuntimeError, ValueError):
        return
