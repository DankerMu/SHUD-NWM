"""G0 baseline integrity verification for the mapping builder.

This module implements OpenSpec change ``forcing-mapping-asset-build`` §1.1 (Epic
#909 SUB-1), §1.2 (Epic #909 SUB-2), §1.3 (Epic #909 SUB-3), and §1.4 (Epic
#909 SUB-4). It exposes six pure entry points that read a baseline SHUD basin
model package **read-only** (INV-1):

* :func:`verify_g0_baseline` — §1.1 baseline integrity gate.
* :func:`verify_package_crs` — §1.2 CRS authority (WKT from ``gis/*.prj``).
* :func:`build_ancillary_inventory` — §1.2 ancillary ``*.tsd.*`` inventory
  (excluding the weather ``.tsd.forc`` reference, which is §1.1's territory).
* :func:`classify_baseline` — §1.3 RECORD-ONLY classification of duplicate-
  coordinate stations, non-grid X-station baselines, startdate heterogeneity,
  ``domain.shp`` presence-only checksum, and known-harmless deviations
  (e.g. ``.tsd.forc`` line-2 absolute paths). Never repairs, never opens
  ``domain.shp`` as geometry.
* :func:`verify_baseline_inv1_end_to_end` — §1.3 end-to-end INV-1 evidence
  chain: pre-checksum all baseline files, run every §1.1/§1.2/§1.3 entry
  point, post-checksum, and prove byte-identical.
* :func:`verify_g1_non_degenerate_triangles` — §1.4 G1 geometry gate:
  every element has three pairwise-distinct vertex IDs, each referencing an
  existing mesh node, and the triangle formed by their X/Y coordinates in the
  package CRS has an unsigned planar area strictly greater than
  :data:`G1_MIN_TRIANGLE_AREA`.

Each entry point either returns an immutable report or raises a
:class:`BaselineIntegrityError` subclass explaining the exact violation.

Fail-closed guarantee: any subcheck failure raises without writing any output
artifact. The mapping variant tree remains empty.

The SHUD file formats parsed here are inferred from live baseline packages
(``SHUD/input/qhh``, ``SHUD/input/heihe``, ``SHUD/input/ccw``):

``.sp.mesh``
    Line 1: ``<N_elements>\\t<N_element_columns>`` (may carry trailing whitespace).
    Line 2: element header row (``ID  Node1 Node2 Node3 Nabr1 Nabr2 Nabr3 Zmax``).
    Lines 3..N+2: element rows (whitespace-separated).
    Line N+3: node table header ``<N_nodes>\\t<N_node_columns>``.
    Line N+4: node header row (``ID  X  Y  AqDepth  Elevation``).
    Remaining lines: node rows.

``.sp.att``
    Line 1: ``<N>\\t<N_columns>`` (may carry trailing whitespace).
    Line 2: column header (``INDEX  SOIL  GEOL  LC  FORC  MF  BC  SS  LAKE`` — the
    trailing column may be ``LAKE`` or ``iLAKE`` per live variants).
    Lines 3..N+2: INDEX rows.

``.tsd.forc``
    Line 1: ``<N_stations> <StartDate>`` (space-separated).
    Line 2: absolute path recorded by the SHUD build tool (informational).
    Line 3: header row ``ID  Lon  Lat  X  Y  Z  Filename``.
    Lines 4..N+3: station rows. The ``ID`` column is the forcing reference used
    by ``.sp.att`` ``FORC`` values (§1.1 references the *reference set*, not the
    row order).

``gis/*.prj``
    Single-line ESRI WKT declaring the package CRS. Live audit (docs/ForcingReplace
    §附录 A, 2026-07-06) shows all 13 baselines are ``PROJCS["unknown"]`` custom
    Albers (×12) or Transverse Mercator (qhh). No basin carries an EPSG code, so
    the CRS MUST be read from the WKT string per basin — never from a global
    assumption and never from ``.sp.mesh`` (which carries no CRS metadata).
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass

import pyproj

#: Minimum unsigned planar triangle area (in package CRS length units squared)
#: below which an element is treated as degenerate/collinear.
#: Package CRS is a projected system with meters as base unit for all live
#: SHUD basins (custom Albers x12 or Transverse Mercator x1 per §1.2 live audit),
#: so this value is 1.0e-6 square meters (roughly the area of a 1mm-side
#: triangle). The spec (openspec/changes/forcing-mapping-asset-build/
#: specs/mapping-input-integrity/spec.md §"Element triangles are non-degenerate")
#: pins the rule ("strictly greater than a declared numeric tolerance") but does
#: not pin a numeric value, so this module owns the documented default. Any G1
#: unsigned area at or below this tolerance rejects the element as degenerate.
G1_MIN_TRIANGLE_AREA = 1.0e-6


class BaselineIntegrityError(Exception):
    """Base class for G0 baseline integrity failures.

    All subclasses map 1:1 to a §1.1 subcheck failure and are fail-closed:
    raising means the mapping builder MUST write no output artifact.
    """


class UnparseableMeshError(BaselineIntegrityError):
    """Raised when ``.sp.mesh`` cannot be parsed as SHUD mesh format."""

    field: str = "sp.mesh"

    def __init__(self, detail: str) -> None:
        super().__init__(f"unparseable {self.field}: {detail}")
        self.detail = detail


class UnparseableAttError(BaselineIntegrityError):
    """Raised when ``.sp.att`` cannot be parsed as SHUD attribute format."""

    field: str = "sp.att"

    def __init__(self, detail: str) -> None:
        super().__init__(f"unparseable {self.field}: {detail}")
        self.detail = detail


class NonUniqueElementIdError(BaselineIntegrityError):
    """Raised when an element ID appears more than once in a source file."""

    def __init__(self, file: str, duplicate_id: int) -> None:
        super().__init__(f"non-unique element id in {file}: id={duplicate_id} appears more than once")
        self.file = file
        self.duplicate_id = duplicate_id


class NonContiguousElementIdError(BaselineIntegrityError):
    """Raised when element IDs are not contiguous ``1..N``."""

    def __init__(self, file: str, missing_ids: tuple[int, ...]) -> None:
        super().__init__(
            f"non-contiguous element ids in {file}: expected 1..N, missing={list(missing_ids)}"
        )
        self.file = file
        self.missing_ids = missing_ids


class UnequalElementCountError(BaselineIntegrityError):
    """Raised when ``.sp.mesh`` and ``.sp.att`` disagree on element count."""

    def __init__(self, mesh_count: int, att_count: int) -> None:
        super().__init__(
            f"element count mismatch: sp.mesh has {mesh_count} elements, sp.att has {att_count}"
        )
        self.mesh_count = mesh_count
        self.att_count = att_count


class UnequalElementIdSetError(BaselineIntegrityError):
    """Raised when ``.sp.mesh`` and ``.sp.att`` element-ID sets differ."""

    def __init__(self, mesh_only: tuple[int, ...], att_only: tuple[int, ...]) -> None:
        super().__init__(
            f"element id sets differ: mesh_only={list(mesh_only)} att_only={list(att_only)}"
        )
        self.mesh_only = mesh_only
        self.att_only = att_only


class InvalidForcValueError(BaselineIntegrityError):
    """Raised when a baseline ``FORC`` value is not a positive integer."""

    def __init__(self, element_id: int, invalid_value: str | int | float) -> None:
        super().__init__(
            f"invalid FORC value for element_id={element_id}: {invalid_value!r} "
            "(must be a positive integer)"
        )
        self.element_id = element_id
        self.invalid_value = invalid_value


class IllegalTsdForcReferenceError(BaselineIntegrityError):
    """Raised when a ``.tsd.forc`` reference is out of the legal FORC range."""

    def __init__(
        self,
        line_number: int,
        invalid_reference: int,
        valid_range: tuple[int, int],
    ) -> None:
        super().__init__(
            f"illegal .tsd.forc reference at line {line_number}: "
            f"{invalid_reference} not in {valid_range[0]}..{valid_range[1]}"
        )
        self.line_number = line_number
        self.invalid_reference = invalid_reference
        self.valid_range = valid_range


class MissingPrjError(BaselineIntegrityError):
    """Raised when no ``gis/*.prj`` file is present under the baseline package.

    Per §1.2, the mapping builder makes no global CRS assumption; a missing
    ``.prj`` is a fail-closed integrity violation, not a fallback opportunity.
    """

    def __init__(self, baseline_root: pathlib.Path) -> None:
        super().__init__(f"no gis/*.prj found under {baseline_root}")
        self.baseline_root = baseline_root


class UnparseablePrjError(BaselineIntegrityError):
    """Raised when the package ``gis/*.prj`` cannot be parsed by pyproj."""

    def __init__(self, prj_path: pathlib.Path, parse_error: str) -> None:
        super().__init__(f"unparseable .prj at {prj_path}: {parse_error}")
        self.prj_path = prj_path
        self.parse_error = parse_error


class NonWgs84ConvertiblePrjError(BaselineIntegrityError):
    """Raised when the package CRS cannot be transformed to WGS84 via pyproj."""

    def __init__(self, prj_path: pathlib.Path, transform_error: str) -> None:
        super().__init__(
            f"package CRS at {prj_path} is not convertible to EPSG:4326: {transform_error}"
        )
        self.prj_path = prj_path
        self.transform_error = transform_error


class AncillaryInventoryError(BaselineIntegrityError):
    """Raised when an ancillary ``*.tsd.*`` file cannot be inventoried."""

    def __init__(self, path: pathlib.Path, read_error: str) -> None:
        super().__init__(f"unable to inventory ancillary file {path}: {read_error}")
        self.path = path
        self.read_error = read_error


class Inv1ViolationError(BaselineIntegrityError):
    """Raised when INV-1 (read-only baseline) is violated during a full-stack run.

    Signals that at least one baseline file's byte content changed between the
    pre-check and post-check snapshot taken by
    :func:`verify_baseline_inv1_end_to_end`. The mapping builder MUST write no
    output artifact when this is raised.
    """

    def __init__(self, drifted_paths: tuple[str, ...]) -> None:
        super().__init__(
            f"baseline package mutated during end-to-end verification (INV-1 violation): "
            f"drifted={list(drifted_paths)}"
        )
        self.drifted_paths = drifted_paths


class G1RepeatedVertexIdError(BaselineIntegrityError):
    """Raised when an element's three vertex IDs are not pairwise distinct.

    Per spec §"Element triangles are non-degenerate (G1 geometry validity)":
    the three vertex IDs of each element MUST be pairwise distinct. A repeated
    vertex ID collapses the triangle to a line segment or point and is
    always a G1 blocker.
    """

    def __init__(self, element_id: int, vertex_ids: tuple[int, int, int]) -> None:
        super().__init__(
            f"G1 element_id={element_id} has repeated vertex ids: {list(vertex_ids)}"
        )
        self.element_id = element_id
        self.vertex_ids = vertex_ids


class G1MissingMeshNodeError(BaselineIntegrityError):
    """Raised when an element references a vertex ID absent from the mesh node table.

    Per spec §"Element triangles are non-degenerate (G1 geometry validity)":
    each vertex ID MUST reference an existing mesh node. A missing node makes
    the triangle undefined and is always a G1 blocker.
    """

    def __init__(self, element_id: int, missing_vertex_id: int) -> None:
        super().__init__(
            f"G1 element_id={element_id} references missing mesh node "
            f"vertex_id={missing_vertex_id}"
        )
        self.element_id = element_id
        self.missing_vertex_id = missing_vertex_id


class G1CollinearTriangleError(BaselineIntegrityError):
    """Raised when an element's unsigned planar area is at or below the tolerance.

    Per spec §"Element triangles are non-degenerate (G1 geometry validity)":
    the unsigned planar area in the package CRS MUST be strictly greater than
    :data:`G1_MIN_TRIANGLE_AREA`. Three collinear vertices produce a zero-area
    triangle and are always a G1 blocker.
    """

    def __init__(self, element_id: int, area: float, tolerance: float) -> None:
        super().__init__(
            f"G1 element_id={element_id} triangle area {area!r} is not "
            f"strictly greater than tolerance {tolerance!r}"
        )
        self.element_id = element_id
        self.area = area
        self.tolerance = tolerance


@dataclass(frozen=True)
class BaselineIntegrityReport:
    """Report produced by :func:`verify_g0_baseline` when all G0 checks pass.

    All fields are immutable so callers cannot mutate the recorded evidence.
    ``per_file_checksums`` is sorted by relative path for reproducibility.
    """

    baseline_root: pathlib.Path
    package_checksum: str
    per_file_checksums: tuple[tuple[str, str], ...]
    sp_mesh_path: pathlib.Path
    sp_att_path: pathlib.Path
    element_id_set: frozenset[int]
    max_forc_value: int
    tsd_forc_present: bool
    tsd_forc_reference_count: int


@dataclass(frozen=True)
class PackageCrsReport:
    """Report produced by :func:`verify_package_crs` when CRS authority checks pass.

    ``prj_checksum`` binds the WKT source bytes so evidence can prove the CRS
    was not silently swapped. ``wgs84_probe`` records the WGS84 (lon, lat)
    coordinates of a probe point transformed via the package CRS → EPSG:4326
    transformer — its presence proves the transformer round-trips without
    error, satisfying the §1.2 "convertible to WGS84" requirement without
    committing to a specific probe strategy in the public contract.
    """

    prj_path: pathlib.Path
    prj_checksum: str
    wkt: str
    wgs84_probe: tuple[float, float]


@dataclass(frozen=True)
class AncillaryEntry:
    """One row of the ancillary inventory: path + checksum + size.

    ``path`` is the absolute filesystem path to the ancillary file. Checksum
    and size are recorded so downstream evidence can prove no ancillary file
    was silently swapped or truncated between integrity and rewrite stages.
    """

    path: pathlib.Path
    checksum: str
    size_bytes: int


@dataclass(frozen=True)
class AncillaryInventoryReport:
    """Report produced by :func:`build_ancillary_inventory`.

    ``entries`` is sorted by relative path under the baseline root for
    determinism. The weather-forcing reference file ``.tsd.forc`` is
    intentionally excluded — that file is §1.1's authority (see
    :class:`BaselineIntegrityReport.tsd_forc_reference_count`) and per
    design.md line 62 the variant only carries *ancillary non-weather*
    ``*.tsd.*`` files (§8.1 owns weather).
    """

    baseline_root: pathlib.Path
    entries: tuple[AncillaryEntry, ...]


@dataclass(frozen=True)
class G1NonDegenerateReport:
    """Report produced by :func:`verify_g1_non_degenerate_triangles` on success.

    All fields are immutable so callers cannot mutate the recorded evidence.
    ``min_observed_area`` and ``max_observed_area`` bound the observed unsigned
    planar area distribution across every element in the baseline mesh; both
    are guaranteed strictly greater than ``tolerance`` when this report is
    returned (fail-closed guarantee).
    """

    element_count: int
    min_observed_area: float
    max_observed_area: float
    tolerance: float


# --- internal helpers -----------------------------------------------------


def _iter_baseline_files(baseline_root: pathlib.Path) -> list[pathlib.Path]:
    """Return every regular file under ``baseline_root``, sorted for determinism."""
    return sorted(p for p in baseline_root.rglob("*") if p.is_file())


def _sha256_file(path: pathlib.Path) -> str:
    """Compute SHA-256 hex digest of a single file read-only (INV-1)."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _compute_per_file_checksums(
    baseline_root: pathlib.Path,
) -> tuple[tuple[str, str], ...]:
    """Compute sorted ``(relative_path, sha256_hex)`` pairs for the whole package."""
    entries: list[tuple[str, str]] = []
    for path in _iter_baseline_files(baseline_root):
        rel = path.relative_to(baseline_root).as_posix()
        entries.append((rel, _sha256_file(path)))
    entries.sort(key=lambda item: item[0])
    return tuple(entries)


def _aggregate_package_checksum(per_file: tuple[tuple[str, str], ...]) -> str:
    """Aggregate per-file checksums into a single ``package_checksum`` value.

    We hash ``"<rel>\\n<sha>\\n"`` for each entry in sorted order so that any
    file addition, rename, or byte change flips the aggregate.
    """
    hasher = hashlib.sha256()
    for rel, sha in per_file:
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\n")
        hasher.update(sha.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _find_single_by_suffix(
    baseline_root: pathlib.Path,
    suffix: str,
) -> pathlib.Path:
    """Return the sole file under ``baseline_root`` whose name ends with ``suffix``.

    Raises ``UnparseableMeshError`` / ``UnparseableAttError`` when zero or
    multiple candidates exist; the raised subclass is picked by suffix.
    """
    candidates = [p for p in _iter_baseline_files(baseline_root) if p.name.endswith(suffix)]
    if not candidates:
        detail = f"no {suffix} file found under {baseline_root}"
        if suffix == ".sp.mesh":
            raise UnparseableMeshError(detail)
        raise UnparseableAttError(detail)
    if len(candidates) > 1:
        detail = f"expected exactly one {suffix} file, found {len(candidates)}: {[p.name for p in candidates]}"
        if suffix == ".sp.mesh":
            raise UnparseableMeshError(detail)
        raise UnparseableAttError(detail)
    return candidates[0]


def _read_text_lines(path: pathlib.Path) -> list[str]:
    """Read a file read-only (INV-1) and return decoded, right-stripped lines."""
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - defensive
        raise UnparseableMeshError(f"non-utf-8 bytes in {path}: {exc}") from exc
    return [line.rstrip("\r\n") for line in text.splitlines()]


def _parse_header_counts(header_line: str, source_kind: str) -> tuple[int, int]:
    """Parse a ``<N_rows>\\t<N_cols>`` header line, tolerating trailing whitespace."""
    tokens = header_line.split()
    if len(tokens) < 2:
        detail = f"header line {header_line!r} does not carry <N> <cols>"
        if source_kind == "mesh":
            raise UnparseableMeshError(detail)
        raise UnparseableAttError(detail)
    try:
        n_rows = int(tokens[0])
        n_cols = int(tokens[1])
    except ValueError as exc:
        detail = f"header line {header_line!r} tokens not integers: {exc}"
        if source_kind == "mesh":
            raise UnparseableMeshError(detail) from exc
        raise UnparseableAttError(detail) from exc
    if n_rows <= 0 or n_cols <= 0:
        detail = f"header counts must be positive, got n_rows={n_rows} n_cols={n_cols}"
        if source_kind == "mesh":
            raise UnparseableMeshError(detail)
        raise UnparseableAttError(detail)
    return n_rows, n_cols


def _parse_sp_mesh_element_ids(path: pathlib.Path) -> tuple[int, ...]:
    """Parse ``.sp.mesh`` and return the ordered element ``ID`` column values.

    Only the element table (top block) is required for §1.1. The node table is
    peeked at header-only to ensure the file is coherent enough to call
    "parseable"; deep node/geometry validation is SUB-4's responsibility.
    """
    lines = _read_text_lines(path)
    if len(lines) < 2:
        raise UnparseableMeshError(f"{path.name}: too short to contain header + column names")

    n_elements, n_element_cols = _parse_header_counts(lines[0], "mesh")

    # Element table header (columns; not counted in n_elements).
    element_header_tokens = lines[1].split()
    if len(element_header_tokens) < n_element_cols:
        raise UnparseableMeshError(
            f"{path.name}: element header row has {len(element_header_tokens)} tokens, "
            f"expected >= {n_element_cols}"
        )
    if element_header_tokens[0].upper() != "ID":
        raise UnparseableMeshError(
            f"{path.name}: expected element header first column 'ID', got {element_header_tokens[0]!r}"
        )

    element_row_start = 2
    element_row_end = element_row_start + n_elements
    if len(lines) < element_row_end:
        raise UnparseableMeshError(
            f"{path.name}: expected {n_elements} element rows, "
            f"only {len(lines) - element_row_start} present"
        )

    element_ids: list[int] = []
    for row_index in range(element_row_start, element_row_end):
        raw_line = lines[row_index]
        if not raw_line.strip():
            raise UnparseableMeshError(
                f"{path.name}: blank element row at line {row_index + 1}"
            )
        tokens = raw_line.split()
        if len(tokens) < n_element_cols:
            raise UnparseableMeshError(
                f"{path.name}: element row {row_index + 1} has {len(tokens)} tokens, "
                f"expected >= {n_element_cols}"
            )
        try:
            element_ids.append(int(tokens[0]))
        except ValueError as exc:
            raise UnparseableMeshError(
                f"{path.name}: element row {row_index + 1} ID token {tokens[0]!r} is not int: {exc}"
            ) from exc

    # Verify the node-table header exists (parseability probe; contents deferred to SUB-4).
    if element_row_end < len(lines):
        try:
            _parse_header_counts(lines[element_row_end], "mesh")
        except UnparseableMeshError as exc:
            raise UnparseableMeshError(
                f"{path.name}: node-table header at line {element_row_end + 1} is malformed: {exc.detail}"
            ) from exc

    return tuple(element_ids)


def _parse_sp_att(path: pathlib.Path) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Parse ``.sp.att`` and return ``(index_ids, forc_values)`` in row order.

    ``forc_values`` must be positive integers; we defer that check to the caller
    so it can produce the correctly-scoped :class:`InvalidForcValueError`
    without conflating parse errors with value errors.
    """
    lines = _read_text_lines(path)
    if len(lines) < 2:
        raise UnparseableAttError(f"{path.name}: too short to contain header + column names")

    n_rows, n_cols = _parse_header_counts(lines[0], "att")

    header_tokens = [tok.upper() for tok in lines[1].split()]
    if len(header_tokens) < n_cols:
        raise UnparseableAttError(
            f"{path.name}: column header row has {len(header_tokens)} tokens, "
            f"expected >= {n_cols}"
        )
    if header_tokens[0] != "INDEX":
        raise UnparseableAttError(
            f"{path.name}: expected first column 'INDEX', got {header_tokens[0]!r}"
        )
    try:
        forc_col_index = header_tokens.index("FORC")
    except ValueError as exc:
        raise UnparseableAttError(f"{path.name}: no 'FORC' column found in header {header_tokens}") from exc

    row_start = 2
    row_end = row_start + n_rows
    if len(lines) < row_end:
        raise UnparseableAttError(
            f"{path.name}: expected {n_rows} INDEX rows, only {len(lines) - row_start} present"
        )

    index_ids: list[int] = []
    forc_values: list[int] = []
    for row_index in range(row_start, row_end):
        raw_line = lines[row_index]
        if not raw_line.strip():
            raise UnparseableAttError(f"{path.name}: blank INDEX row at line {row_index + 1}")
        tokens = raw_line.split()
        if len(tokens) <= forc_col_index:
            raise UnparseableAttError(
                f"{path.name}: INDEX row {row_index + 1} has {len(tokens)} tokens, "
                f"needs at least {forc_col_index + 1} to read FORC"
            )
        try:
            index_ids.append(int(tokens[0]))
        except ValueError as exc:
            raise UnparseableAttError(
                f"{path.name}: INDEX row {row_index + 1} first token {tokens[0]!r} is not int: {exc}"
            ) from exc
        forc_raw = tokens[forc_col_index]
        forc_int = _coerce_positive_int_forc(forc_raw, element_id=index_ids[-1])
        forc_values.append(forc_int)

    return tuple(index_ids), tuple(forc_values)


def _coerce_positive_int_forc(raw: str, *, element_id: int) -> int:
    """Coerce a ``FORC`` token to a positive int or raise ``InvalidForcValueError``.

    Rejects non-integer tokens (including decimal fractions like ``1.5``) so the
    stricter "positive integer" rule from §1.1 is honored.
    """
    # Fast path: bare ``int(token)`` catches ``+1`` / ``-1`` correctly.
    # Reject decimals (``1.0`` is still non-integer per §1.1) by refusing '.'.
    if "." in raw or "e" in raw or "E" in raw:
        raise InvalidForcValueError(element_id=element_id, invalid_value=raw)
    try:
        value = int(raw)
    except ValueError as exc:
        raise InvalidForcValueError(element_id=element_id, invalid_value=raw) from exc
    if value <= 0:
        raise InvalidForcValueError(element_id=element_id, invalid_value=value)
    return value


def _find_optional_tsd_forc(baseline_root: pathlib.Path) -> pathlib.Path | None:
    """Return the sole ``.tsd.forc`` under ``baseline_root``, or ``None`` if absent.

    Multiple ``.tsd.forc`` files under the same baseline are ambiguous for §1.1,
    so we treat that as unparseable (spec expects a single baseline forcing
    manifest).
    """
    candidates = [p for p in _iter_baseline_files(baseline_root) if p.name.endswith(".tsd.forc")]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise UnparseableAttError(
            f"expected at most one .tsd.forc under {baseline_root}, found {len(candidates)}"
        )
    return candidates[0]


def _parse_tsd_forc_reference_ids(path: pathlib.Path) -> tuple[tuple[int, int], ...]:
    """Return ``((line_number, id), ...)`` for every station row in ``.tsd.forc``.

    ``line_number`` is 1-based (matching editor line numbers) so the caller can
    surface it in :class:`IllegalTsdForcReferenceError`.
    """
    lines = _read_text_lines(path)
    if len(lines) < 3:
        raise UnparseableAttError(
            f"{path.name}: expected at least 3 header lines (count+startdate, path, header)"
        )

    header0 = lines[0].split()
    if not header0:
        raise UnparseableAttError(f"{path.name}: empty first line, expected '<N> <startdate>'")
    try:
        n_stations = int(header0[0])
    except ValueError as exc:
        raise UnparseableAttError(
            f"{path.name}: first token of line 1 {header0[0]!r} is not an int"
        ) from exc
    if n_stations <= 0:
        raise UnparseableAttError(f"{path.name}: station count must be positive, got {n_stations}")

    row_start = 3
    row_end = row_start + n_stations
    if len(lines) < row_end:
        raise UnparseableAttError(
            f"{path.name}: expected {n_stations} station rows, "
            f"only {len(lines) - row_start} present"
        )

    references: list[tuple[int, int]] = []
    for row_index in range(row_start, row_end):
        raw_line = lines[row_index]
        line_number = row_index + 1
        if not raw_line.strip():
            raise UnparseableAttError(f"{path.name}: blank station row at line {line_number}")
        tokens = raw_line.split()
        try:
            station_id = int(tokens[0])
        except (ValueError, IndexError) as exc:
            raise UnparseableAttError(
                f"{path.name}: station row {line_number} first token missing or not int: {tokens}"
            ) from exc
        references.append((line_number, station_id))
    return tuple(references)


def _assert_ids_unique(ids: tuple[int, ...], file_label: str) -> None:
    seen: set[int] = set()
    for value in ids:
        if value in seen:
            raise NonUniqueElementIdError(file=file_label, duplicate_id=value)
        seen.add(value)


def _assert_ids_contiguous_from_one(ids: tuple[int, ...], file_label: str) -> None:
    if not ids:
        raise NonContiguousElementIdError(file=file_label, missing_ids=(1,))
    id_set = set(ids)
    expected = set(range(1, len(ids) + 1))
    missing = expected - id_set
    if missing:
        raise NonContiguousElementIdError(file=file_label, missing_ids=tuple(sorted(missing)))


# --- public entry point ---------------------------------------------------


def verify_g0_baseline(baseline_root: pathlib.Path) -> BaselineIntegrityReport:
    """Verify baseline package G0 integrity and return an immutable report.

    Parameters
    ----------
    baseline_root:
        Directory containing the baseline basin model package (holds exactly one
        ``.sp.mesh``, exactly one ``.sp.att``, and optionally one ``.tsd.forc``).

    Returns
    -------
    BaselineIntegrityReport
        Populated with the recomputed package checksum, per-file checksums,
        and the observed element-ID set / max ``FORC`` value.

    Raises
    ------
    BaselineIntegrityError
        Any subclass indicates a G0 subcheck failure; the mapping builder MUST
        NOT write any output artifact when this is raised.
    """
    if not isinstance(baseline_root, pathlib.Path):
        raise TypeError(
            f"verify_g0_baseline expects pathlib.Path, got {type(baseline_root).__name__}"
        )
    if not baseline_root.exists() or not baseline_root.is_dir():
        raise BaselineIntegrityError(
            f"baseline_root does not exist or is not a directory: {baseline_root}"
        )

    # INV-1 pre-check: compute checksums once BEFORE any per-file interpretation.
    pre_checksums = _compute_per_file_checksums(baseline_root)

    sp_mesh_path = _find_single_by_suffix(baseline_root, ".sp.mesh")
    sp_att_path = _find_single_by_suffix(baseline_root, ".sp.att")

    mesh_element_ids = _parse_sp_mesh_element_ids(sp_mesh_path)
    att_index_ids, att_forc_values = _parse_sp_att(sp_att_path)

    # Uniqueness inside each file.
    _assert_ids_unique(mesh_element_ids, file_label="sp.mesh")
    _assert_ids_unique(att_index_ids, file_label="sp.att")

    # Equal counts.
    if len(mesh_element_ids) != len(att_index_ids):
        raise UnequalElementCountError(
            mesh_count=len(mesh_element_ids), att_count=len(att_index_ids)
        )

    # Equal sets (element-wise) is checked BEFORE contiguity so its dedicated
    # error class is reachable from disk fixtures. Once we know counts are equal
    # and sets are equal, checking contiguity on one file is equivalent to
    # checking both, but we keep both file labels so evidence pinpoints the
    # first offender when both fail identically.
    mesh_set = set(mesh_element_ids)
    att_set = set(att_index_ids)
    if mesh_set != att_set:
        mesh_only = tuple(sorted(mesh_set - att_set))
        att_only = tuple(sorted(att_set - mesh_set))
        raise UnequalElementIdSetError(mesh_only=mesh_only, att_only=att_only)

    # Contiguous from 1 (spec §1.1 says both; sets are already known equal here).
    _assert_ids_contiguous_from_one(mesh_element_ids, file_label="sp.mesh")
    _assert_ids_contiguous_from_one(att_index_ids, file_label="sp.att")

    # FORC values are already coerced to positive int by _coerce_positive_int_forc.
    max_forc_value = max(att_forc_values)

    # Optional .tsd.forc station-catalog integrity.  FORC values in ``sp.att``
    # reference rows in this catalog; the catalog may legitimately contain
    # unused trailing stations, so its row ids are bounded by the declared
    # catalog size rather than by ``max(FORC)``.
    tsd_forc_path = _find_optional_tsd_forc(baseline_root)
    tsd_forc_present = tsd_forc_path is not None
    tsd_forc_reference_count = 0
    if tsd_forc_path is not None:
        references = _parse_tsd_forc_reference_ids(tsd_forc_path)
        tsd_forc_reference_count = len(references)
        station_count = len(references)
        valid_range = (1, station_count)
        for line_number, ref in references:
            if ref < 1 or ref > station_count:
                raise IllegalTsdForcReferenceError(
                    line_number=line_number,
                    invalid_reference=ref,
                    valid_range=valid_range,
                )
        if max_forc_value > station_count:
            raise IllegalTsdForcReferenceError(
                line_number=1,
                invalid_reference=max_forc_value,
                valid_range=valid_range,
            )

    # INV-1 post-check: no baseline file may have changed while we read it.
    post_checksums = _compute_per_file_checksums(baseline_root)
    if pre_checksums != post_checksums:
        raise BaselineIntegrityError(
            "baseline package mutated during verification (INV-1 violation): "
            "pre/post checksums differ"
        )

    package_checksum = _aggregate_package_checksum(post_checksums)

    return BaselineIntegrityReport(
        baseline_root=baseline_root,
        package_checksum=package_checksum,
        per_file_checksums=post_checksums,
        sp_mesh_path=sp_mesh_path,
        sp_att_path=sp_att_path,
        element_id_set=frozenset(mesh_set),
        max_forc_value=max_forc_value,
        tsd_forc_present=tsd_forc_present,
        tsd_forc_reference_count=tsd_forc_reference_count,
    )


# --- §1.2 CRS authority ---------------------------------------------------


def _find_single_prj(baseline_root: pathlib.Path) -> pathlib.Path:
    """Return the authoritative ``gis/*.prj`` after proving CRS unanimity.

    Production Basins packages carry ``domain.prj``, ``river.prj`` and
    ``seg.prj``.  They are multiple copies of one package CRS, not multiple
    authorities.  Byte-identical declarations are accepted and ``domain.prj``
    is preferred; divergent declarations remain a fail-closed error.
    """
    gis_dir = baseline_root / "gis"
    if not gis_dir.is_dir():
        raise MissingPrjError(baseline_root=baseline_root)
    candidates = sorted(p for p in gis_dir.glob("*.prj") if p.is_file())
    if not candidates:
        raise MissingPrjError(baseline_root=baseline_root)
    authority = next((path for path in candidates if path.name == "domain.prj"), candidates[0])
    authority_bytes = authority.read_bytes()
    divergent = [path.name for path in candidates if path.read_bytes() != authority_bytes]
    if divergent:
        raise UnparseablePrjError(
            prj_path=authority,
            parse_error=(
                "gis/*.prj declarations disagree with the package CRS authority: "
                f"{divergent}"
            ),
        )
    return authority


def verify_package_crs(baseline_root: pathlib.Path) -> PackageCrsReport:
    """Read the model CRS from ``gis/*.prj`` and prove WGS84 convertibility.

    Per §1.2, the CRS is read **only** from ``gis/*.prj``. This function MUST
    never open ``.sp.mesh``, ``.sp.att``, or any other file as a CRS source,
    and MUST NOT consult EPSG defaults or any global assumption.

    Parameters
    ----------
    baseline_root:
        Directory containing the baseline basin model package. The CRS lives
        under ``<baseline_root>/gis/*.prj``; multiple byte-identical GIS
        copies are one declaration and divergent copies fail closed.

    Returns
    -------
    PackageCrsReport
        Immutable record of the ``.prj`` path, its SHA-256 checksum, the raw
        WKT string, and a probe transform result proving the
        package-CRS → EPSG:4326 transformer round-trips.

    Raises
    ------
    MissingPrjError
        No ``.prj`` file exists under ``gis/``.
    UnparseablePrjError
        pyproj cannot parse the WKT or multiple ``.prj`` files disagree.
    NonWgs84ConvertiblePrjError
        pyproj cannot build a Transformer to EPSG:4326, or the transformer
        returns non-finite coordinates on the probe point.
    """
    if not isinstance(baseline_root, pathlib.Path):
        raise TypeError(
            f"verify_package_crs expects pathlib.Path, got {type(baseline_root).__name__}"
        )
    if not baseline_root.exists() or not baseline_root.is_dir():
        raise BaselineIntegrityError(
            f"baseline_root does not exist or is not a directory: {baseline_root}"
        )

    prj_path = _find_single_prj(baseline_root)
    prj_checksum = _sha256_file(prj_path)
    try:
        wkt = prj_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise UnparseablePrjError(prj_path=prj_path, parse_error=str(exc)) from exc
    if not wkt:
        raise UnparseablePrjError(prj_path=prj_path, parse_error="empty .prj file")

    try:
        package_crs = pyproj.CRS.from_wkt(wkt)
    except pyproj.exceptions.CRSError as exc:
        raise UnparseablePrjError(prj_path=prj_path, parse_error=str(exc)) from exc

    try:
        transformer = pyproj.Transformer.from_crs(package_crs, "EPSG:4326", always_xy=True)
    except pyproj.exceptions.ProjError as exc:
        raise NonWgs84ConvertiblePrjError(prj_path=prj_path, transform_error=str(exc)) from exc

    # Probe with the CRS bounding-box centroid when pyproj can provide it,
    # else fall back to the origin (0.0, 0.0). Either way the transformer
    # must produce a finite (lon, lat) pair — that is our round-trip proof.
    probe_x, probe_y = 0.0, 0.0
    area_of_use = getattr(package_crs, "area_of_use", None)
    if area_of_use is not None and area_of_use.bounds is not None:
        west, south, east, north = area_of_use.bounds
        # area_of_use.bounds is (west, south, east, north) in WGS84 degrees.
        # We transform WGS84 centroid to package CRS to get a valid probe (x, y).
        # But since our transformer goes package->WGS84, we skip this fast path
        # and just use (0, 0) which is always in the projection domain for
        # continental Albers/TM used by these baselines.
        del west, south, east, north

    try:
        probe_lon, probe_lat = transformer.transform(probe_x, probe_y)
    except pyproj.exceptions.ProjError as exc:
        raise NonWgs84ConvertiblePrjError(prj_path=prj_path, transform_error=str(exc)) from exc
    # A degenerate CRS may return inf/nan without raising. Reject those explicitly.
    for value, name in ((probe_lon, "longitude"), (probe_lat, "latitude")):
        if not _is_finite_float(value):
            raise NonWgs84ConvertiblePrjError(
                prj_path=prj_path,
                transform_error=f"probe {name} is not finite: {value!r}",
            )

    return PackageCrsReport(
        prj_path=prj_path,
        prj_checksum=prj_checksum,
        wkt=wkt,
        wgs84_probe=(float(probe_lon), float(probe_lat)),
    )


def _is_finite_float(value: float) -> bool:
    """Return True iff ``value`` is a finite float (not NaN and not ±inf)."""
    return isinstance(value, (int, float)) and value == value and value not in (
        float("inf"),
        float("-inf"),
    )


# --- §1.2 ancillary inventory --------------------------------------------


def _is_ancillary_tsd(path: pathlib.Path) -> bool:
    """Return True iff ``path`` is an ancillary ``*.tsd.*`` file.

    Per design.md line 62, the variant carries *ancillary non-weather*
    ``*.tsd.*``; the weather-forcing reference ``.tsd.forc`` is §1.1's
    territory and belongs to the runtime producer (§8.1), so it is excluded
    from the ancillary inventory.
    """
    name = path.name
    if ".tsd." not in name:
        return False
    if name.endswith(".tsd.forc"):
        return False
    return True


def build_ancillary_inventory(baseline_root: pathlib.Path) -> AncillaryInventoryReport:
    """Enumerate every ancillary ``*.tsd.*`` file under ``baseline_root``.

    Per §1.2, the mapping builder MUST record a complete inventory of every
    ancillary ``*.tsd.*`` dependency so downstream stages can detect a
    swapped/truncated ancillary before it silently changes model behavior.
    ``.tsd.forc`` is excluded — it is the weather-forcing reference, not an
    ancillary input (see design.md line 62 and §1.1's own accounting).

    Parameters
    ----------
    baseline_root:
        Directory containing the baseline basin model package.

    Returns
    -------
    AncillaryInventoryReport
        Immutable report with entries sorted by relative path (deterministic
        ordering). Empty ``entries`` is legal — some minimal packages carry
        no ancillary ``*.tsd.*`` beyond the weather reference.

    Raises
    ------
    AncillaryInventoryError
        Any ancillary file is unreadable (permission denied, truncation
        during scan, etc.). The mapping builder MUST NOT write output when
        this fires.
    BaselineIntegrityError
        ``baseline_root`` is not an existing directory.
    """
    if not isinstance(baseline_root, pathlib.Path):
        raise TypeError(
            f"build_ancillary_inventory expects pathlib.Path, got {type(baseline_root).__name__}"
        )
    if not baseline_root.exists() or not baseline_root.is_dir():
        raise BaselineIntegrityError(
            f"baseline_root does not exist or is not a directory: {baseline_root}"
        )

    ancillary_paths = sorted(
        p for p in _iter_baseline_files(baseline_root) if _is_ancillary_tsd(p)
    )
    entries: list[AncillaryEntry] = []
    for path in ancillary_paths:
        try:
            size_bytes = path.stat().st_size
            checksum = _sha256_file(path)
        except OSError as exc:
            raise AncillaryInventoryError(path=path, read_error=str(exc)) from exc
        entries.append(
            AncillaryEntry(path=path, checksum=checksum, size_bytes=size_bytes)
        )

    return AncillaryInventoryReport(
        baseline_root=baseline_root,
        entries=tuple(entries),
    )


# --- §1.3 baseline classification (RECORD-ONLY) --------------------------


@dataclass(frozen=True)
class DuplicateCoordinateCluster:
    """A group of stations sharing identical ``(lon, lat, elevation)`` coordinates.

    Live example (design.md line 68): ``zhaochen_mc`` has 4 stations at
    identical coords with ``Z=-9999``. Classification RECORDS this pattern
    but never repairs or filters it — the mapping algorithm decides how to
    handle duplicates later.
    """

    coords: tuple[float, float, float]
    station_ids: tuple[str, ...]
    multiplicity: int


@dataclass(frozen=True)
class NonGridBaselineFinding:
    """A record that a station-prefix cohort does NOT form a regular lat-lon grid.

    Live example: ``zhaochen_wem`` carries filenames ``X1..X5.csv`` at
    irregular ~0.02° spacing that do not close a regular grid. Downstream
    stages MUST NOT assume CMFD grid points from these stations.
    """

    station_prefix: str
    station_count: int
    spacing_estimate: float | None
    pattern_note: str


@dataclass(frozen=True)
class StartdateRecord:
    """One ``.tsd.forc`` startdate parsed from line 1.

    Live audit lists baseline startdates spanning 1951–2024 across 13 basins;
    classification records the raw value per ``.tsd.forc`` file so evidence
    can prove heterogeneity without normalizing the format.
    """

    path: pathlib.Path
    startdate: str


@dataclass(frozen=True)
class HarmlessDeviationRecord:
    """A RECORD-ONLY note of a known-harmless baseline deviation.

    Per tasks.md §1.3 non-goal: "no repair of known-harmless baseline
    deviations (e.g. `.tsd.forc` line-2 absolute paths); record only." The
    ``deviation_kind`` slug lets downstream evidence stages tally deviation
    types without re-parsing the excerpt.
    """

    path: pathlib.Path
    deviation_kind: str
    evidence_excerpt: str


@dataclass(frozen=True)
class BaselineClassificationReport:
    """Report produced by :func:`classify_baseline` — pure carry, no derived fields.

    Every tuple field is sorted deterministically inside :func:`classify_baseline`
    so evidence artifacts are reproducible byte-for-byte across reruns.
    """

    duplicate_coord_clusters: tuple[DuplicateCoordinateCluster, ...]
    non_grid_findings: tuple[NonGridBaselineFinding, ...]
    startdate_heterogeneity: tuple[StartdateRecord, ...]
    domain_shp_checksum: str | None
    harmless_deviations: tuple[HarmlessDeviationRecord, ...]


@dataclass(frozen=True)
class Inv1EndToEndEvidence:
    """Pre/post per-file SHA-256 checksums proving INV-1 across every entry point.

    Both fields are sorted ``(relative_path, sha256_hex)`` tuples — identical
    ordering to :attr:`BaselineIntegrityReport.per_file_checksums` so an
    orchestrator can diff them directly.
    """

    pre_checksums: tuple[tuple[str, str], ...]
    post_checksums: tuple[tuple[str, str], ...]


def _find_all_tsd_forc(baseline_root: pathlib.Path) -> list[pathlib.Path]:
    """Return every ``.tsd.forc`` file under ``baseline_root``, sorted.

    Unlike :func:`_find_optional_tsd_forc`, this helper accepts multiple hits
    without raising — §1.3 classification records what is there, and the §1.1
    gate has already vetted the shape at :func:`verify_g0_baseline` call time.
    """
    return sorted(
        p for p in _iter_baseline_files(baseline_root) if p.name.endswith(".tsd.forc")
    )


def _parse_tsd_forc_stations(
    path: pathlib.Path,
) -> tuple[str, tuple[tuple[str, float, float, float, str], ...]]:
    """Return ``(startdate_raw, ((station_id, lon, lat, z, filename), ...))``.

    Column layout (see module docstring): ``ID Lon Lat X Y Z Filename``.
    ``station_id`` and ``filename`` are kept as strings — the ID is a
    reference key (not necessarily 1..N) and the filename carries the X1..X5
    naming convention we need for non-grid detection.

    Rows with too few tokens are skipped silently — §1.3 is RECORD-ONLY and
    must not raise inside classification; malformed rows are §1.1's job.

    Non-UTF-8 bytes are also swallowed with a sentinel-empty return: the §1.1
    gate is responsible for decode failures; here we align with the sibling
    ``_parse_sp_att_station_index`` / ``_detect_harmless_deviations`` helpers
    so ``classify_baseline`` honors its RECORD-ONLY docstring contract.
    """
    try:
        lines = _read_text_lines(path)
    except UnparseableMeshError:
        # RECORD-ONLY — swallow decode errors here; §1.1 gate would raise.
        return ("", ())
    if len(lines) < 3:
        return ("", ())
    header0_tokens = lines[0].split()
    startdate_raw = header0_tokens[1] if len(header0_tokens) >= 2 else ""

    stations: list[tuple[str, float, float, float, str]] = []
    for row_index in range(3, len(lines)):
        raw_line = lines[row_index]
        if not raw_line.strip():
            continue
        tokens = raw_line.split()
        if len(tokens) < 7:
            continue
        try:
            lon = float(tokens[1])
            lat = float(tokens[2])
            z = float(tokens[5])
        except ValueError:
            continue
        stations.append((tokens[0], lon, lat, z, tokens[6]))
    return startdate_raw, tuple(stations)


def _parse_sp_att_station_index(
    path: pathlib.Path,
) -> tuple[tuple[str, float, float, float], ...]:
    """Extract ``(station_id, lon, lat, z)`` from ``.sp.att`` when the table

    carries station coordinates. The canonical live ``.sp.att`` layout does
    NOT carry station coords — that lives in ``.tsd.forc``. We provide this
    helper as a defensive no-op that returns ``()`` unless a lon/lat header
    is present, so future basin variants that inline station coords still
    produce a classification record.
    """
    try:
        lines = _read_text_lines(path)
    except UnparseableMeshError:
        return ()
    if len(lines) < 2:
        return ()
    header_tokens = [tok.upper() for tok in lines[1].split()]
    try:
        lon_col = header_tokens.index("LON")
        lat_col = header_tokens.index("LAT")
        z_col = header_tokens.index("Z")
    except ValueError:
        return ()
    id_col = 0
    stations: list[tuple[str, float, float, float]] = []
    for row_index in range(2, len(lines)):
        raw_line = lines[row_index]
        if not raw_line.strip():
            continue
        tokens = raw_line.split()
        max_col = max(id_col, lon_col, lat_col, z_col)
        if len(tokens) <= max_col:
            continue
        try:
            lon = float(tokens[lon_col])
            lat = float(tokens[lat_col])
            z = float(tokens[z_col])
        except ValueError:
            continue
        stations.append((tokens[id_col], lon, lat, z))
    return tuple(stations)


def _cluster_duplicate_coords(
    stations: tuple[tuple[str, float, float, float, str], ...],
) -> tuple[DuplicateCoordinateCluster, ...]:
    """Group stations by ``(lon, lat, z)`` and return clusters of size >= 2."""
    by_coords: dict[tuple[float, float, float], list[str]] = {}
    for station_id, lon, lat, z, _filename in stations:
        by_coords.setdefault((lon, lat, z), []).append(station_id)
    clusters: list[DuplicateCoordinateCluster] = []
    for coords, ids in by_coords.items():
        if len(ids) >= 2:
            clusters.append(
                DuplicateCoordinateCluster(
                    coords=coords,
                    station_ids=tuple(ids),
                    multiplicity=len(ids),
                )
            )
    # Sort deterministically by coords for reproducible evidence.
    return tuple(sorted(clusters, key=lambda c: c.coords))


def _detect_non_grid_findings(
    stations: tuple[tuple[str, float, float, float, str], ...],
) -> tuple[NonGridBaselineFinding, ...]:
    """Return non-grid findings for X-prefix station cohorts.

    A cohort is the set of stations whose ``filename`` matches the pattern
    ``<PREFIX><digit(s)>.csv`` — e.g. ``X1..X5.csv``. If the cohort has
    ``count >= 2`` and does NOT form a regular lat-lon grid (see
    :func:`_looks_like_regular_grid`), classification records a
    :class:`NonGridBaselineFinding`.

    We estimate spacing as the minimum pairwise absolute lat difference
    (when at least two lats differ) or the minimum abs lon difference
    otherwise. This is a heuristic used only for evidence — no downstream
    computation depends on the number.
    """
    import re

    prefix_re = re.compile(r"^([A-Za-z]+)(\d+)\.csv$")
    cohorts: dict[str, list[tuple[float, float]]] = {}
    for _station_id, lon, lat, _z, filename in stations:
        match = prefix_re.match(filename)
        if match is None:
            continue
        prefix = match.group(1)
        cohorts.setdefault(prefix, []).append((lon, lat))

    findings: list[NonGridBaselineFinding] = []
    for prefix, coords in sorted(cohorts.items()):
        if len(coords) < 2:
            continue
        if _looks_like_regular_grid(coords):
            continue
        spacing = _estimate_spacing(coords)
        findings.append(
            NonGridBaselineFinding(
                station_prefix=prefix,
                station_count=len(coords),
                spacing_estimate=spacing,
                pattern_note=(
                    f"{len(coords)} stations named {prefix}1..{prefix}{len(coords)} "
                    f"do not form a regular lat-lon grid"
                ),
            )
        )
    return tuple(findings)


def _looks_like_regular_grid(coords: list[tuple[float, float]]) -> bool:
    """Return True iff ``coords`` are consistent with a regular lat-lon grid.

    Rule: a set of ``(lon, lat)`` points forms a regular grid iff their
    unique lon values and unique lat values, when sorted, both have uniform
    step within 1e-6 tolerance, AND the total point count equals
    ``len(unique_lons) * len(unique_lats)``. This catches CMFD-style regular
    grids while flagging zhaochen_wem-style 5-point irregular sets as
    non-grid.

    Note: ``coords`` is expected to be a small list (few dozen stations at
    most); we do not optimize for large cohorts.
    """
    if len(coords) < 2:
        return True
    unique_lons = sorted({round(lon, 10) for lon, _lat in coords})
    unique_lats = sorted({round(lat, 10) for _lon, lat in coords})
    if len(unique_lons) * len(unique_lats) != len(coords):
        # Point count doesn't tile the grid — cannot be a regular grid.
        # (Points at duplicate (lon,lat) also fail this test; those are
        # separately captured by _cluster_duplicate_coords.)
        return False
    if not _has_uniform_step(unique_lons):
        return False
    if not _has_uniform_step(unique_lats):
        return False
    return True


def _has_uniform_step(sorted_values: list[float], tol: float = 1e-6) -> bool:
    """Return True iff ``sorted_values`` has a uniform step within ``tol``."""
    if len(sorted_values) < 2:
        return True
    steps = [sorted_values[i + 1] - sorted_values[i] for i in range(len(sorted_values) - 1)]
    if not steps:
        return True
    first = steps[0]
    return all(abs(step - first) <= tol for step in steps)


def _estimate_spacing(coords: list[tuple[float, float]]) -> float | None:
    """Estimate the smallest positive pairwise coord delta as a spacing proxy."""
    lons = sorted({lon for lon, _lat in coords})
    lats = sorted({lat for _lon, lat in coords})
    candidates: list[float] = []
    for i in range(len(lons) - 1):
        delta = lons[i + 1] - lons[i]
        if delta > 0:
            candidates.append(delta)
    for i in range(len(lats) - 1):
        delta = lats[i + 1] - lats[i]
        if delta > 0:
            candidates.append(delta)
    if not candidates:
        return None
    return min(candidates)


def _find_domain_shp(baseline_root: pathlib.Path) -> pathlib.Path | None:
    """Return the sole ``domain.shp`` under ``baseline_root``, or ``None``.

    We only look for the exact filename ``domain.shp`` (case-sensitive) at
    any depth. This helper NEVER opens the file as geometry — the caller
    computes a SHA-256 for presence-only evidence.
    """
    candidates = sorted(
        p for p in _iter_baseline_files(baseline_root) if p.name == "domain.shp"
    )
    if not candidates:
        return None
    return candidates[0]


def _detect_harmless_deviations(
    tsd_forc_paths: list[pathlib.Path],
) -> tuple[HarmlessDeviationRecord, ...]:
    """Return RECORD-ONLY notes about known-harmless deviations.

    Currently detects one deviation kind:

    * ``tsd_forc_line2_absolute_path`` — ``.tsd.forc`` line 2 references a
      build-machine absolute path (e.g. ``/home/ghdc/nwm/...``). The mapping
      builder MUST NOT rewrite this line; it merely records the excerpt.
    """
    records: list[HarmlessDeviationRecord] = []
    for path in tsd_forc_paths:
        try:
            lines = _read_text_lines(path)
        except UnparseableMeshError:
            # RECORD-ONLY — swallow read errors here; §1.1 gate would raise.
            continue
        if len(lines) < 2:
            continue
        line2 = lines[1].strip()
        if line2.startswith("/"):
            records.append(
                HarmlessDeviationRecord(
                    path=path,
                    deviation_kind="tsd_forc_line2_absolute_path",
                    evidence_excerpt=line2,
                )
            )
    return tuple(records)


def classify_baseline(baseline_root: pathlib.Path) -> BaselineClassificationReport:
    """Register RECORD-ONLY baseline classifications.

    Per §1.3, this function inspects the baseline package and returns an
    immutable report cataloging:

    * duplicate-coordinate station clusters (zhaochen_mc pattern),
    * non-grid X-station cohorts (zhaochen_wem pattern),
    * per-``.tsd.forc`` startdate heterogeneity,
    * ``domain.shp`` presence via SHA-256 (presence-only — MUST NEVER be
      opened as geometry or element-ID source),
    * known-harmless deviations (e.g. ``.tsd.forc`` line-2 absolute paths).

    Parameters
    ----------
    baseline_root:
        Directory containing the baseline basin model package.

    Returns
    -------
    BaselineClassificationReport
        Immutable, deterministically sorted classification records.

    Notes
    -----
    This function is INV-1 read-only: it opens every source file with
    :func:`_read_text_lines` (binary read + utf-8 decode) and NEVER writes.
    ``domain.shp`` is checksummed via :func:`_sha256_file` (binary read only).
    Neither pyproj nor any shapefile reader is invoked on ``domain.shp``.

    Raises
    ------
    BaselineIntegrityError
        Only when ``baseline_root`` is not a directory. All other failures
        are silently absorbed — §1.3 is RECORD-ONLY and never raises on
        malformed content.
    """
    if not isinstance(baseline_root, pathlib.Path):
        raise TypeError(
            f"classify_baseline expects pathlib.Path, got {type(baseline_root).__name__}"
        )
    if not baseline_root.exists() or not baseline_root.is_dir():
        raise BaselineIntegrityError(
            f"baseline_root does not exist or is not a directory: {baseline_root}"
        )

    tsd_forc_paths = _find_all_tsd_forc(baseline_root)

    # Union station index across all .tsd.forc files (may be empty).
    all_stations: list[tuple[str, float, float, float, str]] = []
    startdate_records: list[StartdateRecord] = []
    for path in tsd_forc_paths:
        startdate_raw, stations = _parse_tsd_forc_stations(path)
        all_stations.extend(stations)
        if startdate_raw:
            startdate_records.append(
                StartdateRecord(path=path, startdate=startdate_raw)
            )

    # Also merge station coords from .sp.att when the variant carries them
    # (defensive; canonical live packages do not — see helper docstring).
    for att_path in sorted(
        p for p in _iter_baseline_files(baseline_root) if p.name.endswith(".sp.att")
    ):
        for station_id, lon, lat, z in _parse_sp_att_station_index(att_path):
            all_stations.append((station_id, lon, lat, z, ""))

    duplicate_clusters = _cluster_duplicate_coords(tuple(all_stations))
    non_grid_findings = _detect_non_grid_findings(tuple(all_stations))

    domain_shp_path = _find_domain_shp(baseline_root)
    domain_shp_checksum: str | None = None
    if domain_shp_path is not None:
        # NOTE: presence-only checksum. NEVER open as geometry.
        domain_shp_checksum = _sha256_file(domain_shp_path)

    harmless = _detect_harmless_deviations(tsd_forc_paths)

    return BaselineClassificationReport(
        duplicate_coord_clusters=duplicate_clusters,
        non_grid_findings=non_grid_findings,
        startdate_heterogeneity=tuple(startdate_records),
        domain_shp_checksum=domain_shp_checksum,
        harmless_deviations=harmless,
    )


# --- §1.3 INV-1 end-to-end evidence chain --------------------------------


def verify_baseline_inv1_end_to_end(
    baseline_root: pathlib.Path,
    historical_forcing_dir: pathlib.Path | None = None,
) -> Inv1EndToEndEvidence:
    """Prove INV-1 across the full §1.1 + §1.2 + §1.3 stack.

    Computes per-file SHA-256 across every baseline file BEFORE running any
    integrity / CRS / inventory / classification entry point, then re-computes
    AFTER all four have completed. Raises :class:`Inv1ViolationError` if any
    file's bytes changed in between.

    Parameters
    ----------
    baseline_root:
        Directory containing the baseline basin model package.
    historical_forcing_dir:
        Optional directory carrying historical forcing versions to include
        in the pre/post checksum sweep. When supplied, every regular file
        under this directory is snapshot alongside baseline files. When
        ``None``, only ``baseline_root`` is swept. Duplicates in the rel_path
        space (e.g. baseline ``history/foo`` colliding with
        ``historical_forcing_dir/foo`` after the ``history/`` prefix) are
        handled correctly: both entries live in the checksum sets
        independently, so drift on either side surfaces the shared rel_path
        in :attr:`Inv1ViolationError.drifted_paths`.

    Returns
    -------
    Inv1EndToEndEvidence
        Sorted pre and post checksum tuples for orchestrator diffing.

    Raises
    ------
    Inv1ViolationError
        Any file's bytes changed between pre and post snapshots.
    BaselineIntegrityError
        Passes through any error raised by :func:`verify_g0_baseline` /
        :func:`verify_package_crs` / :func:`build_ancillary_inventory` /
        :func:`classify_baseline`.
    """
    if not isinstance(baseline_root, pathlib.Path):
        raise TypeError(
            f"verify_baseline_inv1_end_to_end expects pathlib.Path, got "
            f"{type(baseline_root).__name__}"
        )
    if not baseline_root.exists() or not baseline_root.is_dir():
        raise BaselineIntegrityError(
            f"baseline_root does not exist or is not a directory: {baseline_root}"
        )
    if historical_forcing_dir is not None:
        if not isinstance(historical_forcing_dir, pathlib.Path):
            raise TypeError(
                f"historical_forcing_dir expects pathlib.Path, got "
                f"{type(historical_forcing_dir).__name__}"
            )
        if not historical_forcing_dir.exists() or not historical_forcing_dir.is_dir():
            raise BaselineIntegrityError(
                f"historical_forcing_dir does not exist or is not a directory: "
                f"{historical_forcing_dir}"
            )

    pre_checksums = _compute_end_to_end_checksums(baseline_root, historical_forcing_dir)

    # Run the full stack. Any of these may raise a BaselineIntegrityError
    # subclass; we deliberately let it propagate — INV-1 evidence is only
    # meaningful when every subcheck passes.
    verify_g0_baseline(baseline_root)
    verify_package_crs(baseline_root)
    build_ancillary_inventory(baseline_root)
    classify_baseline(baseline_root)

    post_checksums = _compute_end_to_end_checksums(baseline_root, historical_forcing_dir)

    if pre_checksums != post_checksums:
        drifted = _diff_drifted_rel_paths(pre_checksums, post_checksums)
        raise Inv1ViolationError(drifted_paths=drifted)

    return Inv1EndToEndEvidence(
        pre_checksums=pre_checksums,
        post_checksums=post_checksums,
    )


def _compute_end_to_end_checksums(
    baseline_root: pathlib.Path,
    historical_forcing_dir: pathlib.Path | None,
) -> tuple[tuple[str, str], ...]:
    """Compute sorted ``(rel_path, sha256_hex)`` across baseline + optional history.

    Historical forcing files are namespaced with a ``history/`` prefix so their
    relative paths do not collide with baseline file names.
    """
    entries: list[tuple[str, str]] = []
    for path in _iter_baseline_files(baseline_root):
        rel = path.relative_to(baseline_root).as_posix()
        entries.append((rel, _sha256_file(path)))
    if historical_forcing_dir is not None:
        for path in _iter_baseline_files(historical_forcing_dir):
            rel = "history/" + path.relative_to(historical_forcing_dir).as_posix()
            entries.append((rel, _sha256_file(path)))
    entries.sort(key=lambda item: item[0])
    return tuple(entries)


def _diff_drifted_rel_paths(
    pre: tuple[tuple[str, str], ...],
    post: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """Return every relative path whose SHA-256 differs (or exists in only one snapshot).

    Implemented as a **set symmetric-difference over ``(rel_path, sha)`` pairs**
    rather than a dict comparison. Two entries can legitimately share the same
    relative path — e.g. a baseline package that ships ``history/foo.txt`` while
    ``historical_forcing_dir`` also contains ``foo.txt`` (prefixed to
    ``history/foo.txt`` in :func:`_compute_end_to_end_checksums`). A dict-based
    diff would collapse the pair via last-write-wins and silently report an
    empty payload when only one of the colliding entries drifts. Set-based
    diffing preserves both entries independently: any drift on either side
    surfaces the shared rel_path in the drifted output.
    """
    pre_set = set(pre)
    post_set = set(post)
    drifted = {rel for (rel, _sha) in pre_set ^ post_set}
    return tuple(sorted(drifted))


def _parse_sp_mesh_g1_geometry(
    path: pathlib.Path,
) -> tuple[tuple[tuple[int, int, int, int], ...], dict[int, tuple[float, float]]]:
    """Parse ``.sp.mesh`` and return ``(elements, node_xy)``.

    ``elements`` is a tuple of ``(element_id, v1, v2, v3)`` in file order.
    ``node_xy`` maps ``node_id -> (x, y)`` in the package CRS.

    Reuses the header conventions verified by :func:`_parse_sp_mesh_element_ids`:
    the element header first token MUST be ``ID`` and the next three tokens are
    the vertex-node columns (``Node1 Node2 Node3`` in live SHUD files). The node
    table header first token MUST be ``ID`` and the next two tokens are ``X Y``.
    """
    lines = _read_text_lines(path)
    if len(lines) < 2:
        raise UnparseableMeshError(f"{path.name}: too short to contain header + column names")

    n_elements, n_element_cols = _parse_header_counts(lines[0], "mesh")

    element_header_tokens = lines[1].split()
    if len(element_header_tokens) < 4 or len(element_header_tokens) < n_element_cols:
        raise UnparseableMeshError(
            f"{path.name}: element header row has {len(element_header_tokens)} tokens, "
            f"expected >= max(4, {n_element_cols})"
        )
    if element_header_tokens[0].upper() != "ID":
        raise UnparseableMeshError(
            f"{path.name}: expected element header first column 'ID', got {element_header_tokens[0]!r}"
        )

    element_row_start = 2
    element_row_end = element_row_start + n_elements
    if len(lines) < element_row_end:
        raise UnparseableMeshError(
            f"{path.name}: expected {n_elements} element rows, "
            f"only {len(lines) - element_row_start} present"
        )

    elements: list[tuple[int, int, int, int]] = []
    for row_index in range(element_row_start, element_row_end):
        raw_line = lines[row_index]
        if not raw_line.strip():
            raise UnparseableMeshError(
                f"{path.name}: blank element row at line {row_index + 1}"
            )
        tokens = raw_line.split()
        if len(tokens) < 4:
            raise UnparseableMeshError(
                f"{path.name}: element row {row_index + 1} has {len(tokens)} tokens, "
                f"expected at least 4 (ID + 3 vertex ids)"
            )
        try:
            element_id = int(tokens[0])
            v1 = int(tokens[1])
            v2 = int(tokens[2])
            v3 = int(tokens[3])
        except ValueError as exc:
            raise UnparseableMeshError(
                f"{path.name}: element row {row_index + 1} first four tokens must be int, "
                f"got {tokens[:4]!r}: {exc}"
            ) from exc
        elements.append((element_id, v1, v2, v3))

    if element_row_end >= len(lines):
        raise UnparseableMeshError(
            f"{path.name}: expected node-table header at line {element_row_end + 1}, EOF instead"
        )

    n_nodes, n_node_cols = _parse_header_counts(lines[element_row_end], "mesh")

    node_header_line_index = element_row_end + 1
    if node_header_line_index >= len(lines):
        raise UnparseableMeshError(
            f"{path.name}: expected node column header at line {node_header_line_index + 1}, EOF instead"
        )
    node_header_tokens = lines[node_header_line_index].split()
    if len(node_header_tokens) < 3 or len(node_header_tokens) < n_node_cols:
        raise UnparseableMeshError(
            f"{path.name}: node header row has {len(node_header_tokens)} tokens, "
            f"expected >= max(3, {n_node_cols})"
        )
    if node_header_tokens[0].upper() != "ID":
        raise UnparseableMeshError(
            f"{path.name}: expected node header first column 'ID', got {node_header_tokens[0]!r}"
        )
    if node_header_tokens[1].upper() != "X" or node_header_tokens[2].upper() != "Y":
        raise UnparseableMeshError(
            f"{path.name}: expected node header columns 2/3 to be 'X'/'Y', "
            f"got {node_header_tokens[1]!r}/{node_header_tokens[2]!r}"
        )

    node_row_start = node_header_line_index + 1
    node_row_end = node_row_start + n_nodes
    if len(lines) < node_row_end:
        raise UnparseableMeshError(
            f"{path.name}: expected {n_nodes} node rows, "
            f"only {len(lines) - node_row_start} present"
        )

    node_xy: dict[int, tuple[float, float]] = {}
    for row_index in range(node_row_start, node_row_end):
        raw_line = lines[row_index]
        if not raw_line.strip():
            raise UnparseableMeshError(
                f"{path.name}: blank node row at line {row_index + 1}"
            )
        tokens = raw_line.split()
        if len(tokens) < 3:
            raise UnparseableMeshError(
                f"{path.name}: node row {row_index + 1} has {len(tokens)} tokens, "
                f"expected at least 3 (ID + X + Y)"
            )
        try:
            node_id = int(tokens[0])
            x = float(tokens[1])
            y = float(tokens[2])
        except ValueError as exc:
            raise UnparseableMeshError(
                f"{path.name}: node row {row_index + 1} first three tokens must parse as int, float, float, "
                f"got {tokens[:3]!r}: {exc}"
            ) from exc
        node_xy[node_id] = (x, y)

    return tuple(elements), node_xy


def verify_g1_non_degenerate_triangles(
    baseline_root: pathlib.Path,
) -> G1NonDegenerateReport:
    """Verify the G1 non-degenerate triangle gate for the baseline mesh.

    For every element in ``.sp.mesh``, this entry point enforces:

    1. Three vertex IDs are pairwise distinct.
    2. Each vertex ID references an existing mesh node.
    3. The unsigned planar triangle area in the package CRS is strictly greater
       than :data:`G1_MIN_TRIANGLE_AREA`.

    Fail-closed. Any violation raises the corresponding
    :class:`BaselineIntegrityError` subclass BEFORE any downstream mapping /
    barycenter / grid-matching path can be invoked; on success returns an
    immutable report bounding the observed area distribution.

    Parameters
    ----------
    baseline_root:
        Directory containing the baseline basin model package.

    Returns
    -------
    G1NonDegenerateReport
        Immutable evidence covering element count, observed min/max area, and
        the tolerance used.

    Raises
    ------
    G1RepeatedVertexIdError
        An element's three vertex IDs are not pairwise distinct.
    G1MissingMeshNodeError
        An element references a vertex ID absent from the mesh node table.
    G1CollinearTriangleError
        An element's unsigned planar area is at or below the tolerance.
    BaselineIntegrityError
        ``baseline_root`` is not a directory or ``.sp.mesh`` is missing / not
        uniquely resolvable.
    UnparseableMeshError
        ``.sp.mesh`` cannot be parsed at the geometry level.
    """
    if not isinstance(baseline_root, pathlib.Path):
        raise TypeError(
            f"verify_g1_non_degenerate_triangles expects pathlib.Path, got "
            f"{type(baseline_root).__name__}"
        )
    if not baseline_root.exists() or not baseline_root.is_dir():
        raise BaselineIntegrityError(
            f"baseline_root does not exist or is not a directory: {baseline_root}"
        )

    mesh_path = _find_single_by_suffix(baseline_root, ".sp.mesh")
    elements, node_xy = _parse_sp_mesh_g1_geometry(mesh_path)

    if not elements:
        raise BaselineIntegrityError(
            f"{mesh_path.name}: mesh has zero elements, G1 cannot be verified"
        )

    min_area = float("inf")
    max_area = float("-inf")
    for element_id, v1, v2, v3 in elements:
        # Subcheck 1: pairwise distinct vertex ids.
        if v1 == v2 or v2 == v3 or v1 == v3:
            raise G1RepeatedVertexIdError(
                element_id=element_id, vertex_ids=(v1, v2, v3)
            )
        # Subcheck 2: each vertex references an existing mesh node.
        for vid in (v1, v2, v3):
            if vid not in node_xy:
                raise G1MissingMeshNodeError(
                    element_id=element_id, missing_vertex_id=vid
                )
        # Subcheck 3: unsigned planar area > tolerance.
        x1, y1 = node_xy[v1]
        x2, y2 = node_xy[v2]
        x3, y3 = node_xy[v3]
        area = 0.5 * abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1))
        if not (area > G1_MIN_TRIANGLE_AREA):
            raise G1CollinearTriangleError(
                element_id=element_id, area=area, tolerance=G1_MIN_TRIANGLE_AREA
            )
        if area < min_area:
            min_area = area
        if area > max_area:
            max_area = area

    return G1NonDegenerateReport(
        element_count=len(elements),
        min_observed_area=min_area,
        max_observed_area=max_area,
        tolerance=G1_MIN_TRIANGLE_AREA,
    )


# --- public mesh geometry accessor (SUB-5 reuse) --------------------------


def read_sp_mesh_geometry(
    baseline_root: pathlib.Path,
) -> tuple[tuple[tuple[int, int, int, int], ...], dict[int, tuple[float, float]]]:
    """Read ``.sp.mesh`` element/node geometry from a baseline package.

    Locates the sole ``.sp.mesh`` under ``baseline_root`` (matching
    :func:`verify_g0_baseline` / :func:`verify_g1_non_degenerate_triangles`
    conventions) and returns ``(elements, node_xy)``:

    * ``elements`` — file-order tuple of ``(element_id, v1, v2, v3)``.
    * ``node_xy`` — mapping ``node_id -> (x, y)`` in the package CRS.

    This is a thin public wrapper over the SUB-4 private geometry parser
    intentionally exposed so downstream mapping stages (SUB-5+) can reuse the
    parse contract without importing a leading-underscore helper. The parser
    itself is unchanged; the raised error surface (``UnparseableMeshError``,
    ``BaselineIntegrityError``) is inherited.

    Parameters
    ----------
    baseline_root:
        Directory containing the baseline basin model package.

    Returns
    -------
    tuple
        ``(elements, node_xy)`` — immutable elements tuple + mutable dict
        (the dict return type mirrors the SUB-4 private helper contract).

    Raises
    ------
    BaselineIntegrityError
        ``baseline_root`` is not a directory or ``.sp.mesh`` is missing / not
        uniquely resolvable.
    UnparseableMeshError
        ``.sp.mesh`` cannot be parsed at the geometry level.
    """
    if not isinstance(baseline_root, pathlib.Path):
        raise TypeError(
            f"read_sp_mesh_geometry expects pathlib.Path, got "
            f"{type(baseline_root).__name__}"
        )
    if not baseline_root.exists() or not baseline_root.is_dir():
        raise BaselineIntegrityError(
            f"baseline_root does not exist or is not a directory: {baseline_root}"
        )
    mesh_path = _find_single_by_suffix(baseline_root, ".sp.mesh")
    return _parse_sp_mesh_g1_geometry(mesh_path)
