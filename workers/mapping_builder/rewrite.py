"""§3.1 + §3.2 ``.sp.att`` FORC rewrite + G4 non-``FORC``-unchanged proof.

This module implements OpenSpec change ``forcing-mapping-asset-build`` §3.1 and
§3.2 (Epic #909 SUB-8). It exposes fail-closed primitives that copy the
baseline ``.sp.att`` into a variant package, update every element's ``FORC``
value **by element ID** via the ownership + ``shud_forcing_index`` produced by
``element-grid-ownership-mapping``, and prove that no non-``FORC`` byte changes
in the process.

Public entry points
-------------------
* :func:`copy_and_rewrite_sp_att_forc` — §3.1 orchestrator.
    1. Pre-SHA-256 the baseline ``.sp.att`` (INV-1 anchor).
    2. Parse the baseline row-by-row (schema + tokens).
    3. Validate the ``ownership`` sequence and ``shud_forcing_index`` mapping.
    4. Build new rows by looking up
       ``element_id -> ownership[element_id].grid_cell_id ->
       shud_forcing_index[grid_cell_id]``. Association is by element ID
       (INDEX column), never by row order.
    5. Prove non-``FORC`` columns unchanged in-memory (fail-closed BEFORE any
       variant byte hits disk).
    6. Emit a parse-level semantic diff artifact (FORC deltas only, keyed
       by element ID, sorted ascending).
    7. Serialize the new content atomically to a temp path, compute the
       variant SHA-256, then ``os.replace`` into ``variant_att_path``.
    8. Post-SHA-256 the baseline; raise
       :class:`BaselineImmutabilityViolationError` if it differs from the
       pre-SHA-256 (INV-1 hard block). On raise the variant file is removed.
    9. Return :class:`SpAttRewriteReport` carrying old/new SHA-256, sizes,
       semantic diff, and used-cell count.
* :func:`prove_non_forc_columns_unchanged` — G4 standalone gate: parses
  baseline + variant and asserts equal schema, row count, element-ID set,
  and byte + semantic equality of all non-``FORC`` column tokens keyed by
  element ID.
* :func:`emit_semantic_diff` — parse-level FORC-only diff artifact from
  two sequences of :class:`SpAttForcRow`; deterministic ordering by
  ``element_id`` ascending for byte-identical reproducibility.
* :func:`record_sp_att_checksums` — SHA-256 + size record for both files.
* :func:`parse_sp_att_forc_rows` — helper that reads a ``.sp.att`` and
  yields :class:`SpAttForcRow` records (element_id, FORC) so callers can
  feed :func:`emit_semantic_diff` directly from disk.

Invariants
----------
* INV-1 (baseline read-only): ``baseline_att_path`` is opened read-only
  throughout. Pre + post SHA-256 are computed and compared; any drift
  raises :class:`BaselineImmutabilityViolationError`.
* INV-2 (variant path distinct from baseline): ``variant_att_path`` MUST
  resolve to a path different from ``baseline_att_path``. If callers pass
  the same path (via symlink, ``..``, or literal equality) the function
  refuses loudly rather than overwriting the baseline.

Exception family
----------------
:class:`SpAttRewriteError` is a distinct root — *not* a subclass of
:class:`workers.mapping_builder.integrity.BaselineIntegrityError` (G0/G1)
or :class:`workers.mapping_builder.algorithm.MappingAlgorithmError`
(G2/G3). G4 failures come from a different oracle (baseline file bytes
plus ownership/index consistency) than G0/G1 (baseline package integrity)
or G2/G3 (grid registry + WGS84 coverage). Keeping the roots distinct
lets callers differentiate the three families with dedicated ``except``
clauses.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from workers.mapping_builder.algorithm import ElementOwnership

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


class BaselineImmutabilityViolationError(SpAttRewriteError):
    """Baseline ``.sp.att`` bytes changed between pre and post SHA-256.

    INV-1 hard block: the baseline ``.sp.att`` file MUST be treated as
    read-only. A drift between the pre-check SHA-256 (taken before the
    rewrite pipeline runs) and the post-check SHA-256 (taken after the
    variant is atomically written) indicates either an implementation bug
    that writes back to the baseline path, a race condition with an
    external mutator, or a compromised INV-2 check. Any of these is a G4
    blocker; the variant file is unlinked before this exception raises.
    """

    def __init__(
        self,
        baseline_path: pathlib.Path,
        pre_sha256: str,
        post_sha256: str,
    ) -> None:
        super().__init__(
            f"baseline {baseline_path} mutated during rewrite "
            f"(INV-1 violation): pre_sha256={pre_sha256!r} "
            f"post_sha256={post_sha256!r}"
        )
        self.baseline_path = baseline_path
        self.pre_sha256 = pre_sha256
        self.post_sha256 = post_sha256


class ForcOutOfRangeError(SpAttRewriteError):
    """A rewritten ``FORC`` value is outside ``1..used_cell_count``.

    Per spec §"Every rewritten `FORC` value is an integer in `1..N`"
    (§3.1): the mapping builder MUST fail closed before writing the
    variant when any ``shud_forcing_index`` value falls outside the legal
    binding domain. ``element_id`` and ``grid_cell_id`` are recorded when
    known (per-element path); the pre-validation path leaves ``element_id``
    as ``None`` because the offending value has no element yet.
    """

    def __init__(
        self,
        *,
        new_forc: int,
        valid_range: tuple[int, int],
        element_id: int | None = None,
        grid_cell_id: str | None = None,
    ) -> None:
        parts: list[str] = []
        if element_id is not None:
            parts.append(f"element_id={element_id}")
        if grid_cell_id is not None:
            parts.append(f"grid_cell_id={grid_cell_id!r}")
        parts.append(f"new FORC={new_forc}")
        parts.append(f"out of valid range {valid_range}")
        super().__init__(" ".join(parts))
        self.new_forc = new_forc
        self.valid_range = valid_range
        self.element_id = element_id
        self.grid_cell_id = grid_cell_id


class ForcNonIntegerError(SpAttRewriteError):
    """A ``shud_forcing_index`` value is not an integer.

    Per spec §"Every rewritten `FORC` value is an integer" (§3.1): the
    ``shud_forcing_index`` MUST map ``grid_cell_id -> int``. Float, str,
    None, or bool values are a caller bug — the canonical constructor is
    :func:`workers.mapping_builder.assign_shud_forcing_index`, which
    guarantees int values. Bool is rejected explicitly because ``bool`` is
    a subclass of ``int`` in Python and would otherwise silently pass.
    """

    def __init__(
        self,
        *,
        grid_cell_id: str,
        invalid_value: Any,
    ) -> None:
        super().__init__(
            f"shud_forcing_index[{grid_cell_id!r}]={invalid_value!r} "
            f"(type={type(invalid_value).__name__}) is not an integer"
        )
        self.grid_cell_id = grid_cell_id
        self.invalid_value = invalid_value


class ForcUnmappedError(SpAttRewriteError):
    """A baseline element_id has no ownership entry, or its cell has no index.

    Per spec §"Every baseline element_id is mapped" (§3.1): every element
    row in the baseline ``.sp.att`` MUST have a corresponding ownership
    record whose ``grid_cell_id`` is present in ``shud_forcing_index``.
    Missing ownership means the mapping algorithm silently dropped the
    element; missing index means the algorithm's ownership + index outputs
    are inconsistent. Either is a G4 blocker.
    """

    def __init__(
        self,
        *,
        element_id: int,
        grid_cell_id: str | None = None,
        detail: str = "no ownership entry for baseline element_id",
    ) -> None:
        if grid_cell_id is None:
            super().__init__(f"element_id={element_id}: {detail}")
        else:
            super().__init__(
                f"element_id={element_id} grid_cell_id={grid_cell_id!r}: "
                f"{detail}"
            )
        self.element_id = element_id
        self.grid_cell_id = grid_cell_id
        self.detail = detail


class ForcMultisetMismatchError(SpAttRewriteError):
    """The multiset of ``shud_forcing_index`` values is not ``1..used_cell_count``.

    Per spec §"The multiset of rewritten `FORC` values equals the ownership
    table's mapped `shud_forcing_index` list" (§3.1): the
    ``shud_forcing_index`` values MUST be exactly ``{1, 2, ..., N}`` where
    ``N == used_cell_count``. Duplicates, gaps, or missing values are a G4
    blocker even when every individual value is in range (a duplicate
    binding would silently map two cells to the same forcing index).
    """

    def __init__(
        self,
        *,
        expected_values: tuple[int, ...],
        observed_values: tuple[int, ...],
    ) -> None:
        super().__init__(
            f"shud_forcing_index values mismatch: expected sorted "
            f"{list(expected_values)!r}, observed sorted "
            f"{list(observed_values)!r}"
        )
        self.expected_values = expected_values
        self.observed_values = observed_values


class NonForcColumnChangedError(SpAttRewriteError):
    """A non-``FORC`` column token differs between baseline and variant.

    Per spec §"For all columns except FORC, old_att equals new_att at parse
    level (G4 proof)" (§3.2): the G4 non-``FORC``-unchanged proof compares
    every non-``FORC`` column token for every element (keyed by element ID,
    not row position). Any inequality is a G4 blocker; the variant is not
    written by :func:`copy_and_rewrite_sp_att_forc` when the in-memory
    proof detects a violation.
    """

    def __init__(
        self,
        *,
        element_id: int,
        column_name: str,
        baseline_value: str,
        variant_value: str,
    ) -> None:
        super().__init__(
            f"element_id={element_id} non-FORC column {column_name!r} "
            f"changed: baseline={baseline_value!r} variant={variant_value!r} "
            "(G4 non-FORC-unchanged proof violation)"
        )
        self.element_id = element_id
        self.column_name = column_name
        self.baseline_value = baseline_value
        self.variant_value = variant_value


class RowCountMismatchError(SpAttRewriteError):
    """Baseline and variant ``.sp.att`` have different row counts."""

    def __init__(
        self,
        *,
        baseline_count: int,
        variant_count: int,
    ) -> None:
        super().__init__(
            f"row count mismatch: baseline={baseline_count} "
            f"variant={variant_count} (G4 proof violation)"
        )
        self.baseline_count = baseline_count
        self.variant_count = variant_count


class ElementIdSetMismatchError(SpAttRewriteError):
    """Baseline and variant element-ID sets differ."""

    def __init__(
        self,
        *,
        baseline_only: tuple[int, ...],
        variant_only: tuple[int, ...],
    ) -> None:
        super().__init__(
            f"element_id sets differ: baseline_only={list(baseline_only)} "
            f"variant_only={list(variant_only)} (G4 proof violation)"
        )
        self.baseline_only = baseline_only
        self.variant_only = variant_only


class SchemaMismatchError(SpAttRewriteError):
    """Baseline and variant schemas (column names) differ."""

    def __init__(
        self,
        *,
        baseline_schema: tuple[str, ...],
        variant_schema: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"schema mismatch: baseline={list(baseline_schema)} "
            f"variant={list(variant_schema)} (G4 proof violation)"
        )
        self.baseline_schema = baseline_schema
        self.variant_schema = variant_schema


# --- structured output dataclasses ---------------------------------------


@dataclass(frozen=True)
class SpAttChecksums:
    """SHA-256 hex + size (bytes) record for baseline + variant ``.sp.att``.

    All fields are immutable so downstream evidence can bind the record
    byte-for-byte. Sizes are in bytes as reported by ``os.stat``. The
    two SHA-256 hex strings are 64 lowercase hex characters each.
    """

    baseline_sha256: str
    variant_sha256: str
    baseline_size: int
    variant_size: int


@dataclass(frozen=True)
class SemanticDiffEntry:
    """One ``FORC`` delta: ``element_id`` + old FORC + new FORC.

    Frozen so downstream evidence can bind the row byte-for-byte. Only
    rows whose ``FORC`` actually changed appear in a :class:`SemanticDiff`;
    rows where ``old_forc == new_forc`` are omitted (the diff artifact
    records only the deltas, per spec §"only FORC changes").
    """

    element_id: int
    old_forc: int
    new_forc: int


@dataclass(frozen=True)
class SemanticDiff:
    """Parse-level ``FORC``-only diff artifact.

    ``entries`` is sorted by ``element_id`` ascending for byte-identical
    reproducibility across runs (spec §7 determinism requirement). Empty
    ``entries`` is legal — it means the rewrite made no ``FORC`` changes
    (e.g. a re-run of an already-applied mapping).
    """

    entries: tuple[SemanticDiffEntry, ...]


@dataclass(frozen=True)
class SpAttForcRow:
    """One ``.sp.att`` row's ``element_id`` + ``FORC`` value.

    Input type for :func:`emit_semantic_diff`. Callers can construct a
    tuple of these directly (e.g. from an in-memory model) or via
    :func:`parse_sp_att_forc_rows` (from disk).
    """

    element_id: int
    forc: int


@dataclass(frozen=True)
class SpAttRewriteReport:
    """Top-level return of :func:`copy_and_rewrite_sp_att_forc`.

    Frozen so downstream evidence (SUB-13 mapping evidence package) can
    bind the record byte-for-byte to the variant asset checksum.
    """

    checksums: SpAttChecksums
    semantic_diff: SemanticDiff
    rewritten_row_count: int
    used_cell_count: int


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
        try:
            int(tokens[0])
        except ValueError as exc:
            raise SpAttRewriteError(
                f"{path.name}: data row {row_index + 1} first token "
                f"{tokens[0]!r} is not an integer: {exc}"
            ) from exc
        raw_data_lines.append(raw_line)
        parsed_rows.append(tuple(tokens[: len(schema)]))

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


def _replace_forc_token_in_row(
    raw_line: str,
    forc_col_index: int,
    new_forc: int,
) -> str:
    """Return ``raw_line`` with the ``FORC`` token surgically replaced.

    Uses :func:`re.findall` with ``r'\\S+|\\s+'`` to yield alternating token
    and whitespace runs, then replaces only the token at position
    ``forc_col_index`` (0-based among tokens). Every other token AND every
    whitespace run (including the trailing line terminator) is preserved
    byte-for-byte. This is the mechanism that guarantees non-``FORC`` column
    bytes stay identical between baseline and variant.
    """
    parts = re.findall(r'\S+|\s+', raw_line)
    token_indices = [i for i, p in enumerate(parts) if not p.isspace()]
    if forc_col_index >= len(token_indices):
        raise SpAttRewriteError(
            f"row {raw_line!r} has {len(token_indices)} tokens, "
            f"cannot replace FORC at index {forc_col_index}"
        )
    parts[token_indices[forc_col_index]] = str(new_forc)
    return "".join(parts)


# --- public entry points --------------------------------------------------


def record_sp_att_checksums(
    baseline_att_path: pathlib.Path,
    variant_att_path: pathlib.Path,
) -> SpAttChecksums:
    """Record SHA-256 hex + size (bytes) for baseline + variant ``.sp.att``.

    Reads both files read-only (INV-1). Neither path is modified.

    Parameters
    ----------
    baseline_att_path, variant_att_path:
        Paths to the baseline and variant ``.sp.att`` files. Both MUST exist.

    Returns
    -------
    SpAttChecksums
        Immutable record with baseline_sha256, variant_sha256,
        baseline_size, variant_size.

    Raises
    ------
    SpAttRewriteError
        Either path does not exist or is not a regular file.
    """
    if not baseline_att_path.exists() or not baseline_att_path.is_file():
        raise SpAttRewriteError(
            f"baseline .sp.att does not exist or is not a file: {baseline_att_path}"
        )
    if not variant_att_path.exists() or not variant_att_path.is_file():
        raise SpAttRewriteError(
            f"variant .sp.att does not exist or is not a file: {variant_att_path}"
        )
    return SpAttChecksums(
        baseline_sha256=_sha256_file(baseline_att_path),
        variant_sha256=_sha256_file(variant_att_path),
        baseline_size=baseline_att_path.stat().st_size,
        variant_size=variant_att_path.stat().st_size,
    )


def parse_sp_att_forc_rows(path: pathlib.Path) -> tuple[SpAttForcRow, ...]:
    """Parse a ``.sp.att`` file and return ``(element_id, forc)`` rows.

    Rows are returned in file order — the caller may reorder them if
    needed. Useful for feeding :func:`emit_semantic_diff` directly from
    disk.
    """
    parsed = _parse_sp_att_file(path)
    rows: list[SpAttForcRow] = []
    for token_row in parsed.parsed_rows:
        try:
            element_id = int(token_row[0])
            forc = int(token_row[parsed.forc_col_index])
        except (ValueError, IndexError) as exc:
            raise SpAttRewriteError(
                f"{path.name}: cannot parse element_id/FORC from row "
                f"{token_row!r}: {exc}"
            ) from exc
        rows.append(SpAttForcRow(element_id=element_id, forc=forc))
    return tuple(rows)


def emit_semantic_diff(
    baseline_rows: Sequence[SpAttForcRow],
    variant_rows: Sequence[SpAttForcRow],
) -> SemanticDiff:
    """Emit a parse-level semantic diff artifact showing only ``FORC`` changes.

    Per spec §"A parse-level semantic diff artifact is produced showing
    only FORC changes" (§3.2): the diff MUST contain only
    ``element_id`` + ``old_forc`` + ``new_forc`` for each row where the
    ``FORC`` value changed. No whitespace, column-header, or other noise.

    Entries are sorted by ``element_id`` ascending for byte-identical
    reproducibility (spec §7 determinism).

    Parameters
    ----------
    baseline_rows, variant_rows:
        Sequences of :class:`SpAttForcRow`. Must have equal element-ID
        sets; otherwise raises :class:`ElementIdSetMismatchError`.

    Returns
    -------
    SemanticDiff
        Immutable diff artifact with entries sorted by ``element_id``
        ascending. Empty entries tuple is legal — it means no ``FORC``
        changed (e.g. a re-run of an already-applied mapping).

    Raises
    ------
    ElementIdSetMismatchError
        ``baseline_rows`` and ``variant_rows`` have different element-ID
        sets — the diff is undefined in that case.
    SpAttRewriteError
        Either sequence contains a duplicate ``element_id`` (a caller bug
        — ``.sp.att`` guarantees unique element IDs).
    """
    baseline_by_id: dict[int, int] = {}
    for row in baseline_rows:
        if row.element_id in baseline_by_id:
            raise SpAttRewriteError(
                f"baseline_rows contains duplicate element_id={row.element_id}"
            )
        baseline_by_id[row.element_id] = row.forc
    variant_by_id: dict[int, int] = {}
    for row in variant_rows:
        if row.element_id in variant_by_id:
            raise SpAttRewriteError(
                f"variant_rows contains duplicate element_id={row.element_id}"
            )
        variant_by_id[row.element_id] = row.forc

    baseline_ids = set(baseline_by_id)
    variant_ids = set(variant_by_id)
    if baseline_ids != variant_ids:
        raise ElementIdSetMismatchError(
            baseline_only=tuple(sorted(baseline_ids - variant_ids)),
            variant_only=tuple(sorted(variant_ids - baseline_ids)),
        )
    entries: list[SemanticDiffEntry] = []
    for element_id in sorted(baseline_by_id):
        old = baseline_by_id[element_id]
        new = variant_by_id[element_id]
        if old != new:
            entries.append(
                SemanticDiffEntry(
                    element_id=element_id,
                    old_forc=old,
                    new_forc=new,
                )
            )
    return SemanticDiff(entries=tuple(entries))


def prove_non_forc_columns_unchanged(
    baseline_att_path: pathlib.Path,
    variant_att_path: pathlib.Path,
) -> None:
    """Prove baseline and variant ``.sp.att`` differ only in ``FORC``.

    G4 non-``FORC``-unchanged proof — callable standalone for post-hoc
    verification (e.g. by an evidence bundler). Parses both files and
    asserts:

    * Equal schema (column names + order).
    * Equal row count.
    * Equal element-ID set.
    * For every element ID, all non-``FORC`` column tokens are byte
      identical between baseline and variant.

    Per spec §"For all columns except FORC, old_att equals new_att at parse
    level (G4 proof)" (§3.2): raises the corresponding
    :class:`SpAttRewriteError` subclass on the first violation.

    Parameters
    ----------
    baseline_att_path, variant_att_path:
        Paths to the baseline and variant ``.sp.att`` files.

    Raises
    ------
    SchemaMismatchError
        Column names or order differ.
    RowCountMismatchError
        Row counts differ.
    ElementIdSetMismatchError
        Element-ID sets differ.
    NonForcColumnChangedError
        A non-``FORC`` column token differs (byte + semantic).
    SpAttRewriteError
        Baseline or variant is unparseable.
    """
    baseline = _parse_sp_att_file(baseline_att_path)
    variant = _parse_sp_att_file(variant_att_path)

    if baseline.schema != variant.schema:
        raise SchemaMismatchError(
            baseline_schema=baseline.schema,
            variant_schema=variant.schema,
        )
    if baseline.n_rows != variant.n_rows:
        raise RowCountMismatchError(
            baseline_count=baseline.n_rows,
            variant_count=variant.n_rows,
        )

    baseline_by_id = {int(row[0]): row for row in baseline.parsed_rows}
    variant_by_id = {int(row[0]): row for row in variant.parsed_rows}
    baseline_ids = set(baseline_by_id)
    variant_ids = set(variant_by_id)
    if baseline_ids != variant_ids:
        raise ElementIdSetMismatchError(
            baseline_only=tuple(sorted(baseline_ids - variant_ids)),
            variant_only=tuple(sorted(variant_ids - baseline_ids)),
        )
    forc_idx = baseline.forc_col_index
    for element_id in sorted(baseline_ids):
        b_row = baseline_by_id[element_id]
        v_row = variant_by_id[element_id]
        for col_idx in range(len(baseline.schema)):
            if col_idx == forc_idx:
                continue
            if b_row[col_idx] != v_row[col_idx]:
                raise NonForcColumnChangedError(
                    element_id=element_id,
                    column_name=baseline.schema[col_idx],
                    baseline_value=b_row[col_idx],
                    variant_value=v_row[col_idx],
                )


def copy_and_rewrite_sp_att_forc(
    baseline_att_path: pathlib.Path,
    variant_att_path: pathlib.Path,
    ownership: Sequence[ElementOwnership],
    shud_forcing_index: Mapping[str, int],
    *,
    used_cell_count: int,
) -> SpAttRewriteReport:
    """Copy baseline ``.sp.att`` and rewrite ``FORC`` by element ID.

    Fail-closed guarantee: on any error the variant ``.sp.att`` is NOT
    written; if a variant temp file was created it is removed. The
    baseline ``.sp.att`` is opened read-only throughout and its pre/post
    SHA-256 are verified equal (INV-1). ``variant_att_path`` MUST differ
    from ``baseline_att_path`` (INV-2).

    Association is by element ID (the ``INDEX`` column), never by row
    order. A baseline whose rows are in scrambled order (e.g. 3, 1, 4, 2)
    receives the correct per-element ``FORC`` because the lookup key is
    the parsed ``element_id``.

    Pipeline (fail-closed at every gate):

    1. Resolve both paths and refuse if variant resolves to baseline
       (INV-2).
    2. Compute baseline pre-SHA-256 (INV-1 anchor).
    3. Parse the baseline ``.sp.att`` (schema + rows).
    4. Validate ``ownership`` (no duplicate ``element_id``) and
       ``shud_forcing_index`` (every value int, every value in
       ``[1, used_cell_count]``, sorted values equal
       ``(1, 2, ..., used_cell_count)``).
    5. For each baseline row, look up
       ``element_id -> ownership.grid_cell_id -> shud_forcing_index[grid_cell_id]``
       to obtain the new ``FORC``. Missing ownership or missing index
       raises :class:`ForcUnmappedError`.
    6. Prove non-``FORC`` columns unchanged in-memory (fail-closed before
       any variant byte hits disk).
    7. Emit the parse-level semantic diff (FORC deltas keyed by
       ``element_id``, sorted ascending).
    8. Serialize the new content to a temp file next to
       ``variant_att_path``, compute the variant SHA-256, then atomically
       ``os.replace`` into ``variant_att_path``.
    9. Compute baseline post-SHA-256 and assert equal to pre-SHA-256
       (INV-1 hard block); on mismatch, unlink the variant and raise
       :class:`BaselineImmutabilityViolationError`.
    10. Return :class:`SpAttRewriteReport`.

    Parameters
    ----------
    baseline_att_path:
        Path to the baseline ``.sp.att`` file. MUST exist and be readable.
    variant_att_path:
        Path where the variant ``.sp.att`` will be written. MUST resolve
        to a path different from ``baseline_att_path`` (INV-2). The parent
        directory is created if missing.
    ownership:
        Sequence of :class:`ElementOwnership` records produced by
        :func:`workers.mapping_builder.nearest_cell_barycenter_geodesic_v1`.
        Every baseline ``element_id`` MUST have a corresponding record.
    shud_forcing_index:
        Mapping ``grid_cell_id -> shud_forcing_index`` (int) produced by
        :func:`workers.mapping_builder.assign_shud_forcing_index`. Values
        MUST be the contiguous set ``{1, 2, ..., used_cell_count}``.
    used_cell_count:
        The used-cell count derived by
        :func:`workers.mapping_builder.assign_shud_forcing_index`. Used to
        validate the ``FORC`` range ``1..used_cell_count`` and echoed in
        the report.

    Returns
    -------
    SpAttRewriteReport
        Immutable report with baseline/variant checksums + sizes, the
        semantic diff, rewritten row count, and ``used_cell_count``.

    Raises
    ------
    SpAttRewriteError (and subclasses)
        Any G4 blocker: INV-2 violation, unparseable baseline, ownership
        duplicate, integer/range/multiset failure on ``shud_forcing_index``,
        unmapped element, non-``FORC`` column change (defense in depth),
        or INV-1 violation (:class:`BaselineImmutabilityViolationError`).
    """
    # Step 1: INV-2 — refuse if variant path resolves to baseline path.
    # ``Path.resolve(strict=False)`` (default) returns the absolute path
    # even when the target does not exist, AND follows symlinks along
    # any existing prefix. This catches literal-equal paths, symlink
    # aliases pointing at the baseline, and ``..`` traversal aliases in a
    # single check.
    baseline_resolved = baseline_att_path.resolve()
    try:
        variant_resolved = variant_att_path.resolve()
    except (OSError, RuntimeError):  # pragma: no cover - defensive fallback
        variant_resolved = variant_att_path.absolute()
    if baseline_resolved == variant_resolved:
        raise SpAttRewriteError(
            f"variant_att_path {variant_att_path} resolves to the same path "
            f"as baseline_att_path {baseline_att_path}; INV-2 requires "
            "distinct paths (baseline MUST NOT be overwritten)"
        )

    if not baseline_att_path.exists() or not baseline_att_path.is_file():
        raise SpAttRewriteError(
            f"baseline .sp.att does not exist or is not a file: {baseline_att_path}"
        )

    # Step 2: pre-SHA-256 (INV-1 anchor).
    baseline_pre_sha = _sha256_file(baseline_att_path)
    baseline_size = baseline_att_path.stat().st_size

    # Step 3: parse baseline .sp.att.
    parsed = _parse_sp_att_file(baseline_att_path)

    # Step 4: validate ownership + shud_forcing_index.
    ownership_by_id: dict[int, ElementOwnership] = {}
    for o in ownership:
        if o.element_id in ownership_by_id:
            raise SpAttRewriteError(
                f"ownership sequence contains duplicate element_id={o.element_id}"
            )
        ownership_by_id[o.element_id] = o

    # 4a: every value in shud_forcing_index is int (reject bool explicitly
    # because bool is a subclass of int in Python).
    for grid_cell_id, forc_value in shud_forcing_index.items():
        if isinstance(forc_value, bool) or not isinstance(forc_value, int):
            raise ForcNonIntegerError(
                grid_cell_id=grid_cell_id, invalid_value=forc_value
            )
    # 4b: every value is in [1, used_cell_count].
    for grid_cell_id, forc_value in shud_forcing_index.items():
        if not (1 <= forc_value <= used_cell_count):
            raise ForcOutOfRangeError(
                new_forc=forc_value,
                valid_range=(1, used_cell_count),
                grid_cell_id=grid_cell_id,
            )
    # 4c: multiset equals {1, 2, ..., used_cell_count}.
    values_sorted = tuple(sorted(shud_forcing_index.values()))
    expected = tuple(range(1, used_cell_count + 1))
    if values_sorted != expected:
        raise ForcMultisetMismatchError(
            expected_values=expected,
            observed_values=values_sorted,
        )

    # Step 5: build new rows via element_id -> grid_cell_id -> forcing_index.
    baseline_forc_rows: list[SpAttForcRow] = []
    variant_forc_rows: list[SpAttForcRow] = []
    new_raw_data_lines: list[str] = []
    for raw_line, token_row in zip(
        parsed.raw_data_lines, parsed.parsed_rows, strict=True
    ):
        element_id = int(token_row[0])
        try:
            old_forc = int(token_row[parsed.forc_col_index])
        except ValueError as exc:
            raise SpAttRewriteError(
                f"baseline row element_id={element_id} FORC token "
                f"{token_row[parsed.forc_col_index]!r} is not an integer: {exc}"
            ) from exc
        if element_id not in ownership_by_id:
            raise ForcUnmappedError(
                element_id=element_id,
                detail="no ownership entry for baseline element_id",
            )
        grid_cell_id = ownership_by_id[element_id].grid_cell_id
        if grid_cell_id not in shud_forcing_index:
            raise ForcUnmappedError(
                element_id=element_id,
                grid_cell_id=grid_cell_id,
                detail="ownership.grid_cell_id not in shud_forcing_index",
            )
        new_forc = shud_forcing_index[grid_cell_id]
        baseline_forc_rows.append(
            SpAttForcRow(element_id=element_id, forc=old_forc)
        )
        variant_forc_rows.append(
            SpAttForcRow(element_id=element_id, forc=new_forc)
        )
        new_raw_data_lines.append(
            _replace_forc_token_in_row(
                raw_line, parsed.forc_col_index, new_forc
            )
        )

    # Step 6: prove non-FORC columns unchanged in-memory (defense in depth).
    _prove_non_forc_columns_unchanged_in_memory(parsed, new_raw_data_lines)

    # Step 7: emit semantic diff.
    semantic_diff = emit_semantic_diff(baseline_forc_rows, variant_forc_rows)

    # Step 8: serialize new content to temp path, compute SHA, atomic move.
    new_content = (
        parsed.header_line
        + parsed.header_terminator
        + parsed.column_header_line
        + parsed.column_header_terminator
        + "".join(new_raw_data_lines)
        + parsed.trailing_content
    )
    variant_att_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(variant_att_path.parent),
        prefix=f".{variant_att_path.name}.",
        suffix=".tmp",
    )
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_handle:
            tmp_handle.write(new_content.encode("utf-8"))
        variant_sha = _sha256_file(tmp_path)
        variant_size = tmp_path.stat().st_size
        os.replace(tmp_path, variant_att_path)
    finally:
        # If tmp still present (rename failed), cleanup — best effort.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

    # Step 9: post-SHA-256 (INV-1 hard block). On mismatch, unlink variant.
    baseline_post_sha = _sha256_file(baseline_att_path)
    if baseline_pre_sha != baseline_post_sha:
        try:
            variant_att_path.unlink()
        except OSError:  # pragma: no cover - best-effort cleanup
            pass
        raise BaselineImmutabilityViolationError(
            baseline_path=baseline_att_path,
            pre_sha256=baseline_pre_sha,
            post_sha256=baseline_post_sha,
        )

    # Step 10: return report.
    checksums = SpAttChecksums(
        baseline_sha256=baseline_pre_sha,
        variant_sha256=variant_sha,
        baseline_size=baseline_size,
        variant_size=variant_size,
    )
    return SpAttRewriteReport(
        checksums=checksums,
        semantic_diff=semantic_diff,
        rewritten_row_count=parsed.n_rows,
        used_cell_count=used_cell_count,
    )


def _prove_non_forc_columns_unchanged_in_memory(
    baseline: _ParsedSpAtt,
    variant_raw_data_lines: Sequence[str],
) -> None:
    """In-memory equivalent of :func:`prove_non_forc_columns_unchanged`.

    Runs BEFORE any variant byte hits disk (spec §"the builder fails
    closed when any non-FORC value differs"). Parses the newly-built
    variant data lines using the baseline schema and asserts per-row
    non-``FORC`` column token equality keyed by ``element_id``.
    """
    if len(variant_raw_data_lines) != baseline.n_rows:
        raise RowCountMismatchError(
            baseline_count=baseline.n_rows,
            variant_count=len(variant_raw_data_lines),
        )
    baseline_by_id = {int(row[0]): row for row in baseline.parsed_rows}
    variant_by_id: dict[int, tuple[str, ...]] = {}
    for raw_line in variant_raw_data_lines:
        content = raw_line.rstrip("\r\n")
        tokens = tuple(content.split()[: len(baseline.schema)])
        variant_by_id[int(tokens[0])] = tokens
    baseline_ids = set(baseline_by_id)
    variant_ids = set(variant_by_id)
    if baseline_ids != variant_ids:
        raise ElementIdSetMismatchError(
            baseline_only=tuple(sorted(baseline_ids - variant_ids)),
            variant_only=tuple(sorted(variant_ids - baseline_ids)),
        )
    forc_idx = baseline.forc_col_index
    for element_id in sorted(baseline_by_id):
        b_row = baseline_by_id[element_id]
        v_row = variant_by_id[element_id]
        for col_idx in range(len(baseline.schema)):
            if col_idx == forc_idx:
                continue
            if b_row[col_idx] != v_row[col_idx]:
                raise NonForcColumnChangedError(
                    element_id=element_id,
                    column_name=baseline.schema[col_idx],
                    baseline_value=b_row[col_idx],
                    variant_value=v_row[col_idx],
                )
