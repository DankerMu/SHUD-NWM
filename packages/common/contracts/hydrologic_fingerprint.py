"""``hydrologic_core_fingerprint`` contract: G10 fingerprint + §3.3/§3.4 file gates.

Moved verbatim from :mod:`workers.mapping_builder.rewrite` (entropy-burndown 1.2):
the AST closure of :func:`verify_hydrologic_core_fingerprint_equal` plus
:data:`STATE_COMPATIBILITY_SURFACES` (derived from the G10 labels and consumed by
:mod:`packages.common.state_clone`). The closure forces the whole ``.sp.att``
parser chain (``compute -> _canonicalize_sp_att_non_forc -> _parse_sp_att_file``)
and the file-hash chain (``_compute_file_category_hash -> _resolve_and_sha256 ->
_sha256_file``) to live here too. ``rewrite.py`` re-exports these names and its
remaining ``SpAttRewriteError`` subclasses inherit from the base defined here, so
callers see the same objects through either path. This module imports nothing
above ``packages/common``.
"""

from __future__ import annotations

import hashlib
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# --- exception family ------------------------------------------------------


class SpAttRewriteError(Exception):
    """Base class for §3.1 + §3.2 ``.sp.att`` rewrite failures.

    Distinct root class (not a subclass of
    :class:`workers.mapping_builder.integrity.BaselineIntegrityError` or
    :class:`workers.mapping_builder.algorithm.MappingAlgorithmError`).
    Callers that catch G4 rewrite failures MUST NOT accidentally absorb
    G0/G1 baseline errors or G2/G3 algorithm errors with the same ``except``
    clause; the mapping builder's fail-closed guarantee is meaningful only
    when callers can tell the three families apart.
    """


class MissingPackageFileError(SpAttRewriteError):
    """A category file declared for §3.3/§3.4 is missing from a package root.

    Per §3.3 the variant MUST contain the same hydrologic core files as the
    baseline. If a declared relative path is missing under either package
    root, the checksum comparison cannot proceed — fail closed with an
    actionable message identifying the missing file and which side it's
    missing from.

    ``missing_side`` vocabulary:

    * ``"baseline"`` / ``"variant"`` — raised by the paired equality gates
      (:func:`verify_non_sp_att_checksums_equal`,
      :func:`verify_hydrologic_core_fingerprint_equal`), which know which
      of the two package roots owns the missing file.
    * ``"package"`` — the default when :func:`compute_hydrologic_core_fingerprint`
      is called standalone (no baseline/variant pairing context). Callers
      can override this by passing an explicit label to
      :func:`compute_hydrologic_core_fingerprint`'s ``side_label`` kwarg.
    """

    def __init__(
        self,
        *,
        category: str,
        relative_path: str,
        missing_side: str,  # "baseline" | "variant" | "package"
        package_root: pathlib.Path,
    ) -> None:
        super().__init__(
            f"non-sp.att category={category!r} declared file "
            f"{relative_path!r} missing from {missing_side} package root "
            f"{package_root} (G4 asset delta violation — the variant MUST "
            "carry the same hydrologic core files as the baseline)"
        )
        self.category = category
        self.relative_path = relative_path
        self.missing_side = missing_side
        self.package_root = package_root


class UnknownCategoryError(SpAttRewriteError):
    """A supplied category label is not in :data:`NON_SP_ATT_CATEGORIES`.

    Guards against typos and out-of-scope categories: the §3.3 gate covers
    exactly the seven declared categories, and the §3.4 fingerprint covers
    those seven plus three (``state_schema``, ``solver_config``,
    ``sp_att_non_forc``). Any other label indicates a caller bug and is
    refused before file bytes are read, so downstream SUB-13 evidence never
    binds to an off-schema fingerprint.
    """

    def __init__(
        self,
        *,
        supplied_category: str,
        allowed_categories: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"unknown category {supplied_category!r}; expected one of "
            f"{list(allowed_categories)!r}"
        )
        self.supplied_category = supplied_category
        self.allowed_categories = allowed_categories


class MissingCategoryError(SpAttRewriteError):
    """A required category is absent from the caller-supplied mapping.

    :func:`verify_non_sp_att_checksums_equal` requires the full set of
    :data:`NON_SP_ATT_CATEGORIES`; :func:`compute_hydrologic_core_fingerprint`
    similarly requires the full seven for the file-based portion of the
    fingerprint. Missing coverage is a caller bug that would silently
    exclude an in-scope surface from the G4 proof — refuse loudly.
    """

    def __init__(
        self,
        *,
        missing_categories: tuple[str, ...],
        required_categories: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"missing category coverage: {list(missing_categories)!r}; "
            f"required={list(required_categories)!r}"
        )
        self.missing_categories = missing_categories
        self.required_categories = required_categories


class HydrologicCoreFingerprintMismatchError(SpAttRewriteError):
    """Baseline and variant hydrologic core fingerprints differ.

    Per spec §"hydrologic_core_fingerprint equals the baseline's" (§3.4)
    and docs §Gate G10: the ten-surface fingerprint (mesh topology,
    river/lake topology, ``.sp.att`` non-``FORC`` fields, soil/geol/land,
    calibration, state vector schema, solver-relevant configuration) MUST
    be byte-identical between baseline and variant. Any drift is a G4
    blocker with no variant asset written.

    Both fingerprint attributes are supplied so a downstream evidence
    bundler (SUB-13) can record the two SHA-256 digests verbatim and, via
    :attr:`baseline_covered_paths` / :attr:`variant_covered_paths`, prove
    the mismatch was not caused by a coverage discrepancy but by a bytes
    drift in one of the covered surfaces. The attribute names mirror the
    :class:`HydrologicCoreFingerprint` ``covered_paths`` field so the
    exception carries the raw ``covered_paths`` tuples for both sides.
    """

    def __init__(
        self,
        *,
        baseline_fingerprint_hash: str,
        variant_fingerprint_hash: str,
        baseline_covered_paths: tuple[str, ...],
        variant_covered_paths: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"hydrologic_core_fingerprint mismatch: "
            f"baseline={baseline_fingerprint_hash!r} "
            f"variant={variant_fingerprint_hash!r} "
            f"(baseline_paths={list(baseline_covered_paths)}, "
            f"variant_paths={list(variant_covered_paths)}) "
            "(G4 asset delta / docs §G10 violation)"
        )
        self.baseline_fingerprint_hash = baseline_fingerprint_hash
        self.variant_fingerprint_hash = variant_fingerprint_hash
        self.baseline_covered_paths = baseline_covered_paths
        self.variant_covered_paths = variant_covered_paths


# --- category constants (§3.3 + §3.4 coverage sets) ----------------------

# The 7 non-``.sp.att`` hydrologic core file categories covered by the G4
# checksum-equality gate (§3.3). Alphabetized so the coverage set is a
# stable, review-friendly tuple.
NON_SP_ATT_CATEGORIES: tuple[str, ...] = (
    "calibration",
    "geol",
    "lake",
    "land",
    "mesh",
    "river",
    "soil",
)

# The 10 covered surfaces of the ``hydrologic_core_fingerprint`` (§3.4 +
# docs §Gate G10): the 7 file categories above plus ``.sp.att`` non-``FORC``
# fields, state vector schema, and solver-relevant configuration.
# Alphabetized for deterministic domain-separated hashing.
HYDROLOGIC_CORE_FINGERPRINT_LABELS: tuple[str, ...] = (
    "calibration",
    "geol",
    "lake",
    "land",
    "mesh",
    "river",
    "soil",
    "solver_config",
    "sp_att_non_forc",
    "state_schema",
)

# The 8 covered surfaces of the ``state_compatibility_fingerprint`` (change
# ``recalibration-state-carryover`` D1): the ten G10 labels minus
# ``calibration`` and ``solver_config``.
#
# G10's ten surfaces answer "is this the SAME model package?". State
# transferability is a narrower question: changing a calibration parameter
# (``cfg.calib`` / ``CALIB/*``) or a solver-relevant configuration value
# (``cfg.para``) changes how the model steps FORWARD from a state; it does
# not change what the state vector IS, nor the mesh/river/soil/geol/land/lake
# geometry the state lives on, nor the ``.sp.att`` non-``FORC`` fields, nor
# the initial-condition schema (``cfg.ic``).
#
# Restricting the surface set — NOT introducing a second hash format — keeps
# one hashing algorithm. An 8-surface fingerprint is a different value from a
# 10-surface one by construction (a different line set feeds the top-level
# buffer); the two are never compared to each other.
STATE_COMPATIBILITY_SURFACES: tuple[str, ...] = tuple(
    sorted(set(HYDROLOGIC_CORE_FINGERPRINT_LABELS) - {"calibration", "solver_config"})
)


# --- structured output dataclasses ---------------------------------------


@dataclass(frozen=True)
class HydrologicCoreFingerprint:
    """Result of :func:`compute_hydrologic_core_fingerprint` (§3.4 / docs §G10).

    Frozen so downstream evidence (SUB-13 mapping evidence package) can
    record the two fields verbatim — SUB-13 does not recompute the
    fingerprint from its inputs; it stores what the builder produced,
    with the covered_paths tuple providing the audit trail of every
    surface that fed the hash.

    Attributes
    ----------
    hash:
        The SHA-256 hex digest (64 lowercase hex chars) computed over the
        domain-separated concatenation of per-surface entries in
        :data:`HYDROLOGIC_CORE_FINGERPRINT_LABELS` order.
    covered_paths:
        Immutable tuple of ``f"{label}:{descriptor}"`` strings, one per
        entry that fed the hash. For file categories the descriptor is
        the relative path (or a semicolon-joined list of sorted relative
        paths for multi-file categories). For ``state_schema`` and
        ``solver_config`` the descriptor is ``"<bytes>"``. For
        ``sp_att_non_forc`` the descriptor is the ``.sp.att`` relative
        path. Sorted alphabetically for determinism, so two runs on the
        same inputs produce a byte-identical dataclass.
    """

    hash: str
    covered_paths: tuple[str, ...]


# --- internal parser ------------------------------------------------------


@dataclass(frozen=True)
class _ParsedSpAtt:
    """Internal parse-level record of a ``.sp.att`` file.

    Preserves line terminators so :func:`copy_and_rewrite_sp_att_forc` can
    reconstruct byte-identical non-``FORC`` content when writing the variant.
    """

    schema: tuple[str, ...]
    forc_col_index: int
    n_rows: int
    header_line: str  # line 1 content (no line terminator)
    column_header_line: str  # line 2 content (no line terminator)
    header_terminator: str  # line 1 terminator, preserved verbatim
    column_header_terminator: str  # line 2 terminator, preserved verbatim
    raw_data_lines: tuple[str, ...]  # data rows with their line terminators
    parsed_rows: tuple[tuple[str, ...], ...]  # tokenized rows in file order
    trailing_content: str  # any bytes after the last data row


def _sha256_file(path: pathlib.Path) -> str:
    """Compute SHA-256 hex digest of a file, chunked read (INV-1)."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _read_bytes_and_split_lines(path: pathlib.Path) -> list[str]:
    """Read a file as bytes, decode UTF-8, return keepends-True line list.

    Preserves line terminators so :func:`_parse_sp_att_file` can reconstruct
    the file byte-for-byte when writing the variant.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpAttRewriteError(
            f"{path}: non-utf-8 bytes: {exc}"
        ) from exc
    return text.splitlines(keepends=True)


def _parse_header_counts(header_line: str) -> tuple[int, int]:
    """Parse ``<N_rows>\\t<N_cols>`` header line, tolerating trailing whitespace."""
    tokens = header_line.split()
    if len(tokens) < 2:
        raise SpAttRewriteError(
            f"header line {header_line!r} does not carry <N_rows> <N_cols>"
        )
    try:
        n_rows = int(tokens[0])
        n_cols = int(tokens[1])
    except ValueError as exc:
        raise SpAttRewriteError(
            f"header line {header_line!r} tokens are not integers: {exc}"
        ) from exc
    if n_rows <= 0 or n_cols <= 0:
        raise SpAttRewriteError(
            f"header counts must be positive, got n_rows={n_rows} n_cols={n_cols}"
        )
    return n_rows, n_cols


def _parse_sp_att_file(path: pathlib.Path) -> _ParsedSpAtt:
    """Parse a ``.sp.att`` file into an immutable :class:`_ParsedSpAtt`.

    Follows the SHUD ``.sp.att`` layout documented in
    :mod:`workers.mapping_builder.integrity`:

    * Line 1: ``<N_rows>\\t<N_cols>`` (may carry trailing whitespace).
    * Line 2: column header (first column ``INDEX``, must include ``FORC``).
    * Lines 3..N+2: data rows.

    Preserves raw line bytes so the variant writer can reconstruct
    byte-identical non-``FORC`` content by only surgically replacing the
    ``FORC`` token in each row.
    """
    if not path.exists() or not path.is_file():
        raise SpAttRewriteError(
            f".sp.att does not exist or is not a file: {path}"
        )
    lines_with_terms = _read_bytes_and_split_lines(path)
    if len(lines_with_terms) < 2:
        raise SpAttRewriteError(
            f"{path.name}: too short — needs at least header + column header"
        )

    header_full = lines_with_terms[0]
    column_header_full = lines_with_terms[1]

    header_content = header_full.rstrip("\r\n")
    header_terminator = header_full[len(header_content):]
    column_header_content = column_header_full.rstrip("\r\n")
    column_header_terminator = column_header_full[len(column_header_content):]

    n_rows, n_cols = _parse_header_counts(header_content)

    header_tokens = column_header_content.split()
    if len(header_tokens) < n_cols:
        raise SpAttRewriteError(
            f"{path.name}: column header row has {len(header_tokens)} tokens, "
            f"expected >= {n_cols}"
        )
    schema = tuple(header_tokens[:n_cols])
    schema_upper = tuple(t.upper() for t in schema)
    if schema_upper[0] != "INDEX":
        raise SpAttRewriteError(
            f"{path.name}: expected first column 'INDEX', got {schema[0]!r}"
        )
    try:
        forc_col_index = schema_upper.index("FORC")
    except ValueError as exc:
        raise SpAttRewriteError(
            f"{path.name}: no 'FORC' column found in header {list(schema)!r}"
        ) from exc

    row_start = 2
    row_end = row_start + n_rows
    if len(lines_with_terms) < row_end:
        raise SpAttRewriteError(
            f"{path.name}: expected {n_rows} data rows, only "
            f"{len(lines_with_terms) - row_start} present"
        )

    raw_data_lines: list[str] = []
    parsed_rows: list[tuple[str, ...]] = []
    for row_index in range(row_start, row_end):
        raw_line = lines_with_terms[row_index]
        content = raw_line.rstrip("\r\n")
        if not content.strip():
            raise SpAttRewriteError(
                f"{path.name}: blank data row at line {row_index + 1}"
            )
        tokens = content.split()
        if len(tokens) < len(schema):
            raise SpAttRewriteError(
                f"{path.name}: data row {row_index + 1} has {len(tokens)} "
                f"tokens, expected >= {len(schema)}"
            )
        # Fail-closed on extra tokens per row: the schema declares exactly
        # ``len(schema)`` columns, so any additional non-whitespace token in
        # a data row is a schema violation that would otherwise be silently
        # truncated by ``tokens[: len(schema)]`` below. A drifted schema is
        # a G4 blocker — we cannot claim byte-level non-``FORC`` equality
        # if the parser silently drops trailing columns.
        if len(tokens) > len(schema):
            raise SpAttRewriteError(
                f"{path.name}: data row {row_index + 1} has {len(tokens)} "
                f"tokens, expected exactly {len(schema)} per declared "
                f"schema {list(schema)!r} — extra tokens rejected to "
                "prevent silent schema drift"
            )
        try:
            int(tokens[0])
        except ValueError as exc:
            raise SpAttRewriteError(
                f"{path.name}: data row {row_index + 1} first token "
                f"{tokens[0]!r} is not an integer: {exc}"
            ) from exc
        raw_data_lines.append(raw_line)
        parsed_rows.append(tuple(tokens[: len(schema)]))

    # Fail-closed on extra data rows past ``n_rows``: any additional
    # non-blank line past the declared row count is a structural
    # inconsistency that would otherwise be stashed away in
    # ``trailing_content`` and silently preserved in the variant. The
    # header declares n_rows; the file MUST NOT contain undeclared data.
    for extra_index in range(row_end, len(lines_with_terms)):
        extra_line = lines_with_terms[extra_index]
        if extra_line.rstrip("\r\n").strip():
            raise SpAttRewriteError(
                f"{path.name}: found non-blank content at line "
                f"{extra_index + 1} past declared n_rows={n_rows} "
                "(row_end); extra data rows rejected to prevent silent "
                "row-count drift"
            )

    trailing_content = "".join(lines_with_terms[row_end:])

    return _ParsedSpAtt(
        schema=schema,
        forc_col_index=forc_col_index,
        n_rows=n_rows,
        header_line=header_content,
        column_header_line=column_header_content,
        header_terminator=header_terminator,
        column_header_terminator=column_header_terminator,
        raw_data_lines=tuple(raw_data_lines),
        parsed_rows=tuple(parsed_rows),
        trailing_content=trailing_content,
    )


# --- §3.3 non-.sp.att file-checksum equality gate (G4) --------------------


def _validate_category_files(
    category_files: Mapping[str, Sequence[str]],
    *,
    required_categories: tuple[str, ...],
) -> None:
    """Validate the caller-supplied category-to-relative-paths mapping.

    Raises
    ------
    UnknownCategoryError
        A supplied category is not in ``required_categories``.
    MissingCategoryError
        A required category is absent from ``category_files``.
    SpAttRewriteError
        A category maps to an empty sequence (the gate has no file to hash).
    """
    supplied = set(category_files)
    required = set(required_categories)
    unknown = supplied - required
    if unknown:
        # Deterministic first-unknown reporting: sort to make the failing
        # label reproducible across runs / OS iteration orders.
        raise UnknownCategoryError(
            supplied_category=sorted(unknown)[0],
            allowed_categories=required_categories,
        )
    missing = required - supplied
    if missing:
        raise MissingCategoryError(
            missing_categories=tuple(sorted(missing)),
            required_categories=required_categories,
        )
    # Empty per-category coverage would silently skip the check.
    for label in required_categories:
        if not tuple(category_files[label]):
            raise SpAttRewriteError(
                f"category {label!r} has no declared files; the §3.3/§3.4 "
                "gate requires at least one file per covered category"
            )


def _resolve_and_sha256(
    package_root: pathlib.Path,
    relative_path: str,
    *,
    category: str,
    missing_side: str,
) -> str:
    """Resolve ``relative_path`` under ``package_root`` and return its SHA-256.

    Raises
    ------
    MissingPackageFileError
        The resolved path does not exist or is not a regular file.
    """
    target = package_root / relative_path
    if not target.exists() or not target.is_file():
        raise MissingPackageFileError(
            category=category,
            relative_path=relative_path,
            missing_side=missing_side,
            package_root=package_root,
        )
    return _sha256_file(target)


# --- §3.4 hydrologic_core_fingerprint (G10 coverage) ----------------------


def _sha256_bytes(data: bytes) -> str:
    """SHA-256 hex digest of an in-memory bytes buffer."""
    return hashlib.sha256(data).hexdigest()


def _compute_file_category_hash(
    package_root: pathlib.Path,
    category: str,
    relative_paths: Sequence[str],
    *,
    side_label: str,
) -> str:
    """SHA-256 over ``f"{path}\\t{file_sha256}\\n"`` per file, sorted by path.

    Multi-file categories (e.g. river = river.riv + river.rivseg) fold the
    per-file hashes into a per-category hash before entering the top-level
    fingerprint. Sorted-path order is the domain separator that prevents
    collisions between adjacent files in the same category.

    ``side_label`` propagates to :class:`MissingPackageFileError` so the
    paired equality gate can report ``"baseline"`` / ``"variant"`` while a
    standalone call uses the default ``"package"``.
    """
    per_file_entries: list[str] = []
    for relative_path in sorted(relative_paths):
        file_sha = _resolve_and_sha256(
            package_root,
            relative_path,
            category=category,
            missing_side=side_label,
        )
        per_file_entries.append(f"{relative_path}\t{file_sha}\n")
    joined = "".join(per_file_entries).encode("utf-8")
    return _sha256_bytes(joined)


def _canonicalize_sp_att_non_forc(sp_att_path: pathlib.Path) -> str:
    """Return SHA-256 over the ``.sp.att`` non-``FORC`` columns, canonicalized.

    The ``.sp.att`` file cannot be hashed whole because the FORC column
    changes intentionally between baseline and variant. Instead:

    1. Parse the file with :func:`_parse_sp_att_file` to obtain the schema
       and per-row tokens (already-validated for schema drift).
    2. Serialize each row's non-``FORC`` tokens keyed by ``element_id``,
       sorted by ``element_id`` ascending for determinism.
    3. Prefix the schema (all column names minus ``FORC``, in the schema's
       original order) so a column-name drift (e.g. renaming a non-FORC
       column while preserving its byte payload) cannot silently produce
       the same digest.
    4. SHA-256 the canonicalized string.

    This is the fingerprint contribution of the ``sp_att_non_forc``
    surface. It is symmetric to :func:`verify_non_forc_columns_unchanged`
    (which asserts equality at parse level between baseline and variant),
    but here we produce a compact hash suitable for the top-level
    fingerprint entry.
    """
    parsed = _parse_sp_att_file(sp_att_path)
    non_forc_col_indices = [
        i for i in range(len(parsed.schema)) if i != parsed.forc_col_index
    ]
    non_forc_schema = tuple(parsed.schema[i] for i in non_forc_col_indices)
    lines: list[str] = [
        "schema\t" + "\t".join(non_forc_schema) + "\n",
    ]
    rows_by_id: dict[int, tuple[str, ...]] = {}
    for row in parsed.parsed_rows:
        eid = int(row[0])
        rows_by_id[eid] = row
    for eid in sorted(rows_by_id):
        row = rows_by_id[eid]
        non_forc_tokens = [row[i] for i in non_forc_col_indices]
        lines.append(f"{eid}\t" + "\t".join(non_forc_tokens) + "\n")
    joined = "".join(lines).encode("utf-8")
    return _sha256_bytes(joined)


def _validate_surface_set(surfaces: Sequence[str]) -> tuple[str, ...]:
    """Validate a fingerprint surface set and return it in alphabetical order.

    A surface set MUST be a non-empty, duplicate-free subset of
    :data:`HYDROLOGIC_CORE_FINGERPRINT_LABELS`. An empty set would produce
    the SHA-256 of the empty buffer for every package — a degenerate value
    that would false-pass any equality gate — and a duplicate would silently
    double-weight one surface, so both are refused fail-closed before any
    file bytes are read.

    Returning the alphabetically sorted tuple pins the domain-separation
    order at one place: the caller's argument order can never perturb the
    hash. For the pre-sorted ten-label default this is a no-op, so the
    default fingerprint value is byte-identical to the pre-change one.
    """
    ordered = tuple(surfaces)
    if not ordered:
        raise SpAttRewriteError(
            "surfaces must be a non-empty subset of "
            f"{list(HYDROLOGIC_CORE_FINGERPRINT_LABELS)!r}; got an empty set"
        )
    duplicates = tuple(
        sorted({label for label in ordered if ordered.count(label) > 1})
    )
    if duplicates:
        raise SpAttRewriteError(
            f"surfaces contains duplicate labels {list(duplicates)!r}; each "
            "covered surface must contribute exactly one fingerprint line"
        )
    unknown = tuple(
        sorted(set(ordered) - set(HYDROLOGIC_CORE_FINGERPRINT_LABELS))
    )
    if unknown:
        raise SpAttRewriteError(
            f"surfaces contains unknown labels {list(unknown)!r}; expected a "
            f"subset of {list(HYDROLOGIC_CORE_FINGERPRINT_LABELS)!r}"
        )
    return tuple(sorted(ordered))


def compute_hydrologic_core_fingerprint(
    package_root: pathlib.Path,
    *,
    sp_att_path: pathlib.Path,
    category_files: Mapping[str, Sequence[str]],
    state_schema_bytes: bytes,
    solver_config_bytes: bytes,
    side_label: str = "package",
    surfaces: tuple[str, ...] = HYDROLOGIC_CORE_FINGERPRINT_LABELS,
) -> HydrologicCoreFingerprint:
    """§3.4 / docs §G10 ten-surface ``hydrologic_core_fingerprint`` computation.

    Computes a domain-separated SHA-256 fingerprint over the ten covered
    surfaces enumerated in :data:`HYDROLOGIC_CORE_FINGERPRINT_LABELS`:

    * ``calibration``, ``geol``, ``lake``, ``land``, ``mesh``, ``river``,
      ``soil`` — file categories under ``category_files`` (same shape as
      :func:`verify_non_sp_att_checksums_equal`).
    * ``sp_att_non_forc`` — non-``FORC`` columns of ``sp_att_path``,
      canonicalized by element ID (see :func:`_canonicalize_sp_att_non_forc`).
    * ``state_schema`` — SHA-256 of ``state_schema_bytes`` (pluggable so
      SUB-13 can wire in the concrete state schema bytes when the runtime
      piece lands; per docs §G10 this is a shared platform-level surface).
    * ``solver_config`` — SHA-256 of ``solver_config_bytes`` (same
      rationale as ``state_schema_bytes``).

    Domain separation
    -----------------
    Each surface contributes exactly one line ``f"{label}\\t{hash}\\n"`` to
    the top-level buffer, sorted by ``label`` alphabetically. The final
    fingerprint is the SHA-256 of this UTF-8-encoded buffer. Because each
    entry carries an in-hex sub-hash (not raw bytes) and terminates with
    ``\\n``, adjacent-surface content shifting cannot collide the digest
    (raw-bytes concatenation would be collision-attackable — the docs
    require the fingerprint be a real invariance proof, not a hand-wave).

    The returned :class:`HydrologicCoreFingerprint`'s ``covered_paths``
    field enumerates ``f"{label}:{descriptor}"`` per surface so SUB-13 can
    record verbatim which surfaces fed the hash. Multi-file categories
    join their sorted relative paths with ``";"``; pluggable byte
    surfaces use ``"<bytes>"`` as the descriptor.

    Parameters
    ----------
    package_root:
        Absolute path to a model input package root (baseline or variant).
        MUST be an existing directory.
    sp_att_path:
        Absolute path to the package's ``.sp.att`` file (may be under
        ``package_root`` or a variant path; only its bytes matter here).
        MUST exist and parse cleanly.
    category_files:
        Mapping from each of the seven :data:`NON_SP_ATT_CATEGORIES` to a
        non-empty sequence of relative paths under ``package_root``.
    state_schema_bytes, solver_config_bytes:
        In-memory bytes for the state vector schema and solver-relevant
        configuration surfaces (per docs §G10). Empty bytes ARE legal
        (they hash to the SHA-256 of the empty string) but a caller
        supplying empty bytes on both packages will pass the equality
        gate — the intent is that SUB-13 supplies real bytes.
    side_label:
        Label carried into :class:`MissingPackageFileError`'s
        ``missing_side`` attribute when a declared file is absent under
        ``package_root``. Defaults to ``"package"`` for standalone
        callers; :func:`verify_hydrologic_core_fingerprint_equal` overrides
        this to ``"baseline"`` / ``"variant"`` so paired equality errors
        name the correct side.
    surfaces:
        The surface set the fingerprint covers. Defaults to the full ten
        :data:`HYDROLOGIC_CORE_FINGERPRINT_LABELS` (package identity).
        :data:`STATE_COMPATIBILITY_SURFACES` restricts it to the eight
        state-transferability surfaces (change
        ``recalibration-state-carryover`` D1). MUST be a non-empty,
        duplicate-free subset of the ten labels.

        The hashing algorithm is IDENTICAL for any surface set — one
        ``f"{label}\\t{hash}\\n"`` line per covered surface in alphabetical
        label order, SHA-256 of the joined buffer. Because the line SET
        differs, an 8-surface value is never equal to a 10-surface value
        for the same package; the two are never compared to each other.

        ``required_categories`` for the file-category portion derives from
        this set, so in 8-surface mode ``calibration`` is neither required
        nor accepted in ``category_files`` and ``cfg.calib`` / ``CALIB/*``
        are never enumerated. ``sp_att_path`` is required in both sets.

    Returns
    -------
    HydrologicCoreFingerprint
        Immutable record with the fingerprint SHA-256 hex and the sorted
        covered_paths tuple.

    Raises
    ------
    UnknownCategoryError
        ``category_files`` contains a category not in the file categories
        derived from ``surfaces``.
    MissingCategoryError
        ``category_files`` is missing a required category from the file
        categories derived from ``surfaces``.
    MissingPackageFileError
        A declared relative path is absent from ``package_root``.
        ``missing_side`` reflects ``side_label``.
    SpAttRewriteError
        ``package_root`` is not a directory, ``sp_att_path`` is missing
        or unparseable, a category maps to an empty sequence, or
        ``surfaces`` is empty / contains duplicates / names a label
        outside :data:`HYDROLOGIC_CORE_FINGERPRINT_LABELS`.
    """
    covered_surfaces = _validate_surface_set(surfaces)
    if not package_root.is_dir():
        raise SpAttRewriteError(
            f"package_root {package_root} is not a directory"
        )
    if not sp_att_path.exists() or not sp_att_path.is_file():
        raise SpAttRewriteError(
            f"sp_att_path {sp_att_path} does not exist or is not a file"
        )
    # The file categories in scope are exactly the covered surfaces that name
    # a non-``.sp.att`` file category; in 8-surface mode ``calibration`` drops
    # out of both the required set and the accepted set.
    required_categories = tuple(
        label for label in covered_surfaces if label in NON_SP_ATT_CATEGORIES
    )
    _validate_category_files(
        category_files, required_categories=required_categories
    )
    # Per-surface hashes and descriptors. Descriptors feed covered_paths.
    per_surface_hash: dict[str, str] = {}
    per_surface_descriptor: dict[str, str] = {}
    for category in required_categories:
        relative_paths = tuple(sorted(category_files[category]))
        per_surface_hash[category] = _compute_file_category_hash(
            package_root,
            category,
            relative_paths,
            side_label=side_label,
        )
        per_surface_descriptor[category] = ";".join(relative_paths)
    if "sp_att_non_forc" in covered_surfaces:
        per_surface_hash["sp_att_non_forc"] = _canonicalize_sp_att_non_forc(
            sp_att_path
        )
        per_surface_descriptor["sp_att_non_forc"] = str(sp_att_path.name)
    if "state_schema" in covered_surfaces:
        per_surface_hash["state_schema"] = _sha256_bytes(state_schema_bytes)
        per_surface_descriptor["state_schema"] = "<bytes>"
    if "solver_config" in covered_surfaces:
        per_surface_hash["solver_config"] = _sha256_bytes(solver_config_bytes)
        per_surface_descriptor["solver_config"] = "<bytes>"

    # Sanity: every covered label appears exactly once.
    assert set(per_surface_hash) == set(covered_surfaces)

    # Domain-separated top-level hash. Iterate labels in alphabetical order so
    # the fingerprint is byte-identical across runs and independent of the
    # order the caller happened to list ``surfaces`` in.
    top_lines = [
        f"{label}\t{per_surface_hash[label]}\n" for label in covered_surfaces
    ]
    fingerprint_hash = _sha256_bytes("".join(top_lines).encode("utf-8"))
    covered_paths = tuple(
        sorted(
            f"{label}:{per_surface_descriptor[label]}"
            for label in covered_surfaces
        )
    )
    return HydrologicCoreFingerprint(
        hash=fingerprint_hash, covered_paths=covered_paths
    )


def verify_hydrologic_core_fingerprint_equal(
    baseline_package_root: pathlib.Path,
    variant_package_root: pathlib.Path,
    *,
    baseline_sp_att_path: pathlib.Path,
    variant_sp_att_path: pathlib.Path,
    category_files: Mapping[str, Sequence[str]],
    baseline_state_schema_bytes: bytes,
    variant_state_schema_bytes: bytes,
    baseline_solver_config_bytes: bytes,
    variant_solver_config_bytes: bytes,
    surfaces: tuple[str, ...] = HYDROLOGIC_CORE_FINGERPRINT_LABELS,
) -> HydrologicCoreFingerprint:
    """§3.4 G4 fingerprint-equality gate + shared-fingerprint return.

    Computes :func:`compute_hydrologic_core_fingerprint` on the baseline
    and variant packages, asserts equality, and returns the shared
    fingerprint on pass. Per spec §"hydrologic_core_fingerprint equals the
    baseline's" (§3.4) and docs §Gate G10, drift in any of the ten covered
    surfaces indicates the variant mutated a hydrologic core surface it
    was not supposed to touch, and is a G4 blocker with no variant asset
    written.

    Both packages use the SAME ``category_files`` mapping — the invariant
    is that the SAME relative paths under both roots hash the same. The
    ``state_schema_bytes`` and ``solver_config_bytes`` are per-package
    inputs so callers can inject drift for negative testing (in production
    both callers supply the same platform-level bytes to both fingerprint
    computations).

    Parameters
    ----------
    baseline_package_root, variant_package_root:
        Absolute paths to the baseline and variant model input package
        roots. Both MUST exist and be directories.
    baseline_sp_att_path, variant_sp_att_path:
        Absolute paths to the baseline and variant ``.sp.att`` files.
        Both MUST exist.
    category_files:
        Mapping from each of the seven :data:`NON_SP_ATT_CATEGORIES` to a
        non-empty sequence of relative paths. Same relative paths are
        used for both roots.
    baseline_state_schema_bytes, variant_state_schema_bytes:
        Per-package state vector schema bytes (see
        :func:`compute_hydrologic_core_fingerprint`).
    baseline_solver_config_bytes, variant_solver_config_bytes:
        Per-package solver-relevant configuration bytes (see
        :func:`compute_hydrologic_core_fingerprint`).
    surfaces:
        The surface set both sides are fingerprinted over. Defaults to the
        ten :data:`HYDROLOGIC_CORE_FINGERPRINT_LABELS` (package identity).
        Pass :data:`STATE_COMPATIBILITY_SURFACES` for the eight-surface
        state-transferability gate. Both sides ALWAYS use the same set —
        the returned value is only meaningful relative to that set, so a
        caller must never compare fingerprints computed over different
        sets.

    Returns
    -------
    HydrologicCoreFingerprint
        The shared fingerprint (equal for baseline and variant on pass).
        SUB-13 records this verbatim in the mapping evidence.

    Raises
    ------
    HydrologicCoreFingerprintMismatchError
        Baseline and variant fingerprints differ (G4 blocker per §3.4 /
        docs §G10).
    UnknownCategoryError, MissingCategoryError, SpAttRewriteError:
        Propagated from the underlying
        :func:`compute_hydrologic_core_fingerprint` invocations.
    MissingPackageFileError
        A declared relative path is absent from either package root.
        ``missing_side`` is ``"baseline"`` when the baseline root is
        missing the file and ``"variant"`` when the variant root is
        missing it — the paired gate overrides the standalone
        ``"package"`` default so evidence trails name the correct side.
    """
    baseline_fp = compute_hydrologic_core_fingerprint(
        baseline_package_root,
        sp_att_path=baseline_sp_att_path,
        category_files=category_files,
        state_schema_bytes=baseline_state_schema_bytes,
        solver_config_bytes=baseline_solver_config_bytes,
        side_label="baseline",
        surfaces=surfaces,
    )
    variant_fp = compute_hydrologic_core_fingerprint(
        variant_package_root,
        sp_att_path=variant_sp_att_path,
        category_files=category_files,
        state_schema_bytes=variant_state_schema_bytes,
        solver_config_bytes=variant_solver_config_bytes,
        side_label="variant",
        surfaces=surfaces,
    )
    if baseline_fp.hash != variant_fp.hash:
        raise HydrologicCoreFingerprintMismatchError(
            baseline_fingerprint_hash=baseline_fp.hash,
            variant_fingerprint_hash=variant_fp.hash,
            baseline_covered_paths=baseline_fp.covered_paths,
            variant_covered_paths=variant_fp.covered_paths,
        )
    # Both fingerprints match — return one (they are byte-identical).
    return baseline_fp
