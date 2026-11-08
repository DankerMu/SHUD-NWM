from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import FileIO
from pathlib import Path
from typing import Any

# #3081: geometry/shape code and leaf types live in sibling modules; the names
# importers and tests use from this module are re-exported here (``X as X``).
from .basins_geometry_shapes import _coordinate_transform as _coordinate_transform
from .basins_geometry_shapes import _domain_multipolygon_wkt as _domain_multipolygon_wkt
from .basins_geometry_shapes import _optional_int as _optional_int
from .basins_geometry_shapes import _point_wkt as _point_wkt
from .basins_geometry_shapes import _river_segments_from_layer as _river_segments_from_layer
from .basins_geometry_shapes import _shape_parts as _shape_parts
from .basins_geometry_shapes import _shape_reader as _shape_reader
from .basins_geometry_shapes import parse_seg_shp_crosswalk as parse_seg_shp_crosswalk
from .basins_geometry_types import BasinsGeometryError as BasinsGeometryError
from .basins_geometry_types import CrosswalkRow as CrosswalkRow
from .basins_geometry_types import RiverSegmentGeometry as RiverSegmentGeometry
from .basins_geometry_types import TrustedBasinsRoot as TrustedBasinsRoot
from .basins_geometry_types import _check_resource_limit as _check_resource_limit
from .basins_geometry_types import _CoordinateTransform as _CoordinateTransform
from .basins_geometry_types import _GisReadBudget as _GisReadBudget
from .basins_geometry_types import _jsonable as _jsonable
from .basins_geometry_types import _LayerSnapshot as _LayerSnapshot

SHAPEFILE_REQUIRED_SUFFIXES = ("shp", "shx", "dbf", "prj")
# PR 2 (feat-reach-geom-from-river-shp / spec D1): gis/river.shp must carry
# the full SHUD reach attribute table verbatim. Index_1 is a known artefact
# of rSHUD pre-processing (duplicate Index column with `_1` suffix); the
# qhh-sample fixture README documents this and the invariant excludes it.
RIVER_SHP_REQUIRED_DBF_FIELDS: tuple[str, ...] = (
    "Index",
    "Down",
    "Type",
    "Slope",
    "Length",
    "BC",
    "Depth",
    "BankSlope",
    "Width",
    "Sinuosity",
    "Manning",
    "Cwr",
    "KsatH",
    "BedThick",
)
SHUD_CANONICAL_SUFFIXES = {
    "cfg_para": ".cfg.para",
    "cfg_ic": ".cfg.ic",
    "cfg_calib": ".cfg.calib",
    "sp_mesh": ".sp.mesh",
    "sp_riv": ".sp.riv",
    "sp_rivseg": ".sp.rivseg",
    "sp_att": ".sp.att",
    "para_soil": ".para.soil",
    "para_geol": ".para.geol",
    "para_lc": ".para.lc",
    "tsd_forc": ".tsd.forc",
    "tsd_lai": ".tsd.lai",
    "tsd_mf": ".tsd.mf",
    "tsd_rl": ".tsd.rl",
}
MAX_BASINS_GIS_FEATURES = 250_000
MAX_BASINS_GIS_POINTS = 5_000_000
MAX_BASINS_GIS_SIDECAR_BYTES = 64 * 1024 * 1024
MAX_BASINS_GIS_LAYER_BYTES = 128 * 1024 * 1024
MAX_BASINS_GIS_TOTAL_BYTES = 384 * 1024 * 1024
# SHUD segment evidence files should be tiny count/header or row-count inputs.
# These guards keep stale or hostile local files from turning import into an
# unbounded scan before registry writes begin.
MAX_BASINS_SHUD_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_BASINS_SHUD_EVIDENCE_LINES = 250_000
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_SAFE_OPEN_TEST_HOOK: Any = None


@dataclass(frozen=True)
class ParsedBasinsGeometry:
    domain_wkt: str
    domain_checksum: str
    domain_source_uri: str
    river_segments: list[RiverSegmentGeometry]
    river_network_checksum: str
    river_network_source_uri: str
    # #1693: post-PR-2 (#561) ``segment_count`` is the ``gis/river.shp`` reach
    # record count — NOT the finer ``seg.shp``/``.sp.rivseg`` display geometry,
    # which the pre-PR-2 comment here used to claim. It is written to
    # ``core.river_network_version.segment_count`` and counts reach rows only.
    segment_count: int
    # ``.sp.riv`` reach count. Equal to ``segment_count`` for a valid package:
    # ``_validate_river_shp_single_part_invariant`` fails the import with
    # ``BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED`` unless the ``river.shp``
    # record count matches this one. It is not a ``core.river_network_version``
    # column: it lands in the import receipt and
    # ``core.model_instance.resource_profile``, and is re-read from there into
    # the scheduler file-registry / candidate / chain manifests (carrier list in
    # ``openspec/glossary.md``, entry ``output_segment_count``).
    output_segment_count: int
    evidence_counts: dict[str, int | None]


def trusted_basins_root(path: Path, *, role: str) -> TrustedBasinsRoot:
    expanded = Path(path).expanduser()
    root = expanded if expanded.is_absolute() else Path(os.path.abspath(expanded))
    try:
        st = root.lstat()
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root cannot be safely inspected.",
            path=str(root),
            details={"role": role},
        ) from error
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root is not a regular no-symlink directory.",
            path=str(root),
            details={"role": role},
        )
    return TrustedBasinsRoot(
        path=root,
        resolved_path=root,
        identity=(st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode)),
    )


def parse_basins_geometry(
    *,
    model_id: str,
    input_dir: Path | TrustedBasinsRoot,
    shud_input_name: str,
    required_files: dict[str, Any],
    expected_checksums: dict[str, str] | None = None,
) -> ParsedBasinsGeometry:
    """Parse domain and river geometry from inventory-referenced Basins files."""

    input_root = _coerce_trusted_root(input_dir, role="shud_input_name")
    _run_safe_open_test_hook(input_root.path, "shud_input_name", "before_parse")
    _validate_required_files_canonical(required_files, shud_input_name, input_root.path)
    # PR 2 (spec "Required input files are validated for presence"):
    # fail fast with payload-precise codes
    # (BASINS_REGISTRY_RIVER_SHP_MISSING / BASINS_REGISTRY_SEG_SHP_MISSING)
    # before any layer is opened. The check no-ops when the whole gis/
    # directory is absent so the downstream canonical-sidecar guard
    # (BASINS_REGISTRY_GIS_SIDECAR_MISSING) still owns that structural
    # failure case. Domain sidecar absence remains the responsibility of
    # `_validated_layer_base`.
    _validate_required_files_present(input_root)
    domain_base = _validated_layer_base(input_root, "domain", required_files)
    river_base = _validated_layer_base(input_root, "river", required_files)
    seg_base = _validated_layer_base(input_root, "seg", required_files)
    _enforce_gis_sidecar_byte_limits((domain_base, river_base, seg_base), input_root)
    _run_safe_open_test_hook(input_root.path, "gis_sidecar_limits", "after_precheck")
    gis_budget = _GisReadBudget()
    domain_layer = _load_layer_snapshot(domain_base, input_root, expected_checksums, gis_budget)
    river_layer = _load_layer_snapshot(river_base, input_root, expected_checksums, gis_budget)
    seg_layer = _load_layer_snapshot(seg_base, input_root, expected_checksums, gis_budget)
    domain_transform = _coordinate_transform(domain_layer)
    river_transform = _coordinate_transform(river_layer)
    # PR 2: seg.shp is no longer a geometry source; we still validate its
    # CRS via _coordinate_transform up-front so an unsupported projection
    # surfaces before any DB write (the crosswalk path reads it later).
    _coordinate_transform(seg_layer)

    sp_riv = _required_input_file(input_root, required_files, "sp_riv", shud_input_name)
    sp_rivseg = _required_input_file(input_root, required_files, "sp_rivseg", shud_input_name)
    sp_riv_header, sp_riv_digest = _shud_count_header(sp_riv, expected_checksums)
    sp_rivseg_header, sp_rivseg_digest = _shud_count_header(sp_rivseg, expected_checksums)
    domain_wkt = _domain_multipolygon_wkt(
        domain_layer,
        domain_transform,
        max_features=MAX_BASINS_GIS_FEATURES,
        max_points=MAX_BASINS_GIS_POINTS,
    )

    # PR 2 (feat-reach-geom-from-river-shp): geometry source is always
    # gis/river.shp -- one record per .sp.riv reach. The seg.shp fallback
    # is removed; seg.shp survives only as the crosswalk source.
    sp_riv_count = int(sp_riv_header["count"] or 0)
    _validate_river_shp_single_part_invariant(
        river_layer,
        sp_riv_count=sp_riv_count,
        required_fields=RIVER_SHP_REQUIRED_DBF_FIELDS,
    )
    river_segments = _river_segments_from_layer(
        river_layer,
        model_id=model_id,
        coordinate_transform=river_transform,
        max_features=MAX_BASINS_GIS_FEATURES,
        max_points=MAX_BASINS_GIS_POINTS,
    )
    selected_layer = river_layer
    if not river_segments:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_GIS_PARSE_FAILED",
            "Basins river shapefile did not contain any LineString features.",
            path=str(river_base.with_suffix(".shp")),
        )

    evidence_counts = {
        "river_count": sp_riv_header["count"],
        "river_columns": sp_riv_header["columns"],
        "rivseg_segment_count": sp_rivseg_header["count"],
        "rivseg_columns": sp_rivseg_header["columns"],
    }
    # Reach count now drives row granularity: core.river_segment row count
    # equals the .sp.riv reach count, not the .sp.rivseg segment count.
    if sp_riv_count != len(river_segments):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_REACH_COUNT_MISMATCH",
            "Basins GIS reach count does not match SHUD .sp.riv evidence.",
            path=str(sp_riv),
            details={
                "model_id": model_id,
                "gis_reach_count": len(river_segments),
                "evidence_count": sp_riv_count,
                "evidence": "sp_riv_reach_count",
            },
        )

    return ParsedBasinsGeometry(
        domain_wkt=domain_wkt,
        domain_checksum=_layer_checksum_from_snapshot(domain_layer),
        domain_source_uri=str(domain_base.with_suffix(".shp")),
        river_segments=river_segments,
        river_network_checksum=_river_network_checksum_from_snapshot(
            selected_layer,
            sp_riv_digest=sp_riv_digest,
            sp_rivseg_digest=sp_rivseg_digest,
        ),
        river_network_source_uri=str(selected_layer.base.with_suffix(".shp")),
        segment_count=len(river_segments),
        output_segment_count=sp_riv_count,
        evidence_counts=evidence_counts,
    )


def _coerce_trusted_root(root: Path | TrustedBasinsRoot, *, role: str) -> TrustedBasinsRoot:
    if isinstance(root, TrustedBasinsRoot):
        _validate_trusted_root(root, role=role)
        return root
    return trusted_basins_root(root, role=role)


def _validated_layer_base(input_dir: TrustedBasinsRoot, layer: str, required_files: dict[str, Any]) -> Path:
    for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
        role = f"gis_{layer}_{suffix}"
        expected = f"gis/{layer}.{suffix}"
        values = required_files.get(role)
        if values != [expected]:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_GIS_SIDECAR_MISSING",
                f"Basins inventory is missing required GIS sidecar role {role}.",
                path=str(input_dir.path / expected),
                details={"missing_sidecar": expected, "role": role},
            )
        path = input_dir.path / expected
        try:
            _validate_safe_file(path, input_dir, role=role, error_code="BASINS_REGISTRY_GIS_SIDECAR_MISSING")
        except BasinsGeometryError as error:
            if error.error_code == "BASINS_REGISTRY_GIS_SIDECAR_MISSING":
                error.details.setdefault("missing_sidecar", expected)
            raise
        if not path.is_file():
            raise BasinsGeometryError(
                "BASINS_REGISTRY_GIS_SIDECAR_MISSING",
                f"Basins GIS sidecar is missing: {expected}",
                path=str(path),
                details={"missing_sidecar": expected, "role": role},
            )
    return input_dir.path / "gis" / layer


def _enforce_gis_sidecar_byte_limits(layer_bases: tuple[Path, ...], input_root: TrustedBasinsRoot) -> None:
    total_bytes = 0
    for layer_base in layer_bases:
        layer_bytes = 0
        for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
            role = f"gis_{layer_base.name}_{suffix}"
            path = layer_base.with_suffix(f".{suffix}")
            size_bytes = _verified_file_size(path, input_root, role=role)
            _check_resource_limit(
                size_bytes,
                MAX_BASINS_GIS_SIDECAR_BYTES,
                "gis_sidecar_bytes",
                str(path),
            )
            layer_bytes += size_bytes
            total_bytes += size_bytes
            _check_resource_limit(
                layer_bytes,
                MAX_BASINS_GIS_LAYER_BYTES,
                "gis_layer_bytes",
                str(path),
            )
            _check_resource_limit(
                total_bytes,
                MAX_BASINS_GIS_TOTAL_BYTES,
                "gis_total_bytes",
                str(path),
            )


def _required_input_file(
    input_dir: TrustedBasinsRoot,
    required_files: dict[str, Any],
    role: str,
    shud_input_name: str,
) -> Path:
    values = required_files.get(role)
    expected = f"{shud_input_name}{SHUD_CANONICAL_SUFFIXES[role]}"
    if not isinstance(values, list) or values != [expected]:
        raise BasinsGeometryError(
            "BASINS_REQUIRED_FILES_NON_CANONICAL",
            f"Basins inventory source role {role} must be canonical: {expected}.",
            path=str(input_dir.path),
            details={"role": role},
        )
    path = input_dir.path / expected
    _validate_safe_file(path, input_dir, role=role, error_code="BASINS_REGISTRY_SOURCE_MISSING")
    if not path.is_file():
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            f"Basins source file is missing for role {role}.",
            path=str(path),
            details={"role": role},
        )
    return path


def _load_layer_snapshot(
    layer_base: Path,
    input_root: TrustedBasinsRoot,
    expected_checksums: dict[str, str] | None,
    gis_budget: _GisReadBudget,
) -> _LayerSnapshot:
    data: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    layer_bytes = 0
    for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
        role = f"gis_{layer_base.name}_{suffix}"
        path = layer_base.with_suffix(f".{suffix}")
        payload, digest = _read_verified_binary(
            path,
            input_root,
            role=role,
            expected_checksums=expected_checksums,
            error_code="BASINS_REGISTRY_GIS_SIDECAR_MISSING",
            max_bytes=MAX_BASINS_GIS_SIDECAR_BYTES,
            max_bytes_resource="gis_sidecar_bytes",
        )
        layer_bytes += len(payload)
        gis_budget.total_bytes += len(payload)
        _check_resource_limit(layer_bytes, MAX_BASINS_GIS_LAYER_BYTES, "gis_layer_bytes", str(path))
        _check_resource_limit(gis_budget.total_bytes, MAX_BASINS_GIS_TOTAL_BYTES, "gis_total_bytes", str(path))
        data[suffix] = payload
        digests[suffix] = digest
    return _LayerSnapshot(base=layer_base, data=data, digests=digests)


def _validate_required_files_present(input_dir: Path | TrustedBasinsRoot) -> None:
    """Verify river.shp + seg.shp (with sidecars) exist before any registry write.

    PR 2 (spec "Required input files are validated for presence"). Missing
    files surface as ``BASINS_REGISTRY_RIVER_SHP_MISSING`` or
    ``BASINS_REGISTRY_SEG_SHP_MISSING`` with the basin path payload, before
    the geometry parser opens any layer. Sidecars (.dbf/.shx) are checked
    alongside the .shp so a half-installed package fails fast.

    If the ``gis/`` directory itself is absent, this check defers to the
    downstream canonical-sidecar guard (``BASINS_REGISTRY_GIS_SIDECAR_MISSING``
    with ``missing_sidecar="gis/domain.shp"``) so callers that destroy the
    whole GIS tree still get the structural-failure code rather than a
    layer-specific one.

    The caller is expected to have already validated the trusted root; we
    only need the path here, so we avoid re-running ``_validate_trusted_root``
    (and the role-tagged unsafe-path error it would emit).
    """

    if isinstance(input_dir, TrustedBasinsRoot):
        root = input_dir.path
    else:
        root = _coerce_trusted_root(input_dir, role="required_files_present").path
    if not (root / "gis").is_dir():
        return
    for layer_name, error_code in (
        ("river", "BASINS_REGISTRY_RIVER_SHP_MISSING"),
        ("seg", "BASINS_REGISTRY_SEG_SHP_MISSING"),
    ):
        for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
            candidate = root / "gis" / f"{layer_name}.{suffix}"
            if not candidate.is_file():
                raise BasinsGeometryError(
                    error_code,
                    f"Basins {layer_name}.shp package file is missing: {candidate.name}",
                    path=str(candidate),
                    details={
                        "layer": layer_name,
                        "missing_sidecar": f"gis/{layer_name}.{suffix}",
                    },
                )


def _validate_river_shp_single_part_invariant(
    layer: _LayerSnapshot,
    *,
    sp_riv_count: int,
    required_fields: tuple[str, ...],
) -> None:
    """Fail-fast invariant for the new river.shp geometry contract.

    PR 2 (spec "river.shp single-part invariant is enforced"). The qhh
    sample has 0 multi-part records by construction, and the import path
    refuses to silently fall back to seg.shp if a future basin breaks the
    contract. We check four things on the whole layer in one pass:

    - every record is a single ``LINESTRING`` part with ≥ 2 vertices;
    - record count equals the SHUD ``.sp.riv`` reach count (one row per reach);
    - the dbf carries every required SHUD reach attribute
      (``Index/Down/Type/Slope/Length/BC/Depth/BankSlope/Width/Sinuosity/Manning/Cwr/KsatH/BedThick``).

    Raises ``BasinsGeometryError`` with code
    ``BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED`` so the caller can
    convert it to a structured per-basin failure without aborting other
    basins in the queue.
    """

    reader = _shape_reader(layer)
    try:
        # Use raw field names from the reader so the missing-field check
        # surfaces the exact dbf header the source GIS exposes. Field 0 is
        # the pyshp deletion-flag pseudo-field; skip it.
        actual_fields = {str(field[0]) for field in reader.fields[1:]}
        missing_fields = [name for name in required_fields if name not in actual_fields]
        if missing_fields:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED",
                "Basins river.shp is missing one or more required dbf fields.",
                path=str(layer.base.with_suffix(".shp")),
                details={
                    "missing_fields": missing_fields,
                    "actual_fields": sorted(actual_fields),
                },
            )
        record_count = 0
        for record_index, shape_record in enumerate(reader.iterShapeRecords(), start=1):
            record_count += 1
            shape = shape_record.shape
            parts = _shape_parts(shape)
            valid_parts = [points for points in parts if len(points) >= 2]
            if len(parts) != 1 or len(valid_parts) != 1:
                raise BasinsGeometryError(
                    "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED",
                    "Basins river.shp record is not a single-part LineString.",
                    path=str(layer.base.with_suffix(".shp")),
                    details={
                        "offending_index": record_index,
                        "part_count": len(parts),
                    },
                )
    finally:
        reader.close()
    if record_count != sp_riv_count:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED",
            "Basins river.shp record count does not match .sp.riv reach count.",
            path=str(layer.base.with_suffix(".shp")),
            details={
                "river_shp_record_count": record_count,
                "sp_riv_count": sp_riv_count,
            },
        )


def _shud_count_header(path: Path, expected_checksums: dict[str, str] | None) -> tuple[dict[str, int | None], str]:
    input_root = path.parent
    byte_count = 0
    payload, digest = _read_verified_binary(
        path,
        input_root,
        role="shud_evidence",
        expected_checksums=expected_checksums,
        error_code="BASINS_REGISTRY_SOURCE_MISSING",
        max_bytes=MAX_BASINS_SHUD_EVIDENCE_BYTES,
    )
    for line_count, line in enumerate(payload.decode("utf-8", errors="ignore").splitlines(), start=1):
        _check_resource_limit(
            line_count,
            MAX_BASINS_SHUD_EVIDENCE_LINES,
            "shud_evidence_lines",
            str(path),
        )
        byte_count += len(line.encode("utf-8")) + 1
        _check_resource_limit(
            byte_count,
            MAX_BASINS_SHUD_EVIDENCE_BYTES,
            "shud_evidence_bytes",
            str(path),
        )
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "%")):
            continue
        tokens = re.split(r"[\s,]+", stripped)
        if not tokens:
            continue
        if any(re.search(r"[A-Za-z]", token) for token in tokens):
            continue
        declared = _optional_int(tokens[0])
        columns = _optional_int(tokens[1]) if len(tokens) > 1 else None
        if declared is not None and declared >= 0:
            return {"count": declared, "columns": columns}, digest
        break
    raise BasinsGeometryError(
        "BASINS_REGISTRY_SHUD_PARSE_FAILED",
        "SHUD river evidence did not contain a valid numeric count header.",
        path=str(path),
    )


def _shud_segment_count(path: Path, expected_checksums: dict[str, str] | None) -> tuple[int, str]:
    header, digest = _shud_count_header(path, expected_checksums)
    count = header["count"]
    if count is None:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SHUD_PARSE_FAILED",
            "SHUD river evidence did not contain a valid segment count.",
            path=str(path),
        )
    return count, digest


def _layer_checksum_from_snapshot(layer: _LayerSnapshot) -> str:
    digest = hashlib.sha256()
    for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
        digest.update(suffix.encode("utf-8"))
        digest.update(layer.digests[suffix].encode("ascii"))
    return digest.hexdigest()


def _river_network_checksum_from_snapshot(
    layer: _LayerSnapshot,
    *,
    sp_riv_digest: str,
    sp_rivseg_digest: str,
) -> str:
    material = {
        "gis": _layer_checksum_from_snapshot(layer),
        "sp_riv": sp_riv_digest,
        "sp_rivseg": sp_rivseg_digest,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def _sha256_file(path: Path, containment_root: Path | TrustedBasinsRoot) -> str:
    _validate_safe_file(path, containment_root, role="checksum", error_code="BASINS_REGISTRY_SOURCE_MISSING")
    digest = hashlib.sha256()
    with _open_safe_binary(path, containment_root, role="checksum") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_basins_file_sha256(path: Path, containment_root: Path | TrustedBasinsRoot) -> str:
    _validate_safe_file(path, containment_root, role="checksum", error_code="BASINS_REGISTRY_SOURCE_MISSING")
    return _sha256_file(path, containment_root)


def _read_verified_binary(
    path: Path,
    containment_root: Path | TrustedBasinsRoot,
    *,
    role: str,
    expected_checksums: dict[str, str] | None,
    error_code: str,
    max_bytes: int | None = None,
    max_bytes_resource: str = "shud_evidence_bytes",
) -> tuple[bytes, str]:
    _validate_safe_file(path, containment_root, role=role, error_code=error_code)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    byte_count = 0
    with _open_safe_binary(path, containment_root, role=role) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_count += len(chunk)
            if max_bytes is not None:
                _check_resource_limit(byte_count, max_bytes, max_bytes_resource, str(path))
            digest.update(chunk)
            chunks.append(chunk)
    actual = digest.hexdigest()
    _run_safe_open_test_hook(path, role, "after_read")
    relative_path = _relative_to_root(path, containment_root)
    expected = expected_checksums.get(relative_path) if expected_checksums is not None else None
    if expected_checksums is not None and not expected:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins source file is missing an expected manifest checksum.",
            path=str(path),
            details={"role": role, "relative_path": relative_path},
        )
    if expected is not None and actual != expected:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            "Basins source bytes do not match the manifest-verified checksum.",
            path=str(path),
            details={"role": role, "relative_path": relative_path},
        )
    return b"".join(chunks), actual


def _relative_to_root(path: Path, containment_root: Path | TrustedBasinsRoot) -> str:
    root = _coerce_trusted_root(containment_root, role="relative").resolved_path
    candidate = path if path.is_absolute() else root / path
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _file_identity(path: Path, containment_root: Path | TrustedBasinsRoot, *, role: str) -> tuple[int, int, int]:
    _validate_safe_file(path, containment_root, role=role, error_code="BASINS_REGISTRY_SOURCE_MISSING")
    try:
        st = path.lstat()
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path cannot be safely inspected.",
            path=str(path),
            details={"role": role},
        ) from error
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path is not a regular no-symlink file.",
            path=str(path),
            details={"role": role},
        )
    return (st.st_dev, st.st_ino, st.st_mode)


def _verified_file_size(path: Path, containment_root: Path | TrustedBasinsRoot, *, role: str) -> int:
    _validate_safe_file(path, containment_root, role=role, error_code="BASINS_REGISTRY_SOURCE_MISSING")
    try:
        st = path.lstat()
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path cannot be safely inspected.",
            path=str(path),
            details={"role": role},
        ) from error
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path is not a regular no-symlink file.",
            path=str(path),
            details={"role": role},
        )
    return int(st.st_size)


@contextmanager
def _open_safe_binary(path: Path, containment_root: Path | TrustedBasinsRoot, *, role: str) -> Iterator[FileIO]:
    handle = _open_safe_binary_handle(path, containment_root, role=role)
    try:
        yield handle
    finally:
        handle.close()


def _open_safe_binary_handle(
    path: Path,
    containment_root: Path | TrustedBasinsRoot,
    *,
    role: str,
    expected_identity: tuple[int, int, int] | None = None,
) -> FileIO:
    expected = expected_identity or _file_identity(path, containment_root, role=role)
    _run_safe_open_test_hook(path, role, "before_open")
    flags = os.O_RDONLY | os.O_CLOEXEC | _OPEN_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source file cannot be safely opened.",
            path=str(path),
            details={"role": role},
        ) from error
    try:
        st = os.fstat(fd)
        actual = (st.st_dev, st.st_ino, st.st_mode)
        if actual != expected or not stat.S_ISREG(st.st_mode):
            raise BasinsGeometryError(
                "BASINS_REGISTRY_PATH_UNSAFE",
                "Basins source path changed during safe open.",
                path=str(path),
                details={"role": role},
            )
        _run_safe_open_test_hook(path, role, "after_open")
        if _file_identity(path, containment_root, role=role) != expected:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_PATH_UNSAFE",
                "Basins source path changed after safe open.",
                path=str(path),
                details={"role": role},
            )
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _validate_trusted_root(root: TrustedBasinsRoot, *, role: str) -> None:
    if root.path != root.resolved_path:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root path is not canonical.",
            path=str(root.path),
            details={"role": role},
        )
    try:
        st = root.path.lstat()
    except OSError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root cannot be safely inspected.",
            path=str(root.path),
            details={"role": role},
        ) from error
    actual = (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))
    if actual != root.identity or not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins trusted root changed during import.",
            path=str(root.path),
            details={"role": role},
        )


def _run_safe_open_test_hook(path: Path, role: str, phase: str) -> None:
    hook = _SAFE_OPEN_TEST_HOOK
    if hook is not None:
        hook(path, role, phase)


def _validate_required_files_canonical(required_files: dict[str, Any], shud_input_name: str, input_dir: Path) -> None:
    expected: dict[str, str] = {
        role: f"{shud_input_name}{suffix}" for role, suffix in SHUD_CANONICAL_SUFFIXES.items()
    }
    expected.update(
        {
            f"gis_{layer}_{suffix}": f"gis/{layer}.{suffix}"
            for layer in ("domain", "river", "seg")
            for suffix in SHAPEFILE_REQUIRED_SUFFIXES
        }
    )
    for role, expected_path in expected.items():
        values = required_files.get(role)
        if values != [expected_path]:
            raise BasinsGeometryError(
                "BASINS_REQUIRED_FILES_NON_CANONICAL",
                f"Basins inventory source role {role} must be canonical: {expected_path}.",
                path=str(input_dir),
                details={"role": role, "expected_path": expected_path},
            )
    extras = sorted(str(role) for role in required_files if str(role) not in expected)
    if extras:
        raise BasinsGeometryError(
            "BASINS_REQUIRED_FILES_NON_CANONICAL",
            "Basins inventory contains non-canonical required file roles.",
            path=str(input_dir),
            details={"roles": extras},
        )


def _validate_safe_file(path: Path, containment_root: Path | TrustedBasinsRoot, *, role: str, error_code: str) -> None:
    trusted_root = _coerce_trusted_root(containment_root, role=role)
    root = trusted_root.resolved_path
    _validate_trusted_root(trusted_root, role=role)
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path is outside the canonical input directory.",
            path=str(candidate),
            details={"role": role},
        ) from error
    if ".." in relative.parts:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path contains parent traversal.",
            path=str(candidate),
            details={"role": role},
        )
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            details: dict[str, Any] = {"role": role}
            if error_code == "BASINS_REGISTRY_GIS_SIDECAR_MISSING":
                details["missing_sidecar"] = relative.as_posix()
            raise BasinsGeometryError(
                error_code,
                "Basins source path component is missing.",
                path=str(candidate),
                details=details,
            ) from None
        except OSError as error:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_PATH_UNSAFE",
                "Basins source path cannot be safely inspected.",
                path=str(current),
                details={"role": role},
            ) from error
        if stat.S_ISLNK(mode):
            raise BasinsGeometryError(
                "BASINS_REGISTRY_PATH_UNSAFE",
                "Basins source path contains a symlink descendant.",
                path=str(current),
                details={"role": role},
            )
    if not candidate.is_file():
        raise BasinsGeometryError(
            error_code,
            "Basins source path is not a regular file.",
            path=str(candidate),
            details={"role": role},
        )
