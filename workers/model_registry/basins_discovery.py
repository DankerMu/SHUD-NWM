from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASINS_ROOT = Path("data/Basins")
NHMS_BASINS_ROOT_ENV = "NHMS_BASINS_ROOT"

IGNORED_SIDE_NAMES = {".DS_Store", "@eaDir"}
IGNORED_SIDE_SUFFIXES = ("@SynoEAStream",)

SHUD_REQUIRED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("cfg_para", "*.cfg.para"),
    ("cfg_ic", "*.cfg.ic"),
    ("cfg_calib", "*.cfg.calib"),
    ("sp_mesh", "*.sp.mesh"),
    ("sp_riv", "*.sp.riv"),
    ("sp_rivseg", "*.sp.rivseg"),
    ("sp_att", "*.sp.att"),
    ("para_soil", "*.para.soil"),
    ("para_geol", "*.para.geol"),
    ("para_lc", "*.para.lc"),
    ("tsd_forc", "*.tsd.forc"),
    ("tsd_lai", "*.tsd.lai"),
    ("tsd_mf", "*.tsd.mf"),
    ("tsd_rl", "*.tsd.rl"),
)

GIS_REQUIRED_FILES: tuple[tuple[str, str], ...] = tuple(
    (f"gis_{layer}_{suffix}", f"{layer}.{suffix}")
    for layer in ("domain", "river", "seg")
    for suffix in ("shp", "shx", "dbf", "prj")
)

CHECKSUM_LIMIT_BYTES = 16 * 1024 * 1024
BLOCKING_WARNING_CODES = {"BASINS_SYMLINK_OUTSIDE_ROOT", "BASINS_SYMLINK_UNRESOLVABLE"}


class BasinsDiscoveryError(RuntimeError):
    """Raised when Basins discovery cannot produce an importable inventory."""

    def __init__(self, error_code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.path = path

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.path is not None:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True)
class DiscoveryWarning:
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload


def resolve_basins_root(cli_root: str | None) -> Path:
    if cli_root:
        return Path(cli_root).expanduser()
    env_root = os.getenv(NHMS_BASINS_ROOT_ENV, "").strip()
    if env_root:
        return Path(env_root).expanduser()
    return DEFAULT_BASINS_ROOT


def discover_basins_inventory(basins_root: str | Path) -> dict[str, Any]:
    root = Path(basins_root).expanduser()
    if not root.exists():
        raise BasinsDiscoveryError("BASINS_ROOT_NOT_FOUND", f"Basins root does not exist: {root}", path=str(root))
    if not root.is_dir():
        raise BasinsDiscoveryError("BASINS_ROOT_NOT_FOUND", f"Basins root is not a directory: {root}", path=str(root))
    _ensure_readable_directory(root, "BASINS_ROOT_UNREADABLE")

    resolved_root = root.resolve()
    warnings: list[DiscoveryWarning] = []
    models = [
        _inventory_for_model(candidate, root, resolved_root, warnings)
        for candidate in _find_model_dirs(root, resolved_root, warnings)
    ]
    models.sort(key=lambda record: record["model_id"])
    has_blocking_warnings = any(warning.code in BLOCKING_WARNING_CODES for warning in warnings)

    return {
        "schema_version": "basins.discovery.v1",
        "root": str(root),
        "resolved_root": str(resolved_root),
        "source_is_symlink": root.is_symlink(),
        "models": models,
        "model_count": len(models),
        "warnings": [warning.as_dict() for warning in warnings],
        "importable": bool(models)
        and not has_blocking_warnings
        and not any(model["status"] != "valid" for model in models),
    }


def write_inventory(inventory: dict[str, Any], output_path: str | Path) -> None:
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _find_model_dirs(root: Path, resolved_root: Path, warnings: list[DiscoveryWarning]) -> list[Path]:
    candidates: list[Path] = []
    for entry in _iter_child_dirs(root):
        if _is_ignored_path(entry):
            continue
        resolved_entry = _safe_resolve_under_root(entry, resolved_root, warnings)
        if resolved_entry is None:
            continue
        _ensure_readable_directory(entry, "BASINS_DIRECTORY_UNREADABLE")
        if _has_child_dir(entry, "input", resolved_root, warnings):
            candidates.append(entry)
            continue
        for nested in _iter_child_dirs(entry):
            if _is_ignored_path(nested):
                continue
            resolved_nested = _safe_resolve_under_root(nested, resolved_root, warnings)
            if resolved_nested is None:
                continue
            _ensure_readable_directory(nested, "BASINS_DIRECTORY_UNREADABLE")
            if _has_child_dir(nested, "input", resolved_root, warnings):
                candidates.append(nested)
    return sorted(candidates, key=lambda path: path.relative_to(root).as_posix().lower())


def _inventory_for_model(
    model_dir: Path,
    root: Path,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
) -> dict[str, Any]:
    warning_start = len(warnings)
    basin_slug = model_dir.relative_to(root).as_posix()
    model_id = f"basins_{_slug_id(basin_slug)}_shud"
    quirks: list[str] = []
    sidecar_count = _count_sidecars(model_dir)
    if sidecar_count:
        quirks.append("generated_sidecars_ignored")

    input_parent = model_dir / "input"
    _safe_resolve_under_root(input_parent, resolved_root, warnings)
    _ensure_readable_directory(input_parent, "BASINS_DIRECTORY_UNREADABLE")
    input_dirs = sorted(
        (
            path
            for path in _iter_child_dirs(input_parent)
            if not _is_ignored_path(path) and _safe_resolve_under_root(path, resolved_root, warnings) is not None
        ),
        key=lambda path: path.name.lower(),
    )
    if not input_dirs:
        shud_input_name = ""
        input_dir = input_parent
        quirks.append("missing_input_dir")
    else:
        if len(input_dirs) > 1:
            quirks.append("multiple_input_dirs")
        input_dir = input_dirs[0]
        shud_input_name = input_dir.name
    _ensure_readable_directory(input_dir, "BASINS_DIRECTORY_UNREADABLE")

    gis_dir = input_dir / "gis"
    required_files, missing_required_files = _match_required_files(input_dir, gis_dir, resolved_root, warnings)
    checksums = _checksums_for_required_files(input_dir, required_files, resolved_root, warnings)
    calibration_count = _count_files(model_dir / "CALIB", resolved_root, warnings)
    forcing_info = _forcing_info(model_dir, quirks, warnings, resolved_root)
    if forcing_info["forcing_dir"] is not None:
        forcing_count = _count_csv_files(Path(forcing_info["forcing_dir"]), resolved_root, warnings)
    else:
        forcing_count = 0

    unsafe_descendant = any(warning.code in BLOCKING_WARNING_CODES for warning in warnings[warning_start:])
    if unsafe_descendant:
        quirks.append("unsafe_symlink_outside_root")
    status = "valid" if not missing_required_files and not unsafe_descendant else "partial"
    suggested_ids = {
        "basin_id": f"basins_{_slug_id(basin_slug)}",
        "basin_version_id": f"basins_{_slug_id(basin_slug)}_vbasins",
        "river_network_version_id": f"basins_{_slug_id(basin_slug)}_rivnet_vbasins",
        "mesh_version_id": f"basins_{_slug_id(basin_slug)}_mesh_vbasins",
        "model_id": model_id,
    }

    return {
        "basin_slug": basin_slug,
        "source_path": str(model_dir),
        "resolved_source_path": str(model_dir.resolve()),
        "source_is_symlink": model_dir.is_symlink(),
        "shud_input_name": shud_input_name,
        "input_dir": str(input_dir),
        "gis_dir": str(gis_dir),
        "forcing_dir": forcing_info["forcing_dir"],
        "forcing_dir_original_name": forcing_info["forcing_dir_original_name"],
        "status": status,
        "quirks": sorted(set(quirks)),
        "missing_required_files": missing_required_files,
        "required_files": required_files,
        "calibration_count": calibration_count,
        "forcing_csv_count": forcing_count,
        "model_id": model_id,
        "suggested_ids": suggested_ids,
        "checksums": checksums,
        "generated_sidecar_count": sidecar_count,
        "default_import_eligible": status == "valid",
        "default_publish_eligible": status == "valid",
        "root_relative_path": model_dir.relative_to(root).as_posix(),
        "root_relative_resolved_path": model_dir.resolve().relative_to(resolved_root).as_posix(),
    }


def _match_required_files(
    input_dir: Path,
    gis_dir: Path,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
) -> tuple[dict[str, list[str]], list[str]]:
    required: dict[str, list[str]] = {}
    missing: list[str] = []
    for role, pattern in SHUD_REQUIRED_PATTERNS:
        matches = _glob_non_sidecar_files(input_dir, pattern, resolved_root, warnings)
        required[role] = [str(path.relative_to(input_dir)) for path in matches]
        if not matches:
            missing.append(pattern)
    for role, file_name in GIS_REQUIRED_FILES:
        path = gis_dir / file_name
        if (
            not _is_ignored_path(path)
            and _safe_resolve_under_root(path, resolved_root, warnings) is not None
            and path.is_file()
        ):
            required[role] = [str(path.relative_to(input_dir))]
        else:
            required[role] = []
            missing.append(f"gis/{file_name}")
    return required, missing


def _checksums_for_required_files(
    input_dir: Path,
    required_files: dict[str, list[str]],
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for matches in required_files.values():
        for relative_name in matches:
            path = input_dir / relative_name
            try:
                if (
                    _safe_resolve_under_root(path, resolved_root, warnings) is not None
                    and path.stat().st_size <= CHECKSUM_LIMIT_BYTES
                ):
                    checksums[relative_name] = _sha256(path)
            except OSError:
                continue
    return checksums


def _forcing_info(
    model_dir: Path,
    quirks: list[str],
    warnings: list[DiscoveryWarning],
    resolved_root: Path,
) -> dict[str, str | None]:
    forcing = model_dir / "forcing"
    focing = model_dir / "focing"
    has_forcing = _is_safe_directory(forcing, resolved_root, warnings)
    has_focing = _is_safe_directory(focing, resolved_root, warnings)
    if has_forcing:
        _ensure_readable_directory(forcing, "BASINS_DIRECTORY_UNREADABLE")
    if has_focing:
        _ensure_readable_directory(focing, "BASINS_DIRECTORY_UNREADABLE")
    if has_forcing and has_focing:
        quirks.append("forcing_dir_conflict")
        warnings.append(
            DiscoveryWarning(
                "BASINS_FORCING_DIR_CONFLICT",
                "Both forcing/ and legacy focing/ exist; canonical forcing/ was selected.",
                path=str(model_dir),
            )
        )
        return {"forcing_dir": str(forcing), "forcing_dir_original_name": "forcing"}
    if has_forcing:
        return {"forcing_dir": str(forcing), "forcing_dir_original_name": "forcing"}
    if has_focing:
        quirks.append("legacy_focing_dir")
        return {"forcing_dir": str(focing), "forcing_dir_original_name": "focing"}
    return {"forcing_dir": None, "forcing_dir_original_name": None}


def _glob_non_sidecar_files(
    root: Path,
    pattern: str,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
) -> list[Path]:
    if not root.exists():
        return []
    matches: list[Path] = []
    for path in root.glob(pattern):
        if (
            not _is_ignored_path(path)
            and _safe_resolve_under_root(path, resolved_root, warnings) is not None
            and path.is_file()
        ):
            matches.append(path)
    return sorted(matches, key=lambda path: path.name.lower())


def _count_csv_files(root: Path, resolved_root: Path, warnings: list[DiscoveryWarning]) -> int:
    return sum(1 for path in _walk_files(root, resolved_root, warnings) if path.suffix.lower() == ".csv")


def _count_files(root: Path, resolved_root: Path, warnings: list[DiscoveryWarning]) -> int:
    if not _is_safe_directory(root, resolved_root, warnings):
        return 0
    return sum(1 for _ in _walk_files(root, resolved_root, warnings))


def _count_sidecars(root: Path) -> int:
    count = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    if _is_sidecar_name(entry.name):
                        count += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry_path)
        except OSError:
            continue
    return count


def _walk_files(root: Path, resolved_root: Path, warnings: list[DiscoveryWarning]) -> Iterator[Path]:
    if not _is_safe_directory(root, resolved_root, warnings):
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        if _safe_resolve_under_root(directory, resolved_root, warnings) is None:
            continue
        _ensure_readable_directory(directory, "BASINS_DIRECTORY_UNREADABLE")
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if _is_sidecar_name(entry.name):
                        continue
                    if _safe_resolve_under_root(path, resolved_root, warnings) is None:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        yield path
        except PermissionError as error:
            raise BasinsDiscoveryError(
                "BASINS_DIRECTORY_UNREADABLE",
                f"Basins directory is not readable: {directory}",
                path=str(directory),
            ) from error
        except OSError as error:
            raise BasinsDiscoveryError(
                "BASINS_DIRECTORY_UNREADABLE",
                f"Basins directory cannot be scanned: {directory}",
                path=str(directory),
            ) from error


def _iter_child_dirs(root: Path) -> list[Path]:
    try:
        with os.scandir(root) as entries:
            return sorted(
                (Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False) or entry.is_symlink()),
                key=lambda path: path.name.lower(),
            )
    except PermissionError as error:
        raise BasinsDiscoveryError(
            "BASINS_DIRECTORY_UNREADABLE",
            f"Basins directory is not readable: {root}",
            path=str(root),
        ) from error


def _has_child_dir(root: Path, name: str, resolved_root: Path, warnings: list[DiscoveryWarning]) -> bool:
    child = root / name
    return _is_safe_directory(child, resolved_root, warnings)


def _is_safe_directory(path: Path, resolved_root: Path, warnings: list[DiscoveryWarning]) -> bool:
    return (
        not _is_ignored_path(path)
        and _safe_resolve_under_root(path, resolved_root, warnings) is not None
        and path.is_dir()
    )


def _safe_resolve_under_root(
    path: Path,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
) -> Path | None:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        _append_warning_once(
            warnings,
            DiscoveryWarning(
                "BASINS_SYMLINK_UNRESOLVABLE",
                "Basins descendant cannot be resolved and was skipped.",
                path=str(path),
            ),
        )
        return None
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        _append_warning_once(
            warnings,
            DiscoveryWarning(
                "BASINS_SYMLINK_OUTSIDE_ROOT",
                "Basins descendant resolves outside the configured Basins root and was skipped.",
                path=str(path),
            ),
        )
        return None
    return resolved


def _append_warning_once(warnings: list[DiscoveryWarning], warning: DiscoveryWarning) -> None:
    if any(existing.code == warning.code and existing.path == warning.path for existing in warnings):
        return
    warnings.append(warning)


def _ensure_readable_directory(path: Path, error_code: str) -> None:
    if not path.is_dir():
        raise BasinsDiscoveryError(error_code, f"Basins directory does not exist: {path}", path=str(path))
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise BasinsDiscoveryError(error_code, f"Basins directory cannot be stat'ed: {path}", path=str(path)) from error
    if not mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
        raise BasinsDiscoveryError(error_code, f"Basins directory is not readable: {path}", path=str(path))
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise BasinsDiscoveryError(error_code, f"Basins directory is not searchable: {path}", path=str(path))


def _is_sidecar_name(name: str) -> bool:
    return name in IGNORED_SIDE_NAMES or any(name.endswith(suffix) for suffix in IGNORED_SIDE_SUFFIXES)


def _is_ignored_path(path: Path) -> bool:
    return any(_is_sidecar_name(part) for part in path.parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return normalized or "unknown"
