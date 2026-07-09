"""Tests for :mod:`workers.mapping_builder.rewrite` (Epic #909 SUB-8, §3.1 + §3.2).

Coverage
--------

* §3.1 :func:`copy_and_rewrite_sp_att_forc` — positive path (variant written,
  baseline SHA-256 unchanged, report populated); FORC-by-element-ID
  association proof (scrambled row order); FORC legality blockers
  (out-of-range, non-integer, unmapped element, multiset mismatch); INV-2
  variant-path safety; INV-1 baseline immutability enforcement via
  monkey-patched SHA-256.
* §3.2 :func:`prove_non_forc_columns_unchanged` — schema/row count/element
  ID/non-FORC column change blockers.
* :func:`emit_semantic_diff` — deterministic ordering by element_id and
  FORC-only content.
* :func:`record_sp_att_checksums` — SHA-256 recorded correctly for both
  files.
* Signature pin + frozen dataclass invariants matching the SUB-1..7 style.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import pathlib
import typing

import pytest

from workers.mapping_builder import (
    BaselineImmutabilityViolationError,
    ElementIdSetMismatchError,
    ElementOwnership,
    ForcMultisetMismatchError,
    ForcNonIntegerError,
    ForcOutOfRangeError,
    ForcUnmappedError,
    NonForcColumnChangedError,
    RowCountMismatchError,
    SchemaMismatchError,
    SemanticDiff,
    SemanticDiffEntry,
    SpAttChecksums,
    SpAttForcRow,
    SpAttRewriteError,
    SpAttRewriteReport,
    copy_and_rewrite_sp_att_forc,
    emit_semantic_diff,
    parse_sp_att_forc_rows,
    prove_non_forc_columns_unchanged,
    record_sp_att_checksums,
)
from workers.mapping_builder import rewrite as rewrite_module

# --- fixture helpers ------------------------------------------------------

# Baseline row structure: (INDEX, SOIL, GEOL, LC, FORC, MF, BC, SS, LAKE).
_SCHEMA = ("INDEX", "SOIL", "GEOL", "LC", "FORC", "MF", "BC", "SS", "LAKE")


def _write_sp_att(
    path: pathlib.Path,
    rows: list[tuple[int, int, int, int, int, int, int, int, int]],
    *,
    schema: tuple[str, ...] = _SCHEMA,
) -> pathlib.Path:
    """Write a ``.sp.att`` file with tab-separated header + rows.

    Preserves the SHUD ``.sp.att`` layout used by the fixture
    ``keliya_minimal/keliya.sp.att``: header + column names + data rows,
    with newline terminators after each line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{len(rows)}\t{len(schema)}", "\t".join(schema)]
    for row in rows:
        assert len(row) == len(schema), (
            f"row {row!r} has {len(row)} tokens, expected {len(schema)}"
        )
        lines.append("\t".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_ownership(
    element_id_to_cell_id: dict[int, str],
) -> tuple[ElementOwnership, ...]:
    """Build ownership records from ``element_id -> grid_cell_id`` mapping.

    ``canonical_ordinal`` is assigned by sorted-cell order (1-based).
    Other fields (distance, tie_status, candidate_count) are placeholder
    values — the rewrite pipeline reads only ``element_id`` and
    ``grid_cell_id``.
    """
    unique_cells = sorted(set(element_id_to_cell_id.values()))
    cell_to_ord = {cid: idx + 1 for idx, cid in enumerate(unique_cells)}
    return tuple(
        ElementOwnership(
            element_id=eid,
            grid_cell_id=cid,
            canonical_ordinal=cell_to_ord[cid],
            geodesic_distance_m=0.0,
            tie_status="unique",
            candidate_count=1,
        )
        for eid, cid in element_id_to_cell_id.items()
    )


def _make_shud_forcing_index(cell_ids: list[str]) -> dict[str, int]:
    """Assign contiguous 1..N to unique cells in sorted order."""
    unique = sorted(set(cell_ids))
    return {c: i + 1 for i, c in enumerate(unique)}


def _make_baseline_and_mapping(
    tmp_path: pathlib.Path,
    *,
    baseline_name: str = "baseline.sp.att",
    element_id_to_cell_id: dict[int, str] | None = None,
    rows_in_order: (
        list[tuple[int, int, int, int, int, int, int, int, int]] | None
    ) = None,
    baseline_forc_by_element_id: dict[int, int] | None = None,
) -> tuple[
    pathlib.Path,
    tuple[ElementOwnership, ...],
    dict[str, int],
    int,
]:
    """Build a baseline ``.sp.att`` + matching ownership + shud_forcing_index.

    Default: 4-element basin with each element mapped to its own cell, so
    used_cell_count == 4 (above the small-basin threshold).
    """
    if element_id_to_cell_id is None:
        element_id_to_cell_id = {1: "A", 2: "B", 3: "C", 4: "D"}
    if baseline_forc_by_element_id is None:
        # Baseline FORC just matches element_id 1..N.
        baseline_forc_by_element_id = {
            eid: eid for eid in element_id_to_cell_id
        }
    if rows_in_order is None:
        rows_in_order = [
            (
                eid,
                1,  # SOIL
                1,  # GEOL
                11,  # LC
                baseline_forc_by_element_id[eid],  # FORC
                1,  # MF
                0,  # BC
                0,  # SS
                0,  # LAKE
            )
            for eid in sorted(element_id_to_cell_id)
        ]
    baseline_path = _write_sp_att(tmp_path / baseline_name, rows_in_order)
    ownership = _make_ownership(element_id_to_cell_id)
    shud_forcing_index = _make_shud_forcing_index(
        list(element_id_to_cell_id.values())
    )
    used_cell_count = len(shud_forcing_index)
    return baseline_path, ownership, shud_forcing_index, used_cell_count


# --- §3.1 positive path ---------------------------------------------------


def test_rewrite_positive_path(tmp_path: pathlib.Path) -> None:
    """Well-formed baseline + valid mapping -> variant written, report correct.

    Sanity: baseline SHA-256 unchanged after rewrite; variant file exists
    and its SHA-256 matches the report; row count + used_cell_count are
    echoed on the report; semantic_diff has an entry for every row whose
    FORC changed.
    """
    (
        baseline_path,
        ownership,
        shud_forcing_index,
        used_cell_count,
    ) = _make_baseline_and_mapping(
        tmp_path,
        # Distinct cells so shud_forcing_index yields FORC={1,2,3,4} in
        # sorted-cell order. With element 1->D, 2->C, 3->A, 4->B and
        # sorted cells A=1, B=2, C=3, D=4, the new FORC per element is
        # 1->4, 2->3, 3->1, 4->2 — all rewritten and all different from
        # the baseline FORC=element_id assignment.
        element_id_to_cell_id={1: "D", 2: "C", 3: "A", 4: "B"},
    )
    baseline_sha_before = hashlib.sha256(baseline_path.read_bytes()).hexdigest()

    variant_path = tmp_path / "variant" / "test.sp.att"
    report = copy_and_rewrite_sp_att_forc(
        baseline_att_path=baseline_path,
        variant_att_path=variant_path,
        ownership=ownership,
        shud_forcing_index=shud_forcing_index,
        used_cell_count=used_cell_count,
    )
    assert isinstance(report, SpAttRewriteReport)

    # INV-1: baseline bytes MUST be unchanged.
    baseline_sha_after = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    assert baseline_sha_before == baseline_sha_after
    assert report.checksums.baseline_sha256 == baseline_sha_before

    # Variant file exists and its SHA matches the report.
    assert variant_path.exists()
    variant_sha_actual = hashlib.sha256(variant_path.read_bytes()).hexdigest()
    assert report.checksums.variant_sha256 == variant_sha_actual

    # Sizes and row count are recorded.
    assert report.checksums.baseline_size == baseline_path.stat().st_size
    assert report.checksums.variant_size == variant_path.stat().st_size
    assert report.rewritten_row_count == 4
    assert report.used_cell_count == 4

    # Semantic diff has an entry for every row (all FORC values changed).
    assert isinstance(report.semantic_diff, SemanticDiff)
    assert len(report.semantic_diff.entries) == 4
    # element_id 1 old FORC = 1 (baseline default), new FORC = 4 (cell D
    # sorted last).
    diff_by_id = {e.element_id: e for e in report.semantic_diff.entries}
    assert diff_by_id[1].old_forc == 1
    assert diff_by_id[1].new_forc == 4
    assert diff_by_id[2].new_forc == 3
    assert diff_by_id[3].new_forc == 1
    assert diff_by_id[4].new_forc == 2


def test_rewrite_by_element_id_not_row_order(tmp_path: pathlib.Path) -> None:
    """Scrambled row order -> new FORC follows element_id lookup, not row position.

    Baseline row order is [3, 1, 4, 2] but element IDs still form the
    contiguous set {1, 2, 3, 4}. Ownership maps element_id -> grid_cell_id.
    The variant's per-row FORC MUST match ``shud_forcing_index[ownership[element_id].grid_cell_id]``
    based on element_id, NOT the row position.

    Also asserts variant preserves the same scrambled row order (only
    the FORC column changed).
    """
    element_id_to_cell_id = {1: "D", 2: "C", 3: "A", 4: "B"}
    # scrambled row order
    scrambled = [
        (3, 5, 5, 11, 3, 1, 0, 0, 0),  # element_id=3 first
        (1, 5, 5, 11, 1, 1, 0, 0, 0),
        (4, 5, 5, 11, 4, 1, 0, 0, 0),
        (2, 5, 5, 11, 2, 1, 0, 0, 0),
    ]
    (
        baseline_path,
        ownership,
        shud_forcing_index,
        used_cell_count,
    ) = _make_baseline_and_mapping(
        tmp_path,
        element_id_to_cell_id=element_id_to_cell_id,
        rows_in_order=scrambled,
    )
    variant_path = tmp_path / "variant.sp.att"
    report = copy_and_rewrite_sp_att_forc(
        baseline_att_path=baseline_path,
        variant_att_path=variant_path,
        ownership=ownership,
        shud_forcing_index=shud_forcing_index,
        used_cell_count=used_cell_count,
    )
    assert report.rewritten_row_count == 4

    # Read the variant and confirm the FORC per row is the element_id
    # lookup, NOT the row-position lookup.
    variant_text = variant_path.read_text(encoding="utf-8")
    variant_lines = variant_text.splitlines()
    # Skip header lines (0, 1).
    data_lines = variant_lines[2:]
    # Confirm row order preserved (element IDs in scrambled order 3, 1, 4, 2).
    row_element_ids = [int(line.split()[0]) for line in data_lines]
    assert row_element_ids == [3, 1, 4, 2], (
        "row order should be preserved verbatim from baseline"
    )
    # sorted cells A=1, B=2, C=3, D=4 -> element 1->D=4, 2->C=3, 3->A=1, 4->B=2.
    expected_forc = {1: 4, 2: 3, 3: 1, 4: 2}
    for line in data_lines:
        tokens = line.split()
        eid = int(tokens[0])
        forc = int(tokens[4])
        assert forc == expected_forc[eid], (
            f"element_id={eid} expected new FORC={expected_forc[eid]} "
            f"but got {forc} (row-order association bug?)"
        )
    # Also cross-check via the semantic diff.
    diff_by_id = {e.element_id: e for e in report.semantic_diff.entries}
    for eid in (1, 2, 3, 4):
        assert diff_by_id[eid].new_forc == expected_forc[eid]


def test_baseline_checksum_unchanged_after_rewrite(
    tmp_path: pathlib.Path,
) -> None:
    """INV-1 evidence: baseline SHA-256 recorded on the report matches actual bytes.

    The recomputed post-rewrite SHA MUST equal both the recorded pre-SHA
    (from the report.checksums.baseline_sha256) AND an independently
    computed SHA of the baseline file bytes.
    """
    (
        baseline_path,
        ownership,
        shud_forcing_index,
        used_cell_count,
    ) = _make_baseline_and_mapping(tmp_path)
    baseline_bytes_before = baseline_path.read_bytes()
    baseline_sha_independent = hashlib.sha256(baseline_bytes_before).hexdigest()

    variant_path = tmp_path / "variant.sp.att"
    report = copy_and_rewrite_sp_att_forc(
        baseline_att_path=baseline_path,
        variant_att_path=variant_path,
        ownership=ownership,
        shud_forcing_index=shud_forcing_index,
        used_cell_count=used_cell_count,
    )

    # Bytes on disk unchanged.
    baseline_bytes_after = baseline_path.read_bytes()
    assert baseline_bytes_before == baseline_bytes_after
    # SHA-256 recorded matches independent computation.
    assert report.checksums.baseline_sha256 == baseline_sha_independent


# --- §3.1 FORC legality blockers ------------------------------------------


def test_forc_out_of_range_blocks(tmp_path: pathlib.Path) -> None:
    """shud_forcing_index value > used_cell_count -> ForcOutOfRangeError.

    Uses used_cell_count=2 but injects a shud_forcing_index value of 999,
    which is far outside the legal ``[1, 2]`` binding domain. The
    pre-validation loop MUST fail closed with :class:`ForcOutOfRangeError`
    (and NOT accept the variant despite the multiset having plausible
    total counts).
    """
    element_id_to_cell_id = {1: "A", 2: "B"}
    baseline_path, ownership, _, _ = _make_baseline_and_mapping(
        tmp_path,
        element_id_to_cell_id=element_id_to_cell_id,
        rows_in_order=[
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
        ],
    )
    # Poison the mapping: cell B gets 999.
    bad_shud_forcing_index = {"A": 1, "B": 999}
    variant_path = tmp_path / "variant.sp.att"
    with pytest.raises(ForcOutOfRangeError) as exc_info:
        copy_and_rewrite_sp_att_forc(
            baseline_att_path=baseline_path,
            variant_att_path=variant_path,
            ownership=ownership,
            shud_forcing_index=bad_shud_forcing_index,
            used_cell_count=2,
        )
    assert exc_info.value.new_forc == 999
    assert exc_info.value.valid_range == (1, 2)
    assert exc_info.value.grid_cell_id == "B"
    assert not variant_path.exists(), (
        "variant .sp.att MUST NOT be written on FORC out-of-range failure"
    )
    assert isinstance(exc_info.value, SpAttRewriteError)


def test_forc_non_integer_blocks(tmp_path: pathlib.Path) -> None:
    """shud_forcing_index value that is a float -> ForcNonIntegerError.

    Injects ``1.5`` into shud_forcing_index. The pre-validation loop MUST
    reject the value BEFORE the range check runs (so this test is
    orthogonal to :func:`test_forc_out_of_range_blocks`).
    """
    element_id_to_cell_id = {1: "A", 2: "B"}
    baseline_path, ownership, _, _ = _make_baseline_and_mapping(
        tmp_path,
        element_id_to_cell_id=element_id_to_cell_id,
        rows_in_order=[
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
        ],
    )
    bad_shud_forcing_index: dict[str, int] = {
        "A": 1,
        "B": 1.5,  # type: ignore[dict-item]
    }
    variant_path = tmp_path / "variant.sp.att"
    with pytest.raises(ForcNonIntegerError) as exc_info:
        copy_and_rewrite_sp_att_forc(
            baseline_att_path=baseline_path,
            variant_att_path=variant_path,
            ownership=ownership,
            shud_forcing_index=bad_shud_forcing_index,
            used_cell_count=2,
        )
    assert exc_info.value.grid_cell_id == "B"
    assert exc_info.value.invalid_value == 1.5
    assert not variant_path.exists(), (
        "variant .sp.att MUST NOT be written on FORC non-integer failure"
    )
    assert isinstance(exc_info.value, SpAttRewriteError)


def test_forc_unmapped_element_blocks(tmp_path: pathlib.Path) -> None:
    """Baseline element_id absent from ownership -> ForcUnmappedError.

    Baseline has 4 elements {1,2,3,4} but ownership is only supplied for
    {1,2,3}. The lookup for element_id=4 MUST raise
    :class:`ForcUnmappedError` before the variant file is written.
    """
    baseline_path, _, shud_forcing_index, used_cell_count = (
        _make_baseline_and_mapping(tmp_path)
    )
    # Only 3 ownership records (missing element 4).
    partial_ownership = _make_ownership(
        {1: "A", 2: "B", 3: "C"}
    )
    # But shud_forcing_index must still cover just those 3 cells to pass
    # pre-validation (so the failure is per-element, not multiset).
    partial_shud_forcing_index = {"A": 1, "B": 2, "C": 3}
    variant_path = tmp_path / "variant.sp.att"
    with pytest.raises(ForcUnmappedError) as exc_info:
        copy_and_rewrite_sp_att_forc(
            baseline_att_path=baseline_path,
            variant_att_path=variant_path,
            ownership=partial_ownership,
            shud_forcing_index=partial_shud_forcing_index,
            used_cell_count=3,
        )
    assert exc_info.value.element_id == 4
    assert not variant_path.exists()
    assert isinstance(exc_info.value, SpAttRewriteError)


def test_forc_multiset_mismatch_blocks(tmp_path: pathlib.Path) -> None:
    """shud_forcing_index maps 3 cells to duplicate indices -> ForcMultisetMismatchError.

    ``{A:1, B:1, C:2}`` with used_cell_count=3 has all values in ``[1, 3]``
    (so range check passes) but the sorted multiset ``(1, 1, 2)`` is not
    ``(1, 2, 3)`` — the multiset check MUST fail closed.
    """
    element_id_to_cell_id = {1: "A", 2: "B", 3: "C"}
    baseline_path, ownership, _, _ = _make_baseline_and_mapping(
        tmp_path,
        element_id_to_cell_id=element_id_to_cell_id,
        rows_in_order=[
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
            (3, 1, 1, 11, 3, 1, 0, 0, 0),
        ],
    )
    bad_shud_forcing_index = {"A": 1, "B": 1, "C": 2}
    variant_path = tmp_path / "variant.sp.att"
    with pytest.raises(ForcMultisetMismatchError) as exc_info:
        copy_and_rewrite_sp_att_forc(
            baseline_att_path=baseline_path,
            variant_att_path=variant_path,
            ownership=ownership,
            shud_forcing_index=bad_shud_forcing_index,
            used_cell_count=3,
        )
    assert exc_info.value.expected_values == (1, 2, 3)
    assert exc_info.value.observed_values == (1, 1, 2)
    assert not variant_path.exists()
    assert isinstance(exc_info.value, SpAttRewriteError)


# --- §3.2 non-FORC change blockers (G4 proof) -----------------------------


def test_non_forc_column_change_blocks(tmp_path: pathlib.Path) -> None:
    """prove_non_forc_columns_unchanged catches a SOIL column change.

    Hand-crafts a variant .sp.att where element_id=1's SOIL differs from
    the baseline. Since the variant is NOT produced via
    :func:`copy_and_rewrite_sp_att_forc` (which would refuse to write on
    the in-memory G4 proof), we exercise the standalone G4 gate directly.
    """
    baseline_path = _write_sp_att(
        tmp_path / "baseline.sp.att",
        [
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
            (3, 1, 1, 11, 3, 1, 0, 0, 0),
            (4, 1, 1, 11, 4, 1, 0, 0, 0),
        ],
    )
    # Same schema, same row count, same element IDs — but SOIL for
    # element_id=1 changed from 1 to 42.
    corrupt_variant = _write_sp_att(
        tmp_path / "variant.sp.att",
        [
            (1, 42, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
            (3, 1, 1, 11, 3, 1, 0, 0, 0),
            (4, 1, 1, 11, 4, 1, 0, 0, 0),
        ],
    )
    with pytest.raises(NonForcColumnChangedError) as exc_info:
        prove_non_forc_columns_unchanged(baseline_path, corrupt_variant)
    assert exc_info.value.element_id == 1
    assert exc_info.value.column_name == "SOIL"
    assert exc_info.value.baseline_value == "1"
    assert exc_info.value.variant_value == "42"
    assert isinstance(exc_info.value, SpAttRewriteError)


def test_row_count_change_blocks(tmp_path: pathlib.Path) -> None:
    """Row count mismatch -> RowCountMismatchError from the G4 proof."""
    baseline_path = _write_sp_att(
        tmp_path / "baseline.sp.att",
        [
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
            (3, 1, 1, 11, 3, 1, 0, 0, 0),
            (4, 1, 1, 11, 4, 1, 0, 0, 0),
        ],
    )
    # Variant has one extra row.
    short_variant = _write_sp_att(
        tmp_path / "variant.sp.att",
        [
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
            (3, 1, 1, 11, 3, 1, 0, 0, 0),
        ],
    )
    with pytest.raises(RowCountMismatchError) as exc_info:
        prove_non_forc_columns_unchanged(baseline_path, short_variant)
    assert exc_info.value.baseline_count == 4
    assert exc_info.value.variant_count == 3
    assert isinstance(exc_info.value, SpAttRewriteError)


def test_element_id_set_change_blocks(tmp_path: pathlib.Path) -> None:
    """Element-ID set mismatch -> ElementIdSetMismatchError from the G4 proof.

    Same row count but element_id=4 has been renumbered to 5 in the
    variant. Sets differ: baseline={1,2,3,4}, variant={1,2,3,5}.
    """
    baseline_path = _write_sp_att(
        tmp_path / "baseline.sp.att",
        [
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
            (3, 1, 1, 11, 3, 1, 0, 0, 0),
            (4, 1, 1, 11, 4, 1, 0, 0, 0),
        ],
    )
    renumbered_variant = _write_sp_att(
        tmp_path / "variant.sp.att",
        [
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
            (3, 1, 1, 11, 3, 1, 0, 0, 0),
            (5, 1, 1, 11, 4, 1, 0, 0, 0),
        ],
    )
    with pytest.raises(ElementIdSetMismatchError) as exc_info:
        prove_non_forc_columns_unchanged(baseline_path, renumbered_variant)
    assert exc_info.value.baseline_only == (4,)
    assert exc_info.value.variant_only == (5,)
    assert isinstance(exc_info.value, SpAttRewriteError)


def test_schema_change_blocks(tmp_path: pathlib.Path) -> None:
    """Schema (column names) mismatch -> SchemaMismatchError from the G4 proof.

    Baseline uses ``LAKE``; variant uses ``iLAKE`` (a live variant per
    integrity.py module docstring). Different tokens even though the
    semantic role is the same — G4 proof requires strict schema equality.
    """
    baseline_schema = ("INDEX", "SOIL", "GEOL", "LC", "FORC", "MF", "BC", "SS", "LAKE")
    variant_schema = ("INDEX", "SOIL", "GEOL", "LC", "FORC", "MF", "BC", "SS", "iLAKE")
    baseline_path = _write_sp_att(
        tmp_path / "baseline.sp.att",
        [
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
        ],
        schema=baseline_schema,
    )
    variant_path = _write_sp_att(
        tmp_path / "variant.sp.att",
        [
            (1, 1, 1, 11, 1, 1, 0, 0, 0),
            (2, 1, 1, 11, 2, 1, 0, 0, 0),
        ],
        schema=variant_schema,
    )
    with pytest.raises(SchemaMismatchError) as exc_info:
        prove_non_forc_columns_unchanged(baseline_path, variant_path)
    assert exc_info.value.baseline_schema == baseline_schema
    assert exc_info.value.variant_schema == variant_schema
    assert isinstance(exc_info.value, SpAttRewriteError)


# --- semantic diff evidence -----------------------------------------------


def test_semantic_diff_only_forc_changes(tmp_path: pathlib.Path) -> None:
    """Diff contains ONLY element_id + old_forc + new_forc, no other noise.

    Uses :func:`emit_semantic_diff` directly with two SpAttForcRow
    sequences. Verifies the diff:
      * Excludes rows where old_forc == new_forc.
      * Records only (element_id, old_forc, new_forc) — no schema/column
        names, no whitespace-shaped noise.
      * Is a :class:`SemanticDiff` with :class:`SemanticDiffEntry` entries.
    """
    baseline_rows = (
        SpAttForcRow(element_id=1, forc=1),
        SpAttForcRow(element_id=2, forc=2),
        SpAttForcRow(element_id=3, forc=3),
    )
    variant_rows = (
        SpAttForcRow(element_id=1, forc=5),  # changed
        SpAttForcRow(element_id=2, forc=2),  # unchanged
        SpAttForcRow(element_id=3, forc=7),  # changed
    )
    diff = emit_semantic_diff(baseline_rows, variant_rows)
    assert isinstance(diff, SemanticDiff)
    # Only 2 entries (element_id=2 unchanged is excluded).
    assert len(diff.entries) == 2
    ids = [e.element_id for e in diff.entries]
    assert 2 not in ids
    for entry in diff.entries:
        assert isinstance(entry, SemanticDiffEntry)
        # SemanticDiffEntry fields are exactly {element_id, old_forc, new_forc}.
        entry_fields = {f.name for f in dataclasses.fields(entry)}
        assert entry_fields == {"element_id", "old_forc", "new_forc"}, (
            "SemanticDiffEntry MUST only carry (element_id, old_forc, "
            f"new_forc) — no schema/column-header/noise; got {entry_fields}"
        )
    # Concrete deltas: element 1: 1->5, element 3: 3->7.
    diff_by_id = {e.element_id: e for e in diff.entries}
    assert diff_by_id[1].old_forc == 1
    assert diff_by_id[1].new_forc == 5
    assert diff_by_id[3].old_forc == 3
    assert diff_by_id[3].new_forc == 7


def test_semantic_diff_ordered_by_element_id() -> None:
    """Two runs on shuffled inputs -> byte-identical diff artifact.

    Feeds baseline_rows in reversed order the second time. Since
    :func:`emit_semantic_diff` sorts by element_id ascending, both runs
    MUST produce equal :class:`SemanticDiff` tuples.
    """
    baseline_rows = [
        SpAttForcRow(element_id=1, forc=1),
        SpAttForcRow(element_id=2, forc=2),
        SpAttForcRow(element_id=3, forc=3),
    ]
    variant_rows = [
        SpAttForcRow(element_id=1, forc=10),
        SpAttForcRow(element_id=2, forc=20),
        SpAttForcRow(element_id=3, forc=30),
    ]
    diff_1 = emit_semantic_diff(baseline_rows, variant_rows)
    diff_2 = emit_semantic_diff(
        list(reversed(baseline_rows)),
        list(reversed(variant_rows)),
    )
    assert diff_1 == diff_2, (
        "identical inputs (different order) MUST yield equal semantic diffs "
        "(§7 determinism requirement)"
    )
    # Explicit ascending-by-element_id ordering.
    ordered_ids = [e.element_id for e in diff_1.entries]
    assert ordered_ids == sorted(ordered_ids)
    assert ordered_ids == [1, 2, 3]


# --- checksum evidence -----------------------------------------------------


def test_checksums_recorded_correctly(tmp_path: pathlib.Path) -> None:
    """SpAttChecksums fields = independently computed SHA-256 + size for both files.

    Runs :func:`copy_and_rewrite_sp_att_forc` and then independently
    :func:`record_sp_att_checksums`. Both records MUST agree with a
    manual ``hashlib.sha256`` computation and ``os.stat`` size.
    """
    (
        baseline_path,
        ownership,
        shud_forcing_index,
        used_cell_count,
    ) = _make_baseline_and_mapping(tmp_path)
    variant_path = tmp_path / "variant.sp.att"
    report = copy_and_rewrite_sp_att_forc(
        baseline_att_path=baseline_path,
        variant_att_path=variant_path,
        ownership=ownership,
        shud_forcing_index=shud_forcing_index,
        used_cell_count=used_cell_count,
    )
    baseline_sha_actual = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    variant_sha_actual = hashlib.sha256(variant_path.read_bytes()).hexdigest()
    assert report.checksums.baseline_sha256 == baseline_sha_actual
    assert report.checksums.variant_sha256 == variant_sha_actual
    assert report.checksums.baseline_size == baseline_path.stat().st_size
    assert report.checksums.variant_size == variant_path.stat().st_size

    # Standalone record_sp_att_checksums MUST agree.
    standalone = record_sp_att_checksums(baseline_path, variant_path)
    assert isinstance(standalone, SpAttChecksums)
    assert standalone == report.checksums


# --- INV-1 hard block -----------------------------------------------------


def test_baseline_immutability_violation_raises(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated baseline mutation between pre/post SHA -> BaselineImmutabilityViolationError.

    Monkeypatches :func:`workers.mapping_builder.rewrite._sha256_file` so
    the SECOND call on the baseline path returns a different digest,
    simulating a mid-run mutation without actually mutating the baseline
    on disk. The variant file MUST be unlinked before the exception raises.
    """
    (
        baseline_path,
        ownership,
        shud_forcing_index,
        used_cell_count,
    ) = _make_baseline_and_mapping(tmp_path)
    variant_path = tmp_path / "variant" / "test.sp.att"

    original_sha = rewrite_module._sha256_file
    call_counts: dict[str, int] = {}

    def fake_sha(path: pathlib.Path) -> str:
        key = str(path)
        call_counts[key] = call_counts.get(key, 0) + 1
        real = original_sha(path)
        # On the SECOND call to the baseline path, return a different
        # digest to simulate baseline mutation between pre and post.
        if key == str(baseline_path) and call_counts[key] == 2:
            return "0" * 64
        return real

    monkeypatch.setattr(rewrite_module, "_sha256_file", fake_sha)

    with pytest.raises(BaselineImmutabilityViolationError) as exc_info:
        copy_and_rewrite_sp_att_forc(
            baseline_att_path=baseline_path,
            variant_att_path=variant_path,
            ownership=ownership,
            shud_forcing_index=shud_forcing_index,
            used_cell_count=used_cell_count,
        )
    assert exc_info.value.baseline_path == baseline_path
    assert exc_info.value.pre_sha256 != exc_info.value.post_sha256
    assert exc_info.value.post_sha256 == "0" * 64
    # Variant file MUST be removed as part of the fail-closed cleanup.
    assert not variant_path.exists(), (
        "variant .sp.att MUST be unlinked on INV-1 violation"
    )


# --- variant path safety --------------------------------------------------


def test_variant_path_equals_baseline_path_refuses(
    tmp_path: pathlib.Path,
) -> None:
    """Same path for baseline and variant -> refuse with a clear error.

    INV-2 defends the baseline from accidental overwrite via aliased path.
    The refusal MUST come BEFORE any pre-SHA-256, parse, or write step —
    otherwise a race with a concurrent reader could see a partial file.
    """
    (
        baseline_path,
        ownership,
        shud_forcing_index,
        used_cell_count,
    ) = _make_baseline_and_mapping(tmp_path)
    baseline_bytes_before = baseline_path.read_bytes()

    # Explicit literal-equal path.
    with pytest.raises(SpAttRewriteError) as exc_info:
        copy_and_rewrite_sp_att_forc(
            baseline_att_path=baseline_path,
            variant_att_path=baseline_path,
            ownership=ownership,
            shud_forcing_index=shud_forcing_index,
            used_cell_count=used_cell_count,
        )
    # The message must call out INV-2.
    assert "INV-2" in str(exc_info.value)
    # Baseline bytes MUST be untouched.
    assert baseline_path.read_bytes() == baseline_bytes_before

    # Also test symlink-aliased path (same file via a different name).
    alias_path = tmp_path / "alias.sp.att"
    try:
        alias_path.symlink_to(baseline_path)
    except OSError:  # pragma: no cover - platform without symlink privilege
        pytest.skip("cannot create symlinks on this platform")
    with pytest.raises(SpAttRewriteError) as exc_info:
        copy_and_rewrite_sp_att_forc(
            baseline_att_path=baseline_path,
            variant_att_path=alias_path,
            ownership=ownership,
            shud_forcing_index=shud_forcing_index,
            used_cell_count=used_cell_count,
        )
    assert "INV-2" in str(exc_info.value)
    assert baseline_path.read_bytes() == baseline_bytes_before


# --- signature pins + frozen dataclass invariants -------------------------


def test_copy_and_rewrite_sp_att_forc_signature_pinned() -> None:
    """Signature pin: parameter names + type hints + return type frozen."""
    sig = inspect.signature(copy_and_rewrite_sp_att_forc)
    assert list(sig.parameters) == [
        "baseline_att_path",
        "variant_att_path",
        "ownership",
        "shud_forcing_index",
        "used_cell_count",
    ]
    hints = typing.get_type_hints(copy_and_rewrite_sp_att_forc)
    assert hints["baseline_att_path"] is pathlib.Path
    assert hints["variant_att_path"] is pathlib.Path
    assert hints["used_cell_count"] is int
    assert hints["return"] is SpAttRewriteReport


def test_prove_non_forc_columns_unchanged_signature_pinned() -> None:
    """Signature pin for the G4 standalone gate."""
    sig = inspect.signature(prove_non_forc_columns_unchanged)
    assert list(sig.parameters) == [
        "baseline_att_path",
        "variant_att_path",
    ]
    hints = typing.get_type_hints(prove_non_forc_columns_unchanged)
    assert hints["baseline_att_path"] is pathlib.Path
    assert hints["variant_att_path"] is pathlib.Path
    # Returns None (raises on violation).
    assert hints.get("return") is type(None)


def test_sp_att_rewrite_report_frozen() -> None:
    """SpAttRewriteReport is frozen; field assignment must raise."""
    report = SpAttRewriteReport(
        checksums=SpAttChecksums(
            baseline_sha256="a" * 64,
            variant_sha256="b" * 64,
            baseline_size=100,
            variant_size=101,
        ),
        semantic_diff=SemanticDiff(entries=()),
        rewritten_row_count=4,
        used_cell_count=4,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.rewritten_row_count = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.checksums = None  # type: ignore[misc]


def test_sp_att_rewrite_error_is_distinct_root() -> None:
    """SpAttRewriteError MUST NOT be a subclass of the G0/G1 or G2/G3 roots.

    Guards the design decision that G4 rewrite failures form a DISTINCT
    family so callers can differentiate with dedicated ``except`` clauses.
    """
    from workers.mapping_builder import (
        BaselineIntegrityError,
        MappingAlgorithmError,
    )

    assert not issubclass(SpAttRewriteError, BaselineIntegrityError)
    assert not issubclass(SpAttRewriteError, MappingAlgorithmError)
    # And every named subclass IS a SpAttRewriteError.
    subclasses = (
        BaselineImmutabilityViolationError,
        ElementIdSetMismatchError,
        ForcMultisetMismatchError,
        ForcNonIntegerError,
        ForcOutOfRangeError,
        ForcUnmappedError,
        NonForcColumnChangedError,
        RowCountMismatchError,
        SchemaMismatchError,
    )
    for cls in subclasses:
        assert issubclass(cls, SpAttRewriteError), (
            f"{cls.__name__} MUST inherit from SpAttRewriteError"
        )


def test_parse_sp_att_forc_rows_roundtrip(tmp_path: pathlib.Path) -> None:
    """parse_sp_att_forc_rows returns (element_id, FORC) rows in file order.

    Helper coverage: proves the parser feeds :func:`emit_semantic_diff`
    correctly when the caller wants a disk-based diff.
    """
    path = _write_sp_att(
        tmp_path / "sample.sp.att",
        [
            (3, 5, 5, 11, 3, 1, 0, 0, 0),
            (1, 5, 5, 11, 1, 1, 0, 0, 0),
            (2, 5, 5, 11, 2, 1, 0, 0, 0),
        ],
    )
    rows = parse_sp_att_forc_rows(path)
    assert len(rows) == 3
    # Order preserved: 3, 1, 2.
    assert [r.element_id for r in rows] == [3, 1, 2]
    assert [r.forc for r in rows] == [3, 1, 2]
    for row in rows:
        assert isinstance(row, SpAttForcRow)
