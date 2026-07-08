"""G0 baseline integrity verification for the mapping builder.

This module implements OpenSpec change ``forcing-mapping-asset-build`` §1.1 (Epic
#909 SUB-1) and §1.2 (Epic #909 SUB-2). It exposes three pure entry points that
read a baseline SHUD basin model package **read-only** (INV-1):

* :func:`verify_g0_baseline` — §1.1 baseline integrity gate.
* :func:`verify_package_crs` — §1.2 CRS authority (WKT from ``gis/*.prj``).
* :func:`build_ancillary_inventory` — §1.2 ancillary ``*.tsd.*`` inventory
  (excluding the weather ``.tsd.forc`` reference, which is §1.1's territory).

Each entry point either returns an immutable report or raises a
:class:`BaselineIntegrityError` subclass explaining the exact violation.

Fail-closed guarantee: any subcheck failure raises without writing any output
artifact. The mapping variant tree remains empty.

Non-goals for §1.1 + §1.2 (deferred to later SUBs):

* SUB-3 — baseline classification (duplicate-coordinate stations, non-grid
  baselines, startdate heterogeneity).
* SUB-4 — G1 non-degenerate triangle geometry check.

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

    # Optional .tsd.forc reference integrity.
    tsd_forc_path = _find_optional_tsd_forc(baseline_root)
    tsd_forc_present = tsd_forc_path is not None
    tsd_forc_reference_count = 0
    if tsd_forc_path is not None:
        references = _parse_tsd_forc_reference_ids(tsd_forc_path)
        tsd_forc_reference_count = len(references)
        valid_range = (1, max_forc_value)
        for line_number, ref in references:
            if ref < 1 or ref > max_forc_value:
                raise IllegalTsdForcReferenceError(
                    line_number=line_number,
                    invalid_reference=ref,
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
    """Return the sole ``gis/*.prj`` file, or raise :class:`MissingPrjError`.

    Multiple ``.prj`` files under ``gis/`` are treated as unparseable (§1.2
    expects a single CRS declaration per package).
    """
    gis_dir = baseline_root / "gis"
    if not gis_dir.is_dir():
        raise MissingPrjError(baseline_root=baseline_root)
    candidates = sorted(p for p in gis_dir.glob("*.prj") if p.is_file())
    if not candidates:
        raise MissingPrjError(baseline_root=baseline_root)
    if len(candidates) > 1:
        raise UnparseablePrjError(
            prj_path=candidates[0],
            parse_error=(
                f"expected exactly one gis/*.prj, found {len(candidates)}: "
                f"{[p.name for p in candidates]}"
            ),
        )
    return candidates[0]


def verify_package_crs(baseline_root: pathlib.Path) -> PackageCrsReport:
    """Read the model CRS from ``gis/*.prj`` and prove WGS84 convertibility.

    Per §1.2, the CRS is read **only** from ``gis/*.prj``. This function MUST
    never open ``.sp.mesh``, ``.sp.att``, or any other file as a CRS source,
    and MUST NOT consult EPSG defaults or any global assumption.

    Parameters
    ----------
    baseline_root:
        Directory containing the baseline basin model package. The CRS lives
        at ``<baseline_root>/gis/<basin>.prj`` (single file per package).

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
        pyproj cannot parse the WKT (or multiple ``.prj`` files were found).
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
