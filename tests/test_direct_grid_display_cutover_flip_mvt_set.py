"""Tests for the station-MVT no-mixed-display invariant (Epic #992 SUB-2 §1.2).

Partition (#2527 partition of the 1814-line / 15-case
tests/test_direct_grid_display_cutover_flip.py):
the SUB-2 MVT station-set family -- shadow-period, pre-cutover exclusion,
post-cutover exclusion, never-union and feature-budget non-mixing -- plus the
``_mvt_station_set`` model of the station-MVT layer's row selection. The flip
harness these tests drive lives in
``tests/direct_grid_display_cutover_flip_helpers.py``; the structural lock on
``apps/api/routes/hydro_display_identity.py::_station_source_version`` that
``_mvt_station_set`` mirrors ("test 7 above") lives in
``tests/test_direct_grid_display_cutover_flip_atomic.py``.

Covers the OpenSpec §1.2 required evidence pinned in
``openspec/changes/direct-grid-display-cutover/tasks.md``.
"""

from __future__ import annotations

from tests.direct_grid_display_cutover_flip_helpers import (
    M1_BINDING_CHECKSUM,
    M1_MODEL_INPUT_PACKAGE_ID,
    M1_PRIME_BINDING_CHECKSUM,
    M1_PRIME_MODEL_INPUT_PACKAGE_ID,
    _direct_grid_variant,
    _FlipHarnessStore,
    _legacy_active_model,
    _legacy_row,
    _mirror_row,
    _register_hook,
    _StationInventory,
    _StationRow,
)
from tests.test_variant_activation_cutover import (
    BASIN_VERSION_ID,
    _decision,
)

# ============================================================================
# Epic #992 SUB-2 (#994) — §1.2 no-mixed-display invariant closure
#
# These tests extend the §1.1 flip-hook baseline (tests 1-9 above) with four
# invariant families covering the station-MVT layer's committed-instant
# behavior across the direct-grid cutover lifecycle window:
#
#   (a) shadow-period: mirrors registered by Change 4 stay
#       ``active_flag=false`` from mint through the pre-cutover window; if
#       any shadow mirror is ever ``true``, the MVT layer emits a mixed
#       display (load-bearing lock).
#   (b) pre-cutover exclusion: the station-MVT layer returns only the
#       previously active set; every mirror generation is excluded, no
#       matter how many are registered.
#   (c) post-cutover exclusion: after cutover, the MVT layer returns only
#       the target generation's cell stations (zero legacy rows AND zero
#       non-target-generation mirror rows).
#   (d) never-union / feature-budget non-mixing: at no committed instant is
#       the emitted MVT set the union of two station sets — including
#       across a direct→direct′ re-flip. The feature-budget invariant is
#       "never mix" (design §Decision 1), not "raise the budget", so the
#       observed emitted-set size never exceeds the largest single-set
#       size.
#
# "MVT layer returns" is modeled by :func:`_mvt_station_set` below — it
# mirrors ``apps/api/routes/hydro_display_identity.py::_station_source_version``'s
# row-selection predicate (locked byte-for-byte by test 7 above): the set
# of ``met.met_station`` rows where ``basin_version_id`` matches the
# scope AND ``active_flag`` is true. SUB-2 tests seed the fake inventory,
# optionally drive the SUB-1 flip hook end-to-end via the same
# :class:`_FlipHarnessStore` the SUB-1 tests use, then assert exact-shape
# set equality on the MVT-layer output. Exact-shape equality (rather than
# membership) is required because a stray mirror row that snuck into the
# emitted set would pass a membership check while breaking the
# never-union invariant.
# ============================================================================


def _mvt_station_set(
    inventory: _StationInventory,
    basin_version_id: str = BASIN_VERSION_ID,
) -> set[str]:
    """Return the set of ``station_id``s the station-MVT layer would emit.

    Mirrors ``apps/api/routes/hydro_display_identity.py::_station_source_version``'s
    row-selection predicate (locked by test 7 above): only rows where
    ``basin_version_id`` matches the scope AND ``active_flag`` is true.
    No role filter, no ``model_id`` filter — single-track visibility is
    delivered by the SUB-1 flip hook's row selection, not by the query.

    SUB-2 tests read this to assert exact-shape MVT set equality at each
    committed instant of the cutover lifecycle.
    """
    return {
        row.station_id
        for row in inventory.rows
        if row.basin_version_id == basin_version_id and row.active_flag
    }


# --- SUB-2 shadow-period family --------------------------------------------


def test_shadow_period_mirror_stays_inactive_flag_false_regression_lock() -> None:
    """§1.2 evidence (shadow): registered shadow mirrors stay excluded from MVT.

    Change 4 (Epic #961 SUB-2 #963) MINTs direct-grid mirror rows
    ``active_flag=false`` at registration and never flips them true
    outside the SUB-1 pre-activation flip. Before any cutover has fired,
    the MVT layer's row-selection (``basin_version_id + active_flag=true``)
    excludes every shadow mirror regardless of how many are registered.

    Fixture: 2 legacy rows active + 2 M1 shadow mirrors registered inactive.
    No lifecycle op has run — this is the pure shadow window.
    Assert: the MVT set equals exactly the legacy set (exact-shape
    equality — a stray mirror would break the ``==`` assertion, not just
    a membership check).
    """
    legacy_a = "synth-station-001"
    legacy_b = "synth-station-002"
    mirror_a = f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1"
    mirror_b = f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a2"

    inventory = _StationInventory(
        [
            _legacy_row(station_id=legacy_a, active_flag=True),
            _legacy_row(station_id=legacy_b, active_flag=True),
            _mirror_row(
                cell_id="cell_a1",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_a2",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
        ]
    )

    # Both shadow mirrors are inactive by construction — regression-lock
    # the intent that Change 4 mint MUST NOT set active_flag=true. This
    # inline invariant is what mutation C ("mint accidentally writes
    # active_flag=true") flips, and the exact-shape assertion below
    # catches the resulting mixed emission.
    mirror_flags = {
        row.station_id: row.active_flag
        for row in inventory.rows
        if row.station_role == "direct_grid_cache"
    }
    assert mirror_flags == {mirror_a: False, mirror_b: False}

    # Exact-shape MVT set: only the legacy rows are emitted. Any leaked
    # shadow mirror would break the ``==`` assertion (both a membership
    # miss AND a size miss).
    assert _mvt_station_set(inventory) == {legacy_a, legacy_b}


def test_shadow_mirror_registered_flag_false_fails_closed_if_ever_true() -> None:
    """§1.2 evidence (shadow, load-bearing): the flag-false invariant IS what
    keeps the MVT set clean during shadow.

    Simulates a Change 4 SUB-2 (#963) registration bug that accidentally
    minted ONE shadow mirror ``active_flag=true``. The MVT layer's
    row-selection is byte-for-byte the SUB-1-locked query — it filters
    ONLY on ``basin_version_id + active_flag=true``, so the leaked
    mirror lands in the emitted set (a mixed display commits).

    The load-bearing assertions here are:

    * The mutant mirror IS in the emitted set → the MVT layer does NOT
      independently exclude ``direct_grid_cache`` rows by role. If a
      future refactor added a role filter to the query, this test would
      fail — false confidence that shadow-mint safety is redundant.
    * The well-behaved sibling mirror stays excluded → the exclusion is
      purely flag-based, not identity-based.
    * The exact-shape emitted set is ``{legacy_a, legacy_b, mutant}``
      → mixed display, exactly witnessed. Any change that either dropped
      the mutant OR pulled additional rows in would break the equality.

    Together these assertions prove that the shadow-period contract
    (``active_flag=false`` at mint, kept ``false`` through the pre-cutover
    window) is REQUIRED for MVT cleanliness — not a redundant belt-and-
    braces. The test PASSES today (assertion holds under the current
    query) and would keep passing after the future Change 4 mirror-
    registration invariant lock is added; what it locks is the MVT
    layer's non-role-filtering behavior that MAKES the shadow contract
    load-bearing.
    """
    legacy_a = "synth-station-001"
    legacy_b = "synth-station-002"
    mutant_mirror = f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1"
    well_behaved_mirror = f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a2"

    inventory = _StationInventory(
        [
            _legacy_row(station_id=legacy_a, active_flag=True),
            _legacy_row(station_id=legacy_b, active_flag=True),
            # Mutant shadow mirror: simulated Change 4 SUB-2 mint bug that
            # wrote active_flag=true instead of the invariant false.
            _mirror_row(
                cell_id="cell_a1",
                active_flag=True,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            # Sibling mirror registered correctly.
            _mirror_row(
                cell_id="cell_a2",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
        ]
    )

    emitted = _mvt_station_set(inventory)

    # Load-bearing #1: mutant leaks into emission (mixed display occurs).
    assert mutant_mirror in emitted
    # Load-bearing #2: sibling stays excluded (purely flag-driven).
    assert well_behaved_mirror not in emitted
    # Load-bearing #3: exact-shape mixed-display set witnessed.
    assert emitted == {legacy_a, legacy_b, mutant_mirror}


# --- SUB-2 pre-cutover exclusion family ------------------------------------


def test_pre_cutover_mvt_set_excludes_mixed_mirror_generations() -> None:
    """§1.2 evidence (pre-cutover, mixed generations): MVT excludes ALL
    mirror generations before any cutover fires.

    Fixture: 2 legacy active + 4 mirrors across 2 direct-grid generations
    (M1 × 2 + M1′ × 2), all mirrors inactive. No lifecycle op has run.
    Assert: MVT set equals exactly the legacy set — every mirror
    generation is excluded, regardless of how many are registered
    (Change 4 admits multiple built generations per grain, docs §8.1).
    """
    legacy_a = "synth-station-001"
    legacy_b = "synth-station-002"

    inventory = _StationInventory(
        [
            _legacy_row(station_id=legacy_a, active_flag=True),
            _legacy_row(station_id=legacy_b, active_flag=True),
            # M1 shadow mirrors.
            _mirror_row(
                cell_id="cell_a1",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_a2",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            # M1′ shadow mirrors (distinct built asset identity).
            _mirror_row(
                cell_id="cell_b1",
                active_flag=False,
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_b2",
                active_flag=False,
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
        ]
    )

    assert _mvt_station_set(inventory) == {legacy_a, legacy_b}


# --- SUB-2 post-cutover exclusion family -----------------------------------


def test_post_cutover_mvt_set_excludes_legacy_and_non_target_generations() -> None:
    """§1.2 evidence (post-cutover): after cutover, MVT excludes both legacy
    rows AND non-target-generation mirrors.

    Fixture: 2 legacy (active) + 2 M1 mirrors + 2 M1′ mirrors, all
    mirrors inactive. Drive the SUB-1 flip hook end-to-end via
    ``_FlipHarnessStore.model_lifecycle_operation(activate, direct_grid_m1)``.
    Assert: post-commit MVT set equals exactly the M1 mirror station_ids
    — zero legacy rows, zero M1′ mirror rows.
    """
    legacy_a = "synth-station-001"
    legacy_b = "synth-station-002"
    m1_cell_a = f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1"
    m1_cell_b = f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a2"

    inventory = _StationInventory(
        [
            _legacy_row(station_id=legacy_a, active_flag=True),
            _legacy_row(station_id=legacy_b, active_flag=True),
            _mirror_row(
                cell_id="cell_a1",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_a2",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            # M1′ shadow mirrors — MUST stay inactive across the flip.
            _mirror_row(
                cell_id="cell_b1",
                active_flag=False,
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_b2",
                active_flag=False,
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
        ]
    )
    store = _FlipHarnessStore(
        [
            _legacy_active_model(),
            _direct_grid_variant(
                model_id="direct_grid_m1",
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _direct_grid_variant(
                model_id="direct_grid_m1prime",
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
        ],
        inventory,
    )
    _register_hook(store)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-post-cutover-single-generation",
    )
    assert result["status"] == "allowed"
    assert store._transactions[-1]["committed"] is True

    # Exact-shape post-cutover MVT set: only M1's mirror station_ids. Any
    # leaked legacy row or M1′ mirror would break the ``==`` assertion.
    assert _mvt_station_set(inventory) == {m1_cell_a, m1_cell_b}


# --- SUB-2 never-union family ----------------------------------------------


def test_never_union_mvt_set_across_direct_to_direct_prime_reflip_no_mixed_display() -> (  # noqa: E501
    None
):
    """§1.2 evidence (never-union): across direct→direct′ re-flip, no
    committed instant is the union of two station sets.

    This is the fix-forward variant that goes BEYOND SUB-1 test 3
    (single re-flip). Sequence:

    1. Seed 2 legacy (active) + 2 M1 mirrors + 2 M1′ mirrors (all
       mirrors inactive).
    2. Snapshot 0 (initial shadow): MVT = ``{legacy_a, legacy_b}``.
    3. Drive activate(M1). Snapshot 1: MVT = ``{m1_cell_a1, m1_cell_a2}``.
    4. Drive switch_version(M1′). Snapshot 2:
       MVT = ``{m1prime_cell_b1, m1prime_cell_b2}``.

    Never-union check: at no snapshot does the MVT set contain rows
    from more than one of ``{legacy_set, m1_set, m1prime_set}`` at once.
    The three sets are pair-wise disjoint by construction (distinct
    station_id prefixes and cell IDs), so a pairwise intersection check
    is exact.

    A "committed-instant recorder" pattern is used: snapshots are taken
    AFTER each successful ``model_lifecycle_operation`` return, i.e.
    after the transaction commit that the harness's ``_FakeTransaction``
    delivers. No snapshot represents an intermediate uncommitted state
    (the atomic-flip contract from SUB-1 test 2 guarantees intermediates
    never commit).
    """
    legacy_a = "synth-station-001"
    legacy_b = "synth-station-002"
    m1_cell_a = f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1"
    m1_cell_b = f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a2"
    m1prime_cell_a = f"{M1_PRIME_MODEL_INPUT_PACKAGE_ID}::cell:cell_b1"
    m1prime_cell_b = f"{M1_PRIME_MODEL_INPUT_PACKAGE_ID}::cell:cell_b2"

    legacy_set = {legacy_a, legacy_b}
    m1_set = {m1_cell_a, m1_cell_b}
    m1prime_set = {m1prime_cell_a, m1prime_cell_b}

    # Sanity: the three station sets are pair-wise disjoint by
    # construction (mapping-asset-identity prefix + distinct cell IDs).
    # This makes the pairwise-intersection never-union check exact.
    assert legacy_set.isdisjoint(m1_set)
    assert legacy_set.isdisjoint(m1prime_set)
    assert m1_set.isdisjoint(m1prime_set)

    inventory = _StationInventory(
        [
            _legacy_row(station_id=legacy_a, active_flag=True),
            _legacy_row(station_id=legacy_b, active_flag=True),
            _mirror_row(
                cell_id="cell_a1",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_a2",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_b1",
                active_flag=False,
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_b2",
                active_flag=False,
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
        ]
    )
    store = _FlipHarnessStore(
        [
            _legacy_active_model(),
            _direct_grid_variant(
                model_id="direct_grid_m1",
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _direct_grid_variant(
                model_id="direct_grid_m1prime",
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
        ],
        inventory,
    )
    _register_hook(store)

    # Snapshot 0 — initial shadow window, no lifecycle op has fired.
    snapshots: list[set[str]] = [_mvt_station_set(inventory)]

    # Cutover 1: legacy → M1.
    result_activate = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-never-union-cutover-1",
    )
    assert result_activate["status"] == "allowed"
    assert store._transactions[-1]["committed"] is True
    snapshots.append(_mvt_station_set(inventory))

    # Cutover 2: M1 → M1′ (fix-forward re-flip).
    result_switch = store.model_lifecycle_operation(
        "direct_grid_m1prime",
        operation="switch_version",
        policy_decision=_decision("models.switch_version", "direct_grid_m1prime"),
        request_id="req-never-union-cutover-2",
    )
    assert result_switch["status"] == "allowed"
    assert store._transactions[-1]["committed"] is True
    snapshots.append(_mvt_station_set(inventory))

    # Exact-shape per-instant MVT set at every committed instant.
    assert snapshots[0] == legacy_set
    assert snapshots[1] == m1_set
    assert snapshots[2] == m1prime_set

    # Never-union invariant: no snapshot intersects more than one of the
    # three disjoint sets. Framed pair-wise so the failure message points
    # at the exact pair that leaked.
    for i, snapshot in enumerate(snapshots):
        assert not (
            snapshot & legacy_set and snapshot & m1_set
        ), f"snapshot {i} unions legacy ∪ M1: {snapshot!r}"
        assert not (
            snapshot & legacy_set and snapshot & m1prime_set
        ), f"snapshot {i} unions legacy ∪ M1′: {snapshot!r}"
        assert not (
            snapshot & m1_set and snapshot & m1prime_set
        ), f"snapshot {i} unions M1 ∪ M1′: {snapshot!r}"


# --- SUB-2 feature-budget non-mixing family --------------------------------


def test_feature_budget_never_fed_union_across_lifecycle_window() -> None:
    """§1.2 evidence (feature-budget non-mixing): the MVT feature budget
    is never fed a mixed set across the full lifecycle window.

    Design §Decision 1 pins the invariant as "never mix", not "raise the
    budget". Modeling the MVT feature-budget consumer as
    ``len(mvt_set)``, we assert that at every committed instant across
    the lifecycle sequence (shadow → activate(M1) → switch_version(M1′)),
    the emitted-set size never exceeds the LARGEST single-set size.
    Since the three station sets are disjoint by construction, this
    ``len <= max_single_size`` bound is provably true iff no union is
    emitted (a mixed emission would necessarily add elements from a
    second set, pushing ``len`` above ``max_single_size``).

    Fixture: 3 legacy stations + 5 M1 mirrors + 5 M1′ mirrors.
    Sequence: shadow (len=3) → activate(M1) (len=5) → switch_version(M1′)
    (len=5). Bound: ``max(3, 5, 5) == 5`` — no observed len exceeds 5,
    so no union was ever emitted.

    Additional assertion: len at each phase equals the target set's
    size EXACTLY (not merely ``<= 5``). This closes a subtle loophole —
    a partial mixed emission (e.g., 4 rows from M1 + 1 legacy leak) has
    ``len=5`` too, but it's mixed. Asserting exact per-phase length AND
    exact set equality forecloses that.
    """
    legacy_ids = [f"synth-station-legacy-{i}" for i in range(1, 4)]  # 3
    m1_cell_ids = [f"cell_a{i}" for i in range(1, 6)]  # 5
    m1prime_cell_ids = [f"cell_b{i}" for i in range(1, 6)]  # 5

    m1_ids = {f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:{c}" for c in m1_cell_ids}
    m1prime_ids = {
        f"{M1_PRIME_MODEL_INPUT_PACKAGE_ID}::cell:{c}" for c in m1prime_cell_ids
    }
    legacy_set = set(legacy_ids)

    assert len(legacy_set) == 3
    assert len(m1_ids) == 5
    assert len(m1prime_ids) == 5
    # Pair-wise disjoint by construction.
    assert legacy_set.isdisjoint(m1_ids)
    assert legacy_set.isdisjoint(m1prime_ids)
    assert m1_ids.isdisjoint(m1prime_ids)
    max_single_size = max(len(legacy_set), len(m1_ids), len(m1prime_ids))
    assert max_single_size == 5

    rows: list[_StationRow] = [
        _legacy_row(station_id=sid, active_flag=True) for sid in legacy_ids
    ]
    rows.extend(
        _mirror_row(
            cell_id=c,
            active_flag=False,
            model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
            binding_checksum=M1_BINDING_CHECKSUM,
        )
        for c in m1_cell_ids
    )
    rows.extend(
        _mirror_row(
            cell_id=c,
            active_flag=False,
            model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
            binding_checksum=M1_PRIME_BINDING_CHECKSUM,
        )
        for c in m1prime_cell_ids
    )
    inventory = _StationInventory(rows)
    store = _FlipHarnessStore(
        [
            _legacy_active_model(),
            _direct_grid_variant(
                model_id="direct_grid_m1",
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _direct_grid_variant(
                model_id="direct_grid_m1prime",
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
        ],
        inventory,
    )
    _register_hook(store)

    # Phase 0 — shadow window.
    snapshots: list[set[str]] = [_mvt_station_set(inventory)]

    # Phase 1 — activate(M1).
    assert (
        store.model_lifecycle_operation(
            "direct_grid_m1",
            operation="activate",
            policy_decision=_decision("models.activate", "direct_grid_m1"),
            request_id="req-feature-budget-phase-1",
        )["status"]
        == "allowed"
    )
    assert store._transactions[-1]["committed"] is True
    snapshots.append(_mvt_station_set(inventory))

    # Phase 2 — switch_version(M1′).
    assert (
        store.model_lifecycle_operation(
            "direct_grid_m1prime",
            operation="switch_version",
            policy_decision=_decision(
                "models.switch_version", "direct_grid_m1prime"
            ),
            request_id="req-feature-budget-phase-2",
        )["status"]
        == "allowed"
    )
    assert store._transactions[-1]["committed"] is True
    snapshots.append(_mvt_station_set(inventory))

    # Feature-budget non-mixing bound: len never exceeds the largest
    # single-set size across the lifecycle window. Union would push len
    # above 5 (any mixed set adds rows from a second disjoint set).
    for i, snapshot in enumerate(snapshots):
        assert len(snapshot) <= max_single_size, (
            f"snapshot {i} exceeds feature-budget non-mixing bound "
            f"(len={len(snapshot)} > {max_single_size}); mixed set leaked: "
            f"{snapshot!r}"
        )

    # Exact per-phase size (rules out a partial mixed emission that
    # accidentally still fits under max_single_size).
    assert [len(s) for s in snapshots] == [3, 5, 5]

    # Exact per-phase set equality (final tightener — the emitted set at
    # each phase is exactly the target single-set, never a partial mix).
    assert snapshots[0] == legacy_set
    assert snapshots[1] == m1_ids
    assert snapshots[2] == m1prime_ids
