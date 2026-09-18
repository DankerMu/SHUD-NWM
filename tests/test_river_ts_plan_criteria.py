"""The #2451 pass criteria must BITE — proven on synthetic plans, no database.

``tasks.md`` 1.5's Verify clause is exactly this: *a synthetic plan that carries
``river_segment_key`` late in the ``Index Cond`` and removes nothing at the heap
layer is REJECTED by the extended criteria*. That plan passes criteria 1 and 2,
which is the hole criterion 3 was added to close, and it is constructible only
as a literal — PostgreSQL will not produce it on demand. So it is asserted here
rather than in the integration module, which cannot run without
PostgreSQL + TimescaleDB.

The node shapes below are transcribed from real measurements, not invented:
the bad narrow plan from
``.../receipts/2026-09-17-i8-explain-gate/stats-proof-2451.json`` (discovery
index, ``river_segment_key`` in ``Filter``, 71976 removed / 24 returned, 1581
shared hits), the good one from the same file (primary key, 27 shared hits), and
the legacy ``DecompressChunk`` pair from ``explain-1987.json`` case
``shj_nj/legacy``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.river_ts_plan_criteria import (
    BRANCH_IDENTITY_COLUMNS,
    DEFAULT_SHARED_HIT_MULTIPLE,
    SHARED_HIT_ABSOLUTE_FLOOR,
    cell_row,
    evaluate_cell,
    extract_cell_nodes,
)

_NARROW_CHUNK = "_hyper_6_2_chunk"
_LEGACY_CHUNK = "_hyper_3_62_chunk"
_LEGACY_COMPRESSED_CHILD = "compress_hyper_7_104_chunk"
_FILTER_RATIO_LIMIT = 10
#: The post-ANALYZE primary-key measurement the throwaway fixture recorded.
_BASELINE = 27


def _scan(
    *,
    relation: str,
    node_type: str = "Index Scan",
    index: str | None = None,
    index_cond: str | None = None,
    filter_text: str | None = None,
    removed: int = 0,
    actual: int = 24,
    hits: int = 27,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "Node Type": node_type,
        "Relation Name": relation,
        "Actual Rows": actual,
        "Actual Loops": 1,
        "Rows Removed by Filter": removed,
        "Shared Hit Blocks": hits,
        "Shared Read Blocks": 0,
        "Plan Rows": 24,
        "Startup Cost": 0.42,
        "Total Cost": 25.14,
    }
    if index is not None:
        node["Index Name"] = index
    if index_cond is not None:
        node["Index Cond"] = index_cond
    if filter_text is not None:
        node["Filter"] = filter_text
    if children:
        node["Plans"] = children
    return node


def _root(child: dict[str, Any]) -> dict[str, Any]:
    """A plan root that is NOT itself a scan, as a real EXPLAIN emits."""
    return {"Node Type": "Sort", "Actual Rows": 24, "Actual Loops": 1, "Plans": [child]}


def _narrow(plan: dict[str, Any], baseline: int | None = _BASELINE) -> dict[str, Any]:
    return evaluate_cell(
        plan,
        chunk_relation=_NARROW_CHUNK,
        branch="narrow",
        filter_ratio_limit=_FILTER_RATIO_LIMIT,
        shared_hit_baseline=baseline,
    )


# ---------------------------------------------------------------------------
# tasks.md 1.5 — the criterion that had to be added
# ---------------------------------------------------------------------------


_SEGMENT_KEY_LAST_COND = (
    "((run_key = $7) AND (basin_version_key = $4) AND (river_network_version_key = $6) "
    "AND (variable_e = 'q_down'::hydro.river_variable) "
    "AND (valid_time >= '2026-06-01 00:00:00+00'::timestamp with time zone) "
    "AND (valid_time <= '2026-06-08 00:00:00+00'::timestamp with time zone) "
    "AND (river_segment_key = $5))"
)


def test_segment_key_late_in_the_index_cond_passes_criteria_1_and_2() -> None:
    """The hole, stated before it is closed: 1 and 2 alone do not catch it.

    PostgreSQL lists EVERY qual on an indexed column in ``Index Cond``, boundary
    or not, and rows discarded inside the index layer never reach
    ``Rows Removed by Filter``. So an index shaped
    ``(run_key, …, valid_time, river_segment_key)`` traverses every entry for the
    run on the chunk and still reports a clean ratio.
    """
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                index="a_bad_index_with_the_segment_key_last",
                index_cond=_SEGMENT_KEY_LAST_COND,
                removed=0,
                actual=24,
                hits=1500,
            )
        ),
        baseline=None,
    )
    assert cell["criterion_1_segment_identity_bound"] is True
    assert cell["criterion_2_filter_ratio"] is True
    assert cell["criterion_3_shared_hits"] is None


def test_segment_key_late_in_the_index_cond_is_rejected_by_criterion_3() -> None:
    """tasks.md 1.5's Verify clause, asserted."""
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                index="a_bad_index_with_the_segment_key_last",
                index_cond=_SEGMENT_KEY_LAST_COND,
                removed=0,
                actual=24,
                hits=1500,
            )
        )
    )
    assert cell["criterion_3_shared_hits"] is False
    assert cell["passed"] is False
    assert any("criterion 3" in failure for failure in cell["failures"]), cell["failures"]
    assert cell["shared_hit_limit"] == _BASELINE * DEFAULT_SHARED_HIT_MULTIPLE


def test_the_same_plan_within_the_baseline_multiple_is_accepted() -> None:
    """Criterion 3 must not be a blanket reject: the good plan still passes.

    Same node, same ``Index Cond``, only the block count differs. Without this
    the previous test would be green for "criterion 3 rejects everything".
    """
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                index="2_4_river_timeseries_narrow_pkey",
                index_cond=_SEGMENT_KEY_LAST_COND,
                removed=0,
                actual=24,
                hits=_BASELINE * DEFAULT_SHARED_HIT_MULTIPLE,
            )
        )
    )
    assert cell["criterion_3_shared_hits"] is True
    assert cell["passed"] is True
    assert cell["defect_reproduced"] is False


# ---------------------------------------------------------------------------
# Criterion 3's absolute floor — the 2026-09-18 miscalibration, pinned
#
# The bench's first full run measured 24 cells, 6 failing. Two of those six were
# the SAME healthy plan shape as a cell that passed, separated only by which
# integer the post-ANALYZE baseline happened to be. The nodes below are that run's
# numbers, transcribed from /home/nwm/tmp/2451/matrix.json on node-27.
# ---------------------------------------------------------------------------

_HEALTHY_SEGMENT_TIME_COND = (
    "((river_segment_key = $5) AND (variable_e = 'q_down'::hydro.river_variable) "
    "AND (valid_time >= '2026-06-01 00:00:00+00'::timestamp with time zone) "
    "AND (valid_time <= '2026-06-08 00:00:00+00'::timestamp with time zone))"
)


def _healthy_narrow_cell(*, hits: int, baseline: int) -> dict[str, Any]:
    """``latest/absent/narrow/uncompressed`` as measured: segment key BOUND, ratio 0."""
    return _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                index="_hyper_6_2_chunk_river_ts_segment_time_key_idx",
                index_cond=_HEALTHY_SEGMENT_TIME_COND,
                filter_text="((run_key = ANY ($7)) AND (basin_version_key = $4))",
                removed=0,
                actual=48,
                hits=hits,
            )
        ),
        baseline=baseline,
    )


def test_the_adjacency_pair_the_multiple_alone_could_not_tell_apart() -> None:
    """51 hits/baseline 8 PASSED and 50 hits/baseline 6 FAILED on 2026-09-18.

    Same plan shape, same bound segment key, same 0.0 ratio, opposite verdicts —
    which is the proof that the multiple alone is not a criterion. Under the
    floor BOTH pass, and the raw multiple comparison is still recorded for the
    one that trips it.
    """
    legacy_like = _healthy_narrow_cell(hits=51, baseline=8)
    narrow_like = _healthy_narrow_cell(hits=50, baseline=6)

    assert legacy_like["shared_hit_limit"] == 64
    assert legacy_like["criterion_3_multiple_exceeded"] is False
    assert legacy_like["criterion_3_shared_hits"] is True
    assert legacy_like["passed"] is True

    assert narrow_like["shared_hit_limit"] == 48
    # The raw comparison the old rule failed on is still in the record …
    assert narrow_like["criterion_3_multiple_exceeded"] is True
    assert narrow_like["shared_hit_multiple_observed"] == pytest.approx(50 / 6)
    # … and the verdict is now the floored one.
    assert narrow_like["criterion_3_shared_hits"] is True
    assert narrow_like["passed"] is True
    assert not any("criterion 3" in failure for failure in narrow_like["failures"]), narrow_like["failures"]
    assert "multiple_exceeded=True" in cell_row(narrow_like)


def test_the_genuine_defect_cells_still_fail_criterion_3_under_the_floor() -> None:
    """The other half of the same run: 5977 hits/baseline 3, 12841/baseline 4.

    These are the cells that read the whole network. A floor that let them
    through would have made criterion 3 unfalsifiable, which is worse than the
    miscalibration it replaces.
    """
    for hits, baseline in ((5977, 3), (12841, 4)):
        cell = _healthy_narrow_cell(hits=hits, baseline=baseline)
        assert cell["criterion_3_multiple_exceeded"] is True, (hits, baseline)
        assert cell["criterion_3_shared_hits"] is False, (hits, baseline)
        assert cell["passed"] is False, (hits, baseline)
        assert any("criterion 3" in failure for failure in cell["failures"]), cell["failures"]
        assert str(SHARED_HIT_ABSOLUTE_FLOOR) in " ".join(cell["failures"])


def test_hits_above_the_floor_but_within_the_multiple_still_pass() -> None:
    """The floor is a CONJUNCT, not a replacement: the multiple still has to bite.

    300 shared hits is above the 256-buffer floor, but the baseline is 50 so the
    limit is 400 and nothing anomalous happened. Without this the previous test
    would be green for "anything over 256 fails".
    """
    cell = _healthy_narrow_cell(hits=300, baseline=50)
    assert cell["max_shared_hit_blocks"] > SHARED_HIT_ABSOLUTE_FLOOR
    assert cell["shared_hit_limit"] == 400
    assert cell["criterion_3_multiple_exceeded"] is False
    assert cell["criterion_3_shared_hits"] is True
    assert cell["passed"] is True


def test_the_floor_is_the_documented_constant_and_both_verdicts_are_per_node() -> None:
    """tasks.md: the archived run must be re-readable under the new rule.

    Per node as well as per cell, because a cell aggregates and the per-node
    numbers are what a reader compares against the receipt.
    """
    assert SHARED_HIT_ABSOLUTE_FLOOR == 256
    cell = _healthy_narrow_cell(hits=50, baseline=6)
    assert cell["shared_hit_absolute_floor"] == 256
    (node,) = cell["nodes"]
    assert node["criterion_3_pass"] is True
    assert node["criterion_3_multiple_exceeded"] is True
    assert node["shared_hit_multiple_observed"] == pytest.approx(50 / 6)


def test_criterion_3_stays_unevaluated_rather_than_floored_when_there_is_no_baseline() -> None:
    """A missing baseline must not become "below the floor, therefore fine"."""
    cell = _healthy_narrow_cell(hits=50_000, baseline=None)  # type: ignore[arg-type]
    assert cell["criterion_3_shared_hits"] is None
    assert cell["criterion_3_multiple_exceeded"] is None
    assert cell["shared_hit_multiple_observed"] is None
    (node,) = cell["nodes"]
    assert node["criterion_3_pass"] is None


def test_a_zero_baseline_reports_a_multiple_instead_of_dividing_by_zero() -> None:
    """A post-ANALYZE plan may legitimately measure 0 shared hits."""
    cell = _healthy_narrow_cell(hits=9, baseline=0)
    assert cell["shared_hit_limit"] == 0
    assert cell["criterion_3_multiple_exceeded"] is True
    assert cell["shared_hit_multiple_observed"] == pytest.approx(9.0)
    # 9 buffers is nowhere near a full-network scan, so the floor holds it.
    assert cell["criterion_3_shared_hits"] is True


def test_criteria_1_and_2_are_not_weakened_by_the_floor() -> None:
    """The floor is scoped to criterion 3 alone.

    A node reading 4 buffers — far below the floor — that lost the segment key
    from its access predicate and removed 71976 rows must still fail 1 and 2.
    """
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                index="_hyper_6_2_chunk_river_ts_run_discovery_key_idx",
                index_cond="((run_key = $7) AND (basin_version_key = $4))",
                filter_text="(river_segment_key = $5)",
                removed=71976,
                actual=24,
                hits=4,
            ),
        ),
        baseline=27,
    )
    assert cell["criterion_1_segment_identity_bound"] is False
    assert cell["criterion_2_filter_ratio"] is False
    assert cell["criterion_3_shared_hits"] is True
    assert cell["passed"] is False


# ---------------------------------------------------------------------------
# The measured #2451 defect, and the measured good plan
# ---------------------------------------------------------------------------


def test_the_measured_defect_plan_fails_criteria_1_and_2() -> None:
    """The real 2026-09-17 throwaway measurement, transcribed."""
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                index="_hyper_6_2_chunk_river_ts_run_discovery_key_idx",
                index_cond=(
                    "((run_key = $7) AND (basin_version_key = $4) AND (river_network_version_key = $6) "
                    "AND (variable_e = 'q_down'::hydro.river_variable) "
                    "AND (valid_time >= '2026-06-01 00:00:00+00'::timestamp with time zone))"
                ),
                filter_text="(river_segment_key = $5)",
                removed=71976,
                actual=24,
                hits=1581,
            )
        )
    )
    assert cell["criterion_1_segment_identity_bound"] is False
    assert cell["criterion_2_filter_ratio"] is False
    assert cell["criterion_3_shared_hits"] is False
    assert cell["defect_reproduced"] is True
    assert cell["worst_filter_ratio"] == pytest.approx(2999.0)


def test_the_measured_good_plan_passes_every_criterion() -> None:
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                index="2_4_river_timeseries_narrow_pkey",
                index_cond=(
                    "((run_key = $7) AND (river_segment_key = $5) "
                    "AND (variable_e = 'q_down'::hydro.river_variable) "
                    "AND (valid_time >= '2026-06-01 00:00:00+00'::timestamp with time zone))"
                ),
                filter_text="((basin_version_key = $4) AND (river_network_version_key = $6))",
                removed=0,
                actual=24,
                hits=27,
            )
        )
    )
    assert cell["passed"] is True
    assert cell["defect_reproduced"] is False


def test_a_seq_scan_on_the_chunk_fails_criterion_1() -> None:
    """"No index at all" must not be confused with "the wrong index"."""
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                node_type="Seq Scan",
                filter_text=(
                    "((run_key = $7) AND (river_segment_key = $5) "
                    "AND (variable_e = 'q_down'::hydro.river_variable))"
                ),
                removed=71976,
                actual=24,
                hits=1200,
            )
        )
    )
    assert cell["criterion_1_segment_identity_bound"] is False
    assert cell["node_types"] == ["Seq Scan"]
    assert cell["index_names"] == []


# ---------------------------------------------------------------------------
# tasks.md 1.3 / 1.4 — the legacy branch and the compressed chunk
# ---------------------------------------------------------------------------


def _legacy_decompress_plan(*, child_index_cond: str, parent_hits: int = 42, child_hits: int = 6) -> dict[str, Any]:
    """The 2026-09-17 receipt's ``shj_nj/legacy`` shape, transcribed."""
    return _root(
        _scan(
            relation=_LEGACY_CHUNK,
            node_type="Custom Scan",
            filter_text=(
                "((valid_time >= '2026-08-18 00:00:00+00'::timestamp with time zone) "
                "AND (run_key = $3) AND (basin_version_key = $0) AND (river_segment_key = $1) "
                "AND (river_network_version_key = $2) AND (variable = 'q_down'::text))"
            ),
            removed=0,
            actual=120,
            hits=parent_hits,
            children=[
                _scan(
                    relation=_LEGACY_COMPRESSED_CHILD,
                    index="compress_hyper_7_104_chunk__compressed_hypertable_7_run_id_rive",
                    index_cond=child_index_cond,
                    filter_text="((_ts_meta_max_2 >= '2026-08-18 00:00:00+00'::timestamp with time zone))",
                    removed=0,
                    actual=1,
                    hits=child_hits,
                )
            ],
        )
    )


def _legacy(plan: dict[str, Any], baseline: int | None = 49) -> dict[str, Any]:
    return evaluate_cell(
        plan,
        chunk_relation=_LEGACY_CHUNK,
        branch="legacy",
        filter_ratio_limit=_FILTER_RATIO_LIMIT,
        shared_hit_baseline=baseline,
    )


def test_a_legacy_decompress_parent_inherits_criterion_1_from_its_compressed_child() -> None:
    """The compressed relation is mapped to the measured chunk STRUCTURALLY.

    Without that mapping the extract is empty and "the compressed condition
    passes for nothing" (tasks.md 1.4). The parent carries every key predicate
    in its ``Filter`` and prunes nothing itself; the child is the node that did.
    """
    cell = _legacy(
        _legacy_decompress_plan(
            child_index_cond=(
                "((run_id = 'fcst_gfs_2026081800'::text) "
                "AND (river_network_version_id = 'basins_shj_nj_rivnet_vbasins'::text) "
                "AND (river_segment_id = 'basins_shj_nj_shud_shud_riv_000001'::text))"
            )
        )
    )
    assert cell["node_count"] == 2
    assert [node["role"] for node in cell["nodes"]] == ["chunk", "compressed_child"]
    assert cell["nodes"][0]["criterion_1_inherited"] is True
    assert cell["criterion_1_segment_identity_bound"] is True
    assert cell["passed"] is True


def test_the_measured_narrow_compressed_shape_passes_and_is_not_extracted_empty() -> None:
    """must-preserve #7, transcribed from ``explain-1987.json``'s
    ``shj_nj/narrow_compressed``: ``_hyper_9_126_chunk`` is a ``Custom Scan``
    whose ``Filter`` does not mention ``river_segment_key`` AT ALL — the pruning
    moved into ``compress_hyper_10_174_chunk``'s
    ``Index Cond ((run_key = $7) AND (river_segment_key = $5))``, one batch, 4
    shared hits, nothing removed. A chunk-name-only extractor sees the parent,
    finds no segment key anywhere, and calls a CORRECT plan a demotion.
    """
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                node_type="Custom Scan",
                filter_text=(
                    "((valid_time >= '2026-08-26 12:00:00+00'::timestamp with time zone) "
                    "AND (basin_version_key = $4) AND (river_network_version_key = $6) "
                    "AND (variable_e = 'q_down'::hydro.river_variable))"
                ),
                removed=0,
                actual=12,
                hits=4,
                children=[
                    _scan(
                        relation="compress_hyper_10_174_chunk",
                        index="compress_hyper_10_174_chunk__compressed_hypertable_10_run_key_r",
                        index_cond="((run_key = $7) AND (river_segment_key = $5))",
                        filter_text=(
                            "((_ts_meta_max_2 >= '2026-08-26 12:00:00+00'::timestamp with time zone))"
                        ),
                        removed=0,
                        actual=1,
                        hits=4,
                    )
                ],
            )
        ),
        baseline=4,
    )
    assert cell["node_count"] == 2
    assert cell["nodes"][0]["identity_in_filter"] is False
    assert cell["nodes"][0]["criterion_1_inherited"] is True
    assert cell["passed"] is True


def test_a_legacy_plan_that_loses_the_segment_id_pruning_reddens() -> None:
    """tasks.md 1.4's "reddens if that pruning is lost", on the legacy shape."""
    cell = _legacy(
        _legacy_decompress_plan(
            child_index_cond="((run_id = 'fcst_gfs_2026081800'::text))",
            parent_hits=3000,
            child_hits=900,
        )
    )
    assert cell["criterion_1_segment_identity_bound"] is False
    assert cell["passed"] is False


def test_river_segment_key_on_a_legacy_node_is_not_a_false_red() -> None:
    """design.md criterion 1: the two branches are not the same predicate.

    ``river_segment_id`` is what legacy binds today, but a plan that binds
    ``river_segment_key`` there is a GOOD plan, and rejecting it would be the
    same false red with the sign flipped.
    """
    assert BRANCH_IDENTITY_COLUMNS["legacy"] == ("river_segment_id", "river_segment_key")
    cell = _legacy(
        _root(
            _scan(
                relation=_LEGACY_CHUNK,
                index="river_ts_segment_time_key_idx",
                index_cond="((river_segment_key = $1) AND (variable_e = 'q_down'::hydro.river_variable))",
                removed=0,
                actual=120,
                hits=49,
            )
        )
    )
    assert cell["criterion_1_segment_identity_bound"] is True


def test_requiring_the_narrow_identity_column_on_a_narrow_node_is_strict() -> None:
    """The legacy relaxation must not leak into the narrow branch."""
    assert BRANCH_IDENTITY_COLUMNS["narrow"] == ("river_segment_key",)
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                index="some_text_index",
                index_cond="((river_segment_id = 'it2451_shud_riv_000001'::text))",
                removed=0,
                actual=24,
                hits=27,
            )
        )
    )
    assert cell["criterion_1_segment_identity_bound"] is False


# ---------------------------------------------------------------------------
# The vacuity guard
# ---------------------------------------------------------------------------


def test_a_plan_that_never_reads_the_measured_chunk_is_a_failure_not_a_pass() -> None:
    cell = _narrow(_root(_scan(relation="_hyper_6_99_chunk", index="whatever", index_cond="(run_key = $7)")))
    assert cell["node_count"] == 0
    assert cell["passed"] is False
    assert any("EMPTY EXTRACT" in failure for failure in cell["failures"]), cell["failures"]


def test_an_unknown_branch_is_refused_rather_than_judged_leniently() -> None:
    with pytest.raises(ValueError, match="unknown branch"):
        extract_cell_nodes(
            _root(_scan(relation=_NARROW_CHUNK)),
            chunk_relation=_NARROW_CHUNK,
            branch="wide",
            filter_ratio_limit=10,
        )


def test_a_bitmap_plan_reads_its_index_cond_off_the_child() -> None:
    """Otherwise every bitmap plan reads as a demotion (wrong-reason guard)."""
    cell = _narrow(
        _root(
            _scan(
                relation=_NARROW_CHUNK,
                node_type="Bitmap Heap Scan",
                filter_text="((basin_version_key = $4))",
                removed=0,
                actual=24,
                hits=27,
                children=[
                    {
                        "Node Type": "Bitmap Index Scan",
                        "Index Name": "2_4_river_timeseries_narrow_pkey",
                        "Index Cond": "((run_key = $7) AND (river_segment_key = $5))",
                        "Actual Rows": 24,
                        "Actual Loops": 1,
                    }
                ],
            )
        )
    )
    assert cell["criterion_1_segment_identity_bound"] is True
    assert cell["index_names"] == ["2_4_river_timeseries_narrow_pkey"]
