from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from errno import EACCES, ENOENT, EPERM
from pathlib import Path
from typing import Any

from packages.common.state_qc import cfg_ic_header_shape

# #1813: v2 dropped `forcing_csv_count`, so inventory bytes no longer move with
# forcing CSV payload volume.
BASINS_DISCOVERY_SCHEMA_VERSION = "basins.discovery.v2"
BASINS_DISCOVERY_SCHEMA_VERSION_V1 = "basins.discovery.v1"

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

# Upper bound (bytes) on the leading line discovery reads out of a ``*.cfg.ic`` or
# ``*.sp.mesh`` to validate its declared counts. Both headers are a handful of
# whitespace-separated numbers; anything past this bound is not a header line, and
# the bound keeps discovery from pulling a multi-GB state file into memory.
HEADER_LINE_LIMIT_BYTES = 4 * 1024


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


class _ResolveState(Enum):
    """First-class per-call verdict for a path under the Basins root.

    Owners map the state -- never a shared-warning scan -- to their lane
    semantics: hard directory refusal, matched-file unreadable third state,
    optional skip, or blocking outside-root/unresolvable refusal.
    """

    RESOLVED = "resolved"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    OUTSIDE = "outside"
    UNRESOLVABLE = "unresolvable"


@dataclass(frozen=True)
class _ResolvedPath:
    state: _ResolveState
    path: Path | None = None


class _FileKind(Enum):
    """Errno-aware final-file metadata verdict after containment passes."""

    REGULAR = "regular"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    OTHER = "other"


@dataclass
class DiscoveryBudget:
    max_depth: int
    max_entries: int
    error_code_prefix: str = "BASINS"
    root: Path | None = None
    entries_seen: int = 0

    def enter(self, path: Path, *, depth: int | None = None) -> None:
        if depth is not None and depth > self.max_depth:
            raise BasinsDiscoveryError(
                f"{self.error_code_prefix}_DISCOVERY_DEPTH_EXCEEDED",
                "Basins discovery exceeded the allowed directory depth.",
                path=str(path),
            )
        self.entries_seen += 1
        if self.entries_seen > self.max_entries:
            raise BasinsDiscoveryError(
                f"{self.error_code_prefix}_DISCOVERY_ENTRY_LIMIT_EXCEEDED",
                "Basins discovery exceeded the allowed entry count.",
                path=str(self.root or path),
            )


def resolve_basins_root(cli_root: str | None) -> Path:
    if cli_root:
        return Path(cli_root).expanduser()
    env_root = os.getenv(NHMS_BASINS_ROOT_ENV, "").strip()
    if env_root:
        return Path(env_root).expanduser()
    return DEFAULT_BASINS_ROOT


def discover_basins_inventory(basins_root: str | Path, *, budget: DiscoveryBudget | None = None) -> dict[str, Any]:
    root = Path(basins_root).expanduser()
    root_is_symlink = _classify_basins_root_metadata(root, error_prefix="BASINS_ROOT")
    _ensure_readable_directory(root, "BASINS_ROOT_UNREADABLE")

    resolved_root = root.resolve()
    if budget is not None and budget.root is None:
        budget.root = root
    if budget is not None:
        budget.enter(root, depth=0)
    warnings: list[DiscoveryWarning] = []
    models = [
        _inventory_for_model(candidate, root, resolved_root, warnings, budget=budget)
        for candidate in _find_model_dirs(root, resolved_root, warnings, budget=budget)
    ]
    models.sort(key=lambda record: record["model_id"])
    has_blocking_warnings = any(warning.code in BLOCKING_WARNING_CODES for warning in warnings)

    return {
        "schema_version": BASINS_DISCOVERY_SCHEMA_VERSION,
        "root": str(root),
        "resolved_root": str(resolved_root),
        "source_is_symlink": root_is_symlink,
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


def _find_model_dirs(
    root: Path,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
    *,
    budget: DiscoveryBudget | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    for entry in _iter_child_dirs(root, budget=budget, depth=1):
        if _is_ignored_path(entry):
            continue
        # Required model-candidate depth: unreadable at either permission
        # moment is a hard refusal, never a silent omission that lets a valid
        # sibling keep the inventory importable (cand-r1-01, depth retro).
        if not _require_readable_directory(
            entry,
            resolved_root,
            warnings,
            label="Basins model",
        ):
            continue
        if _has_child_dir(entry, "input", resolved_root, warnings):
            candidates.append(entry)
            continue
        for nested in _iter_child_dirs(entry, budget=budget, depth=2):
            if _is_ignored_path(nested):
                continue
            if not _require_readable_directory(
                nested,
                resolved_root,
                warnings,
                label="Basins model",
            ):
                continue
            if _has_child_dir(nested, "input", resolved_root, warnings):
                candidates.append(nested)
    return sorted(candidates, key=lambda path: path.relative_to(root).as_posix().lower())


def _inventory_for_model(
    model_dir: Path,
    root: Path,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
    *,
    budget: DiscoveryBudget | None = None,
) -> dict[str, Any]:
    warning_start = len(warnings)
    basin_slug = model_dir.relative_to(root).as_posix()
    model_id = f"basins_{_slug_id(basin_slug)}_shud"
    quirks: list[str] = []
    sidecar_count = _count_sidecars(model_dir, budget=budget, depth=_relative_depth(root, model_dir))
    if sidecar_count:
        quirks.append("generated_sidecars_ignored")

    input_parent = model_dir / "input"
    # TOCTOU defense: the parent required-input layer is re-verified here
    # (depth retro) through the same centralized owner as discovery.
    if not _require_readable_directory(
        input_parent,
        resolved_root,
        warnings,
        label="Basins model input",
    ):
        input_dirs = []
    else:
        input_dirs = []
        for path in _iter_child_dirs(
            input_parent, budget=budget, depth=_relative_depth(root, input_parent) + 1
        ):
            if _is_ignored_path(path):
                continue
            # The deepest required alias depth (`input/<shud_input_name>`): the
            # owner inspects each child result explicitly -- an UNREADABLE alias
            # is a hard refusal, never a generator predicate that silently drops
            # it into missing_input_dir (phase 6.2 audit 2).
            if _require_readable_directory(
                path,
                resolved_root,
                warnings,
                label="Basins model input alias",
            ):
                input_dirs.append(path)
        input_dirs = sorted(input_dirs, key=lambda path: path.name.lower())
    if not input_dirs:
        shud_input_name = ""
        input_dir = input_parent
        quirks.append("missing_input_dir")
    else:
        if len(input_dirs) > 1:
            quirks.append("multiple_input_dirs")
        input_dir = input_dirs[0]
        shud_input_name = input_dir.name

    gis_dir = input_dir / "gis"
    required_files, missing_required_files = _match_required_files(input_dir, gis_dir, resolved_root, warnings)
    invalid_required_files = _invalid_required_files(input_dir, required_files, resolved_root, warnings)
    checksums, unreadable_required_files = _checksums_for_required_files(
        input_dir, required_files, resolved_root, warnings
    )
    calibration_count = _count_files(
        model_dir / "CALIB",
        resolved_root,
        warnings,
        budget=budget,
        depth=_relative_depth(root, model_dir / "CALIB"),
    )
    # #1813: the inventory document is hashed raw into the package manifest's
    # `source_inventory_checksum`, which the cutover gate treats as a model
    # identity field.  Counting forcing CSVs here made adding or deleting a
    # historical CSV indistinguishable from a mesh recalibration, so the count
    # is not collected at all.  Structural facts (`forcing_dir`,
    # `forcing_dir_original_name`, forcing quirks) stay: packaging resolves the
    # forcing source directory from them.
    forcing_info = _forcing_info(model_dir, quirks, warnings, resolved_root)

    unsafe_descendant = any(warning.code in BLOCKING_WARNING_CODES for warning in warnings[warning_start:])
    if unsafe_descendant:
        quirks.append("unsafe_symlink_outside_root")
    if invalid_required_files:
        quirks.append("invalid_required_file_content")
    if unreadable_required_files:
        quirks.append("unreadable_required_file")
    status = (
        "valid"
        if not missing_required_files
        and not invalid_required_files
        and not unreadable_required_files
        and not unsafe_descendant
        else "partial"
    )
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
        # Guarded bare probe: a candidate only reaches _inventory_for_model
        # after _safe_resolve_under_root succeeded and _ensure_readable_directory
        # stat'ed it, so the lstat-based is_symlink() here has no pending EACCES
        # to leak on any CPython (#1554).
        "source_is_symlink": model_dir.is_symlink(),
        "shud_input_name": shud_input_name,
        "input_dir": str(input_dir),
        "gis_dir": str(gis_dir),
        "forcing_dir": forcing_info["forcing_dir"],
        "forcing_dir_original_name": forcing_info["forcing_dir_original_name"],
        "status": status,
        "quirks": sorted(set(quirks)),
        "missing_required_files": missing_required_files,
        "invalid_required_files": invalid_required_files,
        "unreadable_required_files": unreadable_required_files,
        "required_files": required_files,
        "calibration_count": calibration_count,
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
        if _is_ignored_path(path):
            required[role] = []
            missing.append(f"gis/{file_name}")
            continue
        resolution = _safe_resolve_under_root(path, resolved_root, warnings)
        if resolution.state is _ResolveState.UNREADABLE:
            # Permission denial is a matched-but-unreadable verdict, not a
            # missing-file verdict (#1554); the checksum walk records it.
            required[role] = [str(path.relative_to(input_dir))]
            continue
        if resolution.state is not _ResolveState.RESOLVED:
            required[role] = []
            missing.append(f"gis/{file_name}")
            continue
        file_kind = _classify_regular_file(path)
        if file_kind is _FileKind.REGULAR or file_kind is _FileKind.UNREADABLE:
            required[role] = [str(path.relative_to(input_dir))]
        else:
            required[role] = []
            missing.append(f"gis/{file_name}")
    return required, missing


def _invalid_required_files(
    input_dir: Path,
    required_files: dict[str, list[str]],
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
) -> list[str]:
    """Return locatable reasons why matched required files are unusable, if any.

    Existence (``missing_required_files``) is not enough for the ``*.cfg.ic``:
    issue #1197 shipped a present, non-empty, checksum-clean IC whose header line
    was ``23106\\t6`` -- two numeric tokens instead of the native three. The
    runtime reads the LAST numeric token as the minute-time, so that header made
    the mesh-state COLUMN COUNT be overwritten with an epoch-minute at first real
    consumption and SHUD allocated ~183 GB. This is where the delivery is refused
    instead: EVERY matched ``*.cfg.ic`` header is validated, cross-checked against
    the mesh element count on the single matched ``*.sp.mesh`` first line.

    Three refusal channels are kept DISTINCT so a receipt says which one fired:
    a shape violation, a header line that could not be read, and an ambiguous /
    unparseable ``*.sp.mesh`` that leaves the cross-check without a subject.
    Reasons are returned (never raised) so one malformed model does not abort
    discovery of the rest, and they land in ``invalid_required_files`` rather
    than ``missing_required_files``, whose consumers compare the glob-pattern set
    for exact repairability matches.
    """

    ic_matches = required_files.get("cfg_ic") or []
    if not ic_matches:
        # Absence is already reported through ``missing_required_files``; there is
        # no content to validate and no second verdict to add.
        return []

    invalid: list[str] = []
    expected_mesh_count: int | None = None
    mesh_matches = required_files.get("sp_mesh") or []
    if len(mesh_matches) > 1:
        listed = ", ".join(sorted(mesh_matches))
        invalid.append(
            f"{listed}: model matches {len(mesh_matches)} *.sp.mesh files; "
            "the IC mesh-count cross-check has no unambiguous element count"
        )
    elif len(mesh_matches) == 1:
        mesh_relative = mesh_matches[0]
        mesh_kind = _read_header_line(input_dir / mesh_relative, resolved_root, warnings)
        if mesh_kind.unreadable:
            # Permission-denied matches land only in unreadable_required_files
            # (cand-r1-05); the checksum walk records them, never content shape.
            expected_mesh_count = None
        elif mesh_kind.line is None:
            invalid.append(f"{mesh_relative}: mesh header line could not be read")
        else:
            expected_mesh_count = _leading_int_token(mesh_kind.line)
            if expected_mesh_count is None:
                invalid.append(
                    f"{mesh_relative}: mesh header line declares no leading integer element count"
                )

    for ic_relative in ic_matches:
        header_kind = _read_header_line(input_dir / ic_relative, resolved_root, warnings)
        if header_kind.unreadable:
            # Same as the mesh arm: unreadable-only, never invalid content.
            continue
        header_line = header_kind.line
        if header_line is None:
            invalid.append(f"{ic_relative}: IC header line could not be read")
            continue
        shape = cfg_ic_header_shape(header_line.split(), expected_mesh_count=expected_mesh_count)
        if not shape.valid:
            invalid.append(f"{ic_relative}: {shape.reason}")
    return invalid


@dataclass(frozen=True)
class _HeaderRead:
    line: str | None
    unreadable: bool = False


def _read_header_line(path: Path, resolved_root: Path, warnings: list[DiscoveryWarning]) -> _HeaderRead:
    """Return the bounded first line of ``path`` with an unreadable flag.

    ``unreadable=True`` means permission denial at resolution/stat/open: the
    matched file belongs only to the unreadable third state and must never be
    reported as a content-shape violation (cand-r1-05).  ``line is None`` with
    ``unreadable=False`` is the "could not be read" verdict -- an empty or
    whitespace-only first line is returned as-is so the caller reports it as a
    shape violation (zero numeric tokens) rather than conflating it with
    unreadability.
    """

    resolution = _safe_resolve_under_root(path, resolved_root, warnings)
    if resolution.state is _ResolveState.UNREADABLE:
        return _HeaderRead(line=None, unreadable=True)
    if resolution.state is not _ResolveState.RESOLVED:
        return _HeaderRead(line=None)
    try:
        with path.open("rb") as handle:
            chunk = handle.read(HEADER_LINE_LIMIT_BYTES)
    except PermissionError:
        return _HeaderRead(line=None, unreadable=True)
    except OSError:
        return _HeaderRead(line=None)
    try:
        return _HeaderRead(line=chunk.split(b"\n", 1)[0].decode("utf-8"))
    except UnicodeDecodeError:
        # Not a text header at all: unreadable for header purposes, and reported
        # through the unreadable channel rather than as a token-count verdict.
        return _HeaderRead(line=None)


def _leading_int_token(line: str) -> int | None:
    tokens = line.split()
    if not tokens:
        return None
    try:
        value = float(tokens[0])
    except ValueError:
        return None
    if not value.is_integer():
        return None
    return int(value)


def _checksums_for_required_files(
    input_dir: Path,
    required_files: dict[str, list[str]],
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
) -> tuple[dict[str, str], list[str]]:
    """Return the required-file checksums plus the files that could not be read.

    A matched required file whose stat/hash raises OSError (EACCES, EIO, an ENOENT
    race) used to be skipped silently: no checksum entry, no quirk, no warning, so
    "present but unreadable" registered as a healthy model. It is now a third state
    of its own, returned as a locatable reason list the caller feeds into ``status``
    the way ``invalid_required_files`` is fed -- quirks alone do not drive status.
    ``missing_required_files`` is untouched (the file WAS matched), and the
    unsafe-symlink skip keeps its own pre-existing ``BASINS_SYMLINK_*`` channel
    rather than being folded in here. Files above ``CHECKSUM_LIMIT_BYTES`` keep
    today's deliberate no-checksum-no-verdict behavior.
    """

    checksums: dict[str, str] = {}
    unreadable: list[str] = []
    for matches in required_files.values():
        for relative_name in matches:
            path = input_dir / relative_name
            resolution = _safe_resolve_under_root(path, resolved_root, warnings)
            if resolution.state is _ResolveState.UNREADABLE:
                # The strict walk proved permission denial, not a symlink
                # defect and not absence: this matched required file is
                # unreadable (#1554).  It lands in the existing third state and
                # never in missing_required_files or a SYMLINK_* arm.
                unreadable.append(f"{relative_name}: required file could not be read for checksum")
                _append_warning_once(
                    warnings,
                    DiscoveryWarning(
                        "BASINS_REQUIRED_FILE_UNREADABLE",
                        "Required file was matched but could not be read for checksum.",
                        path=str(path),
                    ),
                )
                continue
            if resolution.state is not _ResolveState.RESOLVED:
                continue
            try:
                if path.stat().st_size <= CHECKSUM_LIMIT_BYTES:
                    checksums[relative_name] = _sha256(path)
            except OSError:
                unreadable.append(f"{relative_name}: required file could not be read for checksum")
                _append_warning_once(
                    warnings,
                    DiscoveryWarning(
                        "BASINS_REQUIRED_FILE_UNREADABLE",
                        "Required file was matched but could not be read for checksum.",
                        path=str(path),
                    ),
                )
    return checksums, unreadable


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
    # The old ``root.exists()`` guard swallowed a different OSError set per
    # CPython (EACCES raises on 3.11 but returns False from 3.12 on), so it
    # could leak a raw PermissionError exactly where #1554 says a bare
    # predicate must not be probed unguarded.  The first-class resolution
    # classifies by errno instead: MISSING on the root means no matches, while
    # a permission-denied root is an unreadable skip (not a missing label).
    root_resolution = _safe_resolve_under_root(root, resolved_root, warnings)
    if root_resolution.state in (_ResolveState.MISSING, _ResolveState.UNREADABLE, _ResolveState.OUTSIDE):
        return []
    if root_resolution.state is not _ResolveState.RESOLVED:
        return []
    matches: list[Path] = []
    for path in root.glob(pattern):
        if _is_ignored_path(path):
            continue
        resolution = _safe_resolve_under_root(path, resolved_root, warnings)
        if resolution.state is _ResolveState.UNREADABLE:
            # The strict walk proved permission denial, not absence and not a
            # symlink defect (#1554): the file IS matched by discovery and must
            # not be mislabelled missing.  The checksum walk later routes it
            # into the unreadable-required-file third state.
            matches.append(path)
            continue
        if resolution.state is not _ResolveState.RESOLVED:
            continue
        file_kind = _classify_regular_file(path)
        if file_kind is _FileKind.REGULAR or file_kind is _FileKind.UNREADABLE:
            # A permission-denied final follow-stat is a matched-but-unreadable
            # verdict (cand-r1-06), not missing; the checksum walk records it.
            matches.append(path)
    return sorted(matches, key=lambda path: path.name.lower())


def _count_files(
    root: Path,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
    *,
    budget: DiscoveryBudget | None = None,
    depth: int = 0,
) -> int:
    if not _is_safe_directory(root, resolved_root, warnings):
        return 0
    return sum(1 for _ in _walk_files(root, resolved_root, warnings, budget=budget, start_depth=depth))


def _count_sidecars(root: Path, *, budget: DiscoveryBudget | None = None, depth: int = 0) -> int:
    count = 0
    stack: list[tuple[Path, int]] = [(root, depth)]
    while stack:
        directory, directory_depth = stack.pop()
        if budget is not None:
            budget.enter(directory, depth=directory_depth)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    if budget is not None:
                        budget.enter(entry_path, depth=directory_depth + 1)
                    if _is_sidecar_name(entry.name):
                        count += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry_path, directory_depth + 1))
        except OSError:
            continue
    return count


def _walk_files(
    root: Path,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
    *,
    budget: DiscoveryBudget | None = None,
    start_depth: int = 0,
) -> Iterator[Path]:
    if not _is_safe_directory(root, resolved_root, warnings):
        return
    stack: list[tuple[Path, int]] = [(root, start_depth)]
    while stack:
        directory, depth = stack.pop()
        if budget is not None:
            budget.enter(directory, depth=depth)
        if _safe_resolve_under_root(directory, resolved_root, warnings).state is not _ResolveState.RESOLVED:
            continue
        _ensure_readable_directory(directory, "BASINS_DIRECTORY_UNREADABLE")
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if budget is not None:
                        budget.enter(path, depth=depth + 1)
                    if _is_sidecar_name(entry.name):
                        continue
                    if _safe_resolve_under_root(path, resolved_root, warnings).state is not _ResolveState.RESOLVED:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((path, depth + 1))
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


def _iter_child_dirs(root: Path, *, budget: DiscoveryBudget | None = None, depth: int = 0) -> list[Path]:
    try:
        with os.scandir(root) as entries:
            paths: list[Path] = []
            for entry in entries:
                path = Path(entry.path)
                if budget is not None:
                    budget.enter(path, depth=depth)
                if entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                    paths.append(path)
            return sorted(paths, key=lambda path: path.name.lower())
    except PermissionError as error:
        raise BasinsDiscoveryError(
            "BASINS_DIRECTORY_UNREADABLE",
            f"Basins directory is not readable: {root}",
            path=str(root),
        ) from error


def _require_readable_directory(
    path: Path,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
    *,
    label: str,
) -> bool:
    """Centralized required-directory owner for every required-depth layer.

    One mapping for model candidates, required ``input/`` parents, and the
    required ``input/<shud_input_name>`` alias children (depth retro): the two
    permission moments must converge, so both strict-resolution UNREADABLE and
    final-follow-stat UNREADABLE raise the same exact
    ``BASINS_DIRECTORY_UNREADABLE``.  MISSING or non-directory is False under
    the established missing semantics; OUTSIDE/UNRESOLVABLE is False with the
    resolver's already-recorded blocking warning (never hard-mapped to a
    permission refusal).  A RESOLVED directory additionally keeps the
    mode-bit readable/searchable enforcement.
    """

    resolution = _safe_resolve_under_root(path, resolved_root, warnings)
    if resolution.state is _ResolveState.UNREADABLE:
        raise BasinsDiscoveryError(
            "BASINS_DIRECTORY_UNREADABLE",
            f"{label} directory is not readable: {path}",
            path=str(path),
        )
    if resolution.state is not _ResolveState.RESOLVED:
        return False
    kind = _classify_directory_kind(path)
    if kind is _FileKind.UNREADABLE:
        raise BasinsDiscoveryError(
            "BASINS_DIRECTORY_UNREADABLE",
            f"{label} directory is not readable: {path}",
            path=str(path),
        )
    if kind is not _FileKind.REGULAR:
        return False
    # Mode-bit readable/searchable enforcement, mirroring the old
    # _ensure_readable_directory gate for a present required directory.
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise BasinsDiscoveryError(
            "BASINS_DIRECTORY_UNREADABLE",
            f"{label} directory cannot be stat'ed: {path}",
            path=str(path),
        ) from error
    if not mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
        raise BasinsDiscoveryError(
            "BASINS_DIRECTORY_UNREADABLE",
            f"{label} directory is not readable: {path}",
            path=str(path),
        )
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise BasinsDiscoveryError(
            "BASINS_DIRECTORY_UNREADABLE",
            f"{label} directory is not searchable: {path}",
            path=str(path),
        )
    return True


def _has_child_dir(root: Path, name: str, resolved_root: Path, warnings: list[DiscoveryWarning]) -> bool:
    """True when ``root/name`` is a required ``input/`` directory.

    Delegates the required-directory verdict to the centralized owner; a
    required ``input/`` that is unreadable at either permission moment is a
    hard refusal, never a silent optional-skip (phase 6.2, depth retro).
    """

    return _require_readable_directory(
        root / name,
        resolved_root,
        warnings,
        label="Basins model input",
    )


def _is_safe_directory(path: Path, resolved_root: Path, warnings: list[DiscoveryWarning]) -> bool:
    """Optional-directory predicate: unreadable metadata is a warning/skip.

    Used for forcing/focing/CALIB and model-enumeration candidates only --
    never for a required ``input/`` (see ``_has_child_dir``).  OUTSIDE and
    UNRESOLVABLE skip here with the resolver's already-recorded blocking
    warning; a contained unreadable optional directory is a non-symlink
    unreadability skip, never a raw PermissionError.
    """

    if _is_ignored_path(path):
        return False
    resolution = _safe_resolve_under_root(path, resolved_root, warnings)
    if resolution.state is not _ResolveState.RESOLVED:
        return False
    kind = _classify_directory_kind(path)
    if kind is _FileKind.UNREADABLE:
        _append_warning_once(
            warnings,
            DiscoveryWarning(
                "BASINS_PATH_UNREADABLE",
                "Basins directory cannot be read and was not classified as a symlink defect.",
                path=str(path),
            ),
        )
        return False
    return kind is _FileKind.REGULAR


def _safe_resolve_under_root(
    path: Path,
    resolved_root: Path,
    warnings: list[DiscoveryWarning],
) -> _ResolvedPath:
    """Resolve ``path`` and classify containment/errno into a first-class result.

    The shared ``warnings`` list is OUTPUT ONLY -- no caller may reconstruct a
    verdict by scanning it.  Every failure arm below still runs containment on
    a non-strict realpath product, so an EACCES that hides an escape cannot
    weaken the fail-closed outside-root refusal (cand-r1-01/security-perf).
    """

    try:
        resolved = Path(os.path.realpath(path, strict=True))
        strict_errno = None
    except OSError as error:
        strict_errno = getattr(error, "errno", None)
        # Non-strict os.path.realpath() never raises on 3.11-3.14; Path.resolve()
        # must not be used because on <=3.12 it raises an errno-less RuntimeError
        # when the `..`-collapsed tail meets a symlink loop behind a missing
        # component (e.g. `gone/../loopdir`).
        resolved = Path(os.path.realpath(path))
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
        return _ResolvedPath(state=_ResolveState.OUTSIDE)
    if strict_errno is None:
        return _ResolvedPath(state=_ResolveState.RESOLVED, path=resolved)
    if strict_errno == ENOENT:
        # The strict walk proved a missing component, not a loop; keep the
        # pre-change nonexistence semantics (the caller filters silently).
        return _ResolvedPath(state=_ResolveState.MISSING)
    if strict_errno in (EACCES, EPERM):
        # Permission denial is NOT a symlink verdict and NOT nonexistence
        # (#1554).  Containment already passed above, so the path stays under
        # the root: report the non-symlink unreadability state and let the
        # owner decide (hard directory refusal, optional skip, or required-file
        # third state).  Never return it as RESOLVED.
        _append_warning_once(
            warnings,
            DiscoveryWarning(
                "BASINS_PATH_UNREADABLE",
                "Basins descendant cannot be read and was not classified as a symlink defect.",
                path=str(path),
            ),
        )
        return _ResolvedPath(state=_ResolveState.UNREADABLE)
    _append_warning_once(
        warnings,
        DiscoveryWarning(
            "BASINS_SYMLINK_UNRESOLVABLE",
            "Basins descendant cannot be resolved and was skipped.",
            path=str(path),
        ),
    )
    return _ResolvedPath(state=_ResolveState.UNRESOLVABLE)


def _append_warning_once(warnings: list[DiscoveryWarning], warning: DiscoveryWarning) -> None:
    if any(existing.code == warning.code and existing.path == warning.path for existing in warnings):
        return
    warnings.append(warning)


def _ensure_readable_directory(path: Path, error_code: str) -> None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise BasinsDiscoveryError(error_code, f"Basins directory does not exist: {path}", path=str(path))
    except NotADirectoryError:
        raise BasinsDiscoveryError(error_code, f"Basins directory does not exist: {path}", path=str(path))
    except OSError as error:
        # One structured boundary for the kind/stat probes (#1554): the old
        # ``is_dir()`` pre-check swallowed a different OSError set per CPython
        # (EACCES on 3.11 raised, on 3.12+ returned False and was then
        # misreported as missing).  Permission denial stays on the caller's
        # unreadable code, never leaks as a bare PermissionError and never
        # becomes a NOT_FOUND mislabel.
        raise BasinsDiscoveryError(error_code, f"Basins directory cannot be stat'ed: {path}", path=str(path)) from error
    if not stat.S_ISDIR(mode):
        raise BasinsDiscoveryError(error_code, f"Basins directory does not exist: {path}", path=str(path))
    if not mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
        raise BasinsDiscoveryError(error_code, f"Basins directory is not readable: {path}", path=str(path))
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise BasinsDiscoveryError(error_code, f"Basins directory is not searchable: {path}", path=str(path))


def _classify_basins_root_metadata(root: Path, *, error_prefix: str = "BASINS_ROOT") -> bool:
    """Classify the explicit root's metadata once, by errno; return source-symlink identity.

    ``Path.exists()`` / ``is_dir()`` swallow different ``OSError`` sets on
    different CPython versions (EACCES on a denied ancestor raises on 3.11 but
    returns False from 3.12 on), so they cannot decide root classification.
    One no-follow ``lstat`` probe identifies a source symlink and one
    follow-target ``stat`` probe confirms the directory this classifier
    admits.  Missing (``ENOENT``/``ENOTDIR``) is the ``*_NOT_FOUND`` verdict,
    permission denial (``EACCES``/``EPERM``) and any other unreadable metadata
    failure is the ``*_UNREADABLE`` verdict, and no raw ``PermissionError``
    escapes.  Callers consume the returned symlink identity instead of
    re-probing the root with a bare ``Path.is_symlink()`` after this
    classifier has run (#1554).
    """

    try:
        lstat_mode = root.lstat().st_mode
    except FileNotFoundError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root does not exist: {root}",
            path=str(root),
        )
    except NotADirectoryError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root does not exist: {root}",
            path=str(root),
        )
    except PermissionError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_UNREADABLE",
            f"Basins root is not readable: {root}",
            path=str(root),
        )
    except OSError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_UNREADABLE",
            f"Basins root cannot be inspected: {root}",
            path=str(root),
        )
    is_symlink = stat.S_ISLNK(lstat_mode)
    try:
        mode = root.stat().st_mode
    except FileNotFoundError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root does not exist: {root}",
            path=str(root),
        )
    except NotADirectoryError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root does not exist: {root}",
            path=str(root),
        )
    except PermissionError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_UNREADABLE",
            f"Basins root is not readable: {root}",
            path=str(root),
        )
    except OSError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_UNREADABLE",
            f"Basins root cannot be inspected: {root}",
            path=str(root),
        )
    if not stat.S_ISDIR(mode):
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root is not a directory: {root}",
            path=str(root),
        )
    return is_symlink


def _classify_entry_kind(path: Path) -> tuple[_FileKind, int | None]:
    """Errno-aware single follow-stat verdict shared by file and directory owners.

    ``Path.is_file()`` / ``is_dir()`` each swallow a different OSError set per
    CPython (EACCES on the final follow-stat raises on 3.11-3.13 but returns
    False on 3.14), so neither predicate can decide final metadata.  One
    errno-aware stat yields a verdict every interpreter agrees on and returns
    the raw mode for the caller's type-bit check: missing / not-a-directory ->
    MISSING, permission denial -> UNREADABLE, any other metadata failure ->
    OTHER, otherwise REGULAR with the mode.
    """

    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return _FileKind.MISSING, None
    except NotADirectoryError:
        return _FileKind.MISSING, None
    except PermissionError:
        return _FileKind.UNREADABLE, None
    except OSError:
        return _FileKind.OTHER, None
    return _FileKind.REGULAR, mode


def _classify_regular_file(path: Path) -> _FileKind:
    """Errno-aware final-file metadata verdict.

    ``Path.is_file()`` swallows a different OSError set per CPython: EACCES on
    the final follow-stat raises on 3.11-3.13 but returns False on 3.14
    (cand-r1-06).  This classifier yields one verdict on every interpreter:
    missing/not-a-dir -> MISSING, permission denial -> UNREADABLE, other
    metadata failure -> OTHER, regular file -> REGULAR.
    """

    kind, mode = _classify_entry_kind(path)
    if kind is _FileKind.REGULAR:
        return _FileKind.REGULAR if mode is not None and stat.S_ISREG(mode) else _FileKind.OTHER
    return kind


def _classify_directory_kind(path: Path) -> _FileKind:
    """Errno-aware final-directory metadata verdict for required-input ownership.

    Mirrors ``_classify_regular_file`` for directories: the final follow-stat
    EACCES/EPERM is UNREADABLE on every interpreter, missing/not-a-dir is
    MISSING, a regular file at the leaf is OTHER, and an actual directory is
    REGULAR.
    """

    kind, mode = _classify_entry_kind(path)
    if kind is _FileKind.REGULAR:
        return _FileKind.REGULAR if mode is not None and stat.S_ISDIR(mode) else _FileKind.OTHER
    return kind


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


def _relative_depth(root: Path, path: Path) -> int:
    try:
        return len(path.relative_to(root).parts)
    except ValueError:
        return 0
