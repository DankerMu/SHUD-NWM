"""Tests for the pre-activation station-flag flip hook (Epic #992 SUB-1 §1.1).

Partition (#2527 partition of the 1814-line / 15-case
tests/test_direct_grid_display_cutover_flip.py):
the SUB-1 atomic flip / rollback family (tests 1-9 and the public module
contract of ``StationFlagFlipError``). The harness lives in
``tests/direct_grid_display_cutover_flip_helpers.py``.

Covers the OpenSpec ``station-set-atomic-flip`` §1.1 required evidence
pinned in ``openspec/changes/direct-grid-display-cutover/tasks.md``:

* (1) Commit-time re-pointing: on the engaged happy path, exactly the
  target's mirror rows land ``active_flag=true`` and every other row of
  the ``basin_version`` lands ``false``.
* (2) Forced mid-transaction failure rolls the whole transaction back
  with no ``active_flag`` change persisted (no "previous set off /
  target not on" and no "target on before activation" intermediate).
* (3) Direct→direct′ fix-forward re-flip: with M1 active + M1′ registered
  (both generations' mirror rows coexist in the same ``basin_version``),
  cutover to M1′ ends with only M1′'s mirror rows true and every M1 mirror
  + M0 legacy row false — the committed set is NEVER M1 ∪ M1′.
* (4) With two registered-but-inactive direct-grid generations, cutover
  activates only the target generation's mirror.
* (5) Legacy-target routine activate / switch_version / rollback_version
  leaves every ``met.met_station`` row untouched and records the audited
  skip reason ``target_not_direct_grid``.
* (6) Fresh-basin direct-grid activation with no previous active model
  no-ops with the audited skip reason ``no_previous_active_model`` and
  touches no station row.
* (7) Retired (#2773 item 4): the static lock on the station-MVT source
  query ``_station_source_version`` went with the station vector-tile route
  it served; there is no such query left to lock.

Scenarios (1)-(6) drive
:meth:`packages.common.model_registry.PsycopgModelRegistryStore.model_lifecycle_operation`
end-to-end via a ``_HarnessStore`` subclass (the pattern from
``tests/test_variant_activation_cutover.py`` +
``tests/test_state_clone_index_publish.py``) so the real preflight →
hook-dispatch → transition → audit path exercises the flip hook exactly
as production will.
"""

from __future__ import annotations

import pytest

from packages.common.station_set_flip import (
    SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL,
    SKIP_REASON_TARGET_NOT_DIRECT_GRID,
    StationFlagFlipError,
)
from tests.direct_grid_display_cutover_flip_helpers import (
    GRID_SNAPSHOT_ID,
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
)
from tests.test_variant_activation_cutover import (
    BASIN_VERSION_ID,
    _decision,
    _model_row,
)

# ============================================================================
# (1) Happy path: whole set re-points atomically
# ============================================================================


def test_commit_re_points_whole_set_atomically_target_true_others_false() -> None:
    """§1.1 evidence (1): on commit, exactly target mirrors → true, rest → false.

    Setup: legacy M0 active + two legacy stations active, one M1 target
    with two registered-but-inactive mirrors. After ``activate`` on M1
    commits, the two M1 mirror rows land ``true`` and both legacy rows
    land ``false``.
    """
    inventory = _StationInventory(
        [
            _legacy_row(station_id="synth-station-001", active_flag=True),
            _legacy_row(station_id="synth-station-002", active_flag=True),
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
    store = _FlipHarnessStore(
        [
            _legacy_active_model(),
            _direct_grid_variant(
                model_id="direct_grid_m1",
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
        ],
        inventory,
    )
    audit = _register_hook(store)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-flip-happy",
    )

    assert result["status"] == "allowed"
    assert audit.skips == []  # engaged path — no skip recorded

    by_id = {row.station_id: row for row in inventory.rows}
    # Target M1 mirror rows both land true.
    assert by_id[f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1"].active_flag is True
    assert by_id[f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a2"].active_flag is True
    # Legacy rows both land false.
    assert by_id["synth-station-001"].active_flag is False
    assert by_id["synth-station-002"].active_flag is False

    # Two-step ordering: turn-off THEN turn-on, both against the same
    # basin_version_id. This locks the design-pinned "deterministic
    # starting point" — the whole set turns off before the target set
    # turns on, so no intermediate "M1 ∪ M0" state ever exists.
    cursor = store._transactions[-1]["cursor"]
    turn_off, turn_on = (stmt for stmt, _ in cursor.executed)
    assert turn_off.startswith("UPDATE met.met_station SET active_flag = false")
    assert turn_on.startswith("UPDATE met.met_station SET active_flag = true")
    assert store._transactions[-1]["committed"] is True


# ============================================================================
# (2) Forced failure at flip step rolls back whole tx — no active_flag change
# ============================================================================


def test_forced_failure_at_flip_step_rolls_back_whole_tx_no_active_flag_change() -> (
    None
):
    """§1.1 evidence (2): a forced flip-step raise rolls back the whole tx.

    The fake cursor is configured to raise on the "turn on target"
    UPDATE — the FIRST UPDATE (turn off) has already staged mutations on
    the shared inventory. The atomic-rollback contract (``_FakeTransaction``
    restores the pre-tx snapshot on any raised exception) is what proves
    the rollback covers BOTH statements; no "previous set off / target
    not on" empty-display intermediate ever commits.
    """

    class _InjectedFailure(RuntimeError):
        pass

    inventory = _StationInventory(
        [
            _legacy_row(station_id="synth-station-001", active_flag=True),
            _mirror_row(
                cell_id="cell_a1",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
        ]
    )
    pre_tx = inventory.snapshot()
    store = _FlipHarnessStore(
        [
            _legacy_active_model(),
            _direct_grid_variant(
                model_id="direct_grid_m1",
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
        ],
        inventory,
        raise_on_turn_on=_InjectedFailure("injected flip-step failure"),
    )
    _register_hook(store)

    with pytest.raises(_InjectedFailure, match="injected flip-step failure"):
        store.model_lifecycle_operation(
            "direct_grid_m1",
            operation="activate",
            policy_decision=_decision("models.activate", "direct_grid_m1"),
            request_id="req-flip-rollback",
        )

    # Whole tx rolled back: inventory is byte-for-byte the pre-tx snapshot.
    assert inventory.snapshot() == pre_tx
    # No supersede+activate swap fired either.
    assert store._state_updates == []
    assert store._transactions[-1]["committed"] is False


# ============================================================================
# (3) direct→direct′ fix-forward: only M1′ ends active
# ============================================================================


def test_fix_forward_direct_to_direct_prime_reflip_only_target_generation_active() -> (
    None
):
    """§1.1 evidence (3): re-flip lands ONLY M1′ mirrors true; NEVER M1 ∪ M1′.

    Both generations' mirror rows coexist in the same ``basin_version``
    (Change 4 admits multiple built generations per grain). M1 is
    currently active; M1′ is registered inactive. After ``switch_version``
    on M1′, only M1′ mirrors are true and every M1 mirror + M0 legacy
    row is false. The committed set is NEVER the union.
    """
    inventory = _StationInventory(
        [
            # Pre-flip legacy row (irrelevant to display now, but locks
            # the "every other row → false" invariant).
            _legacy_row(station_id="synth-station-001", active_flag=False),
            # M1 mirror rows currently ACTIVE (M1 is the outgoing generation).
            _mirror_row(
                cell_id="cell_a1",
                active_flag=True,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_a2",
                active_flag=True,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            # M1′ mirror rows registered but INACTIVE (Change 4 shadow
            # rows, per docs §8.1 registration invariant).
            _mirror_row(
                cell_id="cell_a1",
                active_flag=False,
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_a2",
                active_flag=False,
                model_input_package_id=M1_PRIME_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_PRIME_BINDING_CHECKSUM,
            ),
        ]
    )
    store = _FlipHarnessStore(
        [
            _direct_grid_variant(
                model_id="direct_grid_m1",
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
                active_flag=True,
                lifecycle_state="active",
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
        "direct_grid_m1prime",
        operation="switch_version",
        policy_decision=_decision("models.switch_version", "direct_grid_m1prime"),
        request_id="req-flip-fix-forward",
    )

    assert result["status"] == "allowed"
    by_id = {row.station_id: row for row in inventory.rows}
    # M1′ mirrors ON.
    assert by_id[
        f"{M1_PRIME_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1"
    ].active_flag is True
    assert by_id[
        f"{M1_PRIME_MODEL_INPUT_PACKAGE_ID}::cell:cell_a2"
    ].active_flag is True
    # M1 mirrors OFF (formerly active).
    assert by_id[f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1"].active_flag is False
    assert by_id[f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a2"].active_flag is False
    # Legacy row stays OFF.
    assert by_id["synth-station-001"].active_flag is False

    # Explicit "never M1 ∪ M1′" assertion: no row exists whose
    # active_flag=true across BOTH generations. The committed active set
    # is exactly the M1′ set.
    active_rows = [row for row in inventory.rows if row.active_flag]
    active_ids = {row.station_id for row in active_rows}
    assert active_ids == {
        f"{M1_PRIME_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1",
        f"{M1_PRIME_MODEL_INPUT_PACKAGE_ID}::cell:cell_a2",
    }


# ============================================================================
# (4) Two registered-but-inactive generations → only target activates
# ============================================================================


def test_two_registered_but_inactive_generations_only_target_activated() -> None:
    """§1.1 evidence (3-b): two inactive generations, cutover activates only target.

    Both direct-grid generations are registered inactive (no cutover
    has happened yet — this is a first-time activation of M1). Legacy M0
    is currently the active model. After ``activate`` on M1, only M1's
    mirrors are true; M1′'s registered-inactive mirrors stay false; the
    legacy row goes false.
    """
    inventory = _StationInventory(
        [
            _legacy_row(station_id="synth-station-001", active_flag=True),
            _mirror_row(
                cell_id="cell_a1",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
            _mirror_row(
                cell_id="cell_a1",
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
        request_id="req-flip-two-generations",
    )

    assert result["status"] == "allowed"
    by_id = {row.station_id: row for row in inventory.rows}
    assert by_id[f"{M1_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1"].active_flag is True
    # M1′'s registered-inactive mirror stays inactive (unlike M1's
    # matching row, its ``binding_checksum`` doesn't match the target's
    # WHERE predicate, so step 2 does not flip it on).
    assert (
        by_id[f"{M1_PRIME_MODEL_INPUT_PACKAGE_ID}::cell:cell_a1"].active_flag is False
    )
    assert by_id["synth-station-001"].active_flag is False


# ============================================================================
# (5) Legacy target activation no-ops with target_not_direct_grid skip
# ============================================================================


def test_legacy_target_activation_legacy_noop_with_target_not_direct_grid_skip_reason() -> (
    None
):
    """§1.1 evidence: routine legacy-target op leaves every row untouched.

    Two legacy models with a currently-active baseline; a routine
    ``switch_version`` to the other legacy model must NOT touch any
    station row. Audit records the ``target_not_direct_grid`` skip.
    This is the invariant that keeps the 13 production basins' station
    layers safe across their routine lifecycle ops.
    """
    inventory = _StationInventory(
        [
            _legacy_row(station_id="synth-station-001", active_flag=True),
            _legacy_row(station_id="synth-station-002", active_flag=True),
            _legacy_row(station_id="synth-station-003", active_flag=False),
        ]
    )
    pre_snapshot = inventory.snapshot()
    store = _FlipHarnessStore(
        [
            _legacy_active_model(),
            _model_row(
                model_id="legacy_m0_next",
                active_flag=False,
                lifecycle_state="inactive",
            ),
        ],
        inventory,
    )
    audit = _register_hook(store)

    result = store.model_lifecycle_operation(
        "legacy_m0_next",
        operation="switch_version",
        policy_decision=_decision("models.switch_version", "legacy_m0_next"),
        request_id="req-flip-legacy-noop",
    )

    assert result["status"] == "allowed"
    # No station row touched by the flip — the audit skip is what fires.
    assert inventory.snapshot() == pre_snapshot
    assert audit.skips == [
        {
            "reason": SKIP_REASON_TARGET_NOT_DIRECT_GRID,
            "basin_version_id": BASIN_VERSION_ID,
            "target_model_id": "legacy_m0_next",
        }
    ]
    # No SQL was ever issued against met.met_station on the skip path.
    cursor = store._transactions[-1]["cursor"]
    assert cursor.executed == []
    # Positive lifecycle-commit assertion (fold Note 1): the skip must be
    # a hook-level NO-OP inside a SUCCESSFUL transaction — the target still
    # transitioned to ``active`` and the previous active model was
    # ``superseded``. Sharpens the "no-op vs abort" distinction beyond the
    # ``status == "allowed"`` check.
    assert ("legacy_m0_next", "active", True) in store._state_updates
    assert ("legacy_m0", "superseded", False) in store._state_updates
    assert store._transactions[-1]["committed"] is True


# ============================================================================
# (6) Fresh basin: no previous active model → no_previous_active_model skip
# ============================================================================


def test_no_previous_active_model_no_ops_with_no_previous_active_model_skip_reason() -> (
    None
):
    """§1.1 evidence: fresh-basin direct-grid activation records the audited skip.

    Only the direct-grid variant is registered (no previous active
    model exists on the basin). ``activate`` MUST skip audibly with
    ``no_previous_active_model`` and touch no station row — Change 7
    (batch-rollout) owns fresh-basin first-display bring-up; this
    change's flip hook is a strict cutover mechanism.
    """
    inventory = _StationInventory(
        [
            _mirror_row(
                cell_id="cell_a1",
                active_flag=False,
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
        ]
    )
    pre_snapshot = inventory.snapshot()
    store = _FlipHarnessStore(
        [
            _direct_grid_variant(
                model_id="direct_grid_m1",
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
        ],
        inventory,
    )
    audit = _register_hook(store)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-flip-fresh-basin",
    )

    assert result["status"] == "allowed"
    assert inventory.snapshot() == pre_snapshot
    assert audit.skips == [
        {
            "reason": SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL,
            "basin_version_id": BASIN_VERSION_ID,
            "target_model_id": "direct_grid_m1",
        }
    ]
    cursor = store._transactions[-1]["cursor"]
    assert cursor.executed == []
    # Positive lifecycle-commit assertion (fold Note 1): fresh-basin
    # activation is a hook-skip WITHIN a successful transaction — the
    # target still transitions to ``active``. No previous active model
    # exists, so no ``superseded`` update is expected. This sharpens the
    # "hook no-op vs whole-op abort" distinction.
    assert ("direct_grid_m1", "active", True) in store._state_updates
    assert store._transactions[-1]["committed"] is True


# ============================================================================
# (9) Fail-closed rowcount==0 end-to-end: direct-grid target with NO
# registered mirror rows raises StationFlagFlipError and rolls back the
# whole activation transaction (no state updates, station rows unchanged).
# ============================================================================


def test_direct_grid_target_with_no_registered_mirrors_raises_station_flag_flip_error_and_rolls_back() -> (  # noqa: E501
    None
):
    """§1.1 fail-closed evidence: rowcount==0 on step 2 aborts the whole tx.

    Setup: a legacy M0 is currently active (so the hook engages — the
    ``no_previous_active_model`` skip does NOT fire), and a direct-grid
    M1 target is registered with a well-formed
    ``resource_profile.direct_grid_forcing`` (so the classifier engages —
    the ``target_not_direct_grid`` skip does NOT fire). However, ZERO
    mirror rows exist for M1's ``(model_input_package_id,
    binding_checksum, grid_snapshot_id)`` triple; the only rows are
    legacy ``forcing_proxy`` rows.

    Under this contract, step 1 (``UPDATE ... SET active_flag=false``)
    turns off the legacy rows, and step 2 (``UPDATE ... SET
    active_flag=true`` matched against the target identity) matches zero
    rows. The hook MUST raise :class:`StationFlagFlipError`; Change 4's
    dispatcher lets it propagate; the whole transaction rolls back and
    the pre-tx station rows are restored byte-for-byte. No
    ``_state_updates`` fire (the transition never runs), and the audit
    row never commits.

    This is the end-to-end lock on the "no empty-display window ever
    commits" invariant — a direct-grid target with no registered mirrors
    is a Change-4 registration invariant violation (Epic #961 SUB-2
    registers mirrors atomically with the ``core.model_instance`` row
    insert), and this test proves the pre-activation transaction
    fail-closes fully on it.
    """
    inventory = _StationInventory(
        [
            _legacy_row(station_id="synth-station-001", active_flag=True),
            _legacy_row(station_id="synth-station-002", active_flag=True),
            # Intentionally NO ``direct_grid_cache`` mirror rows for M1 —
            # this is the Change-4 registration invariant violation the
            # hook fail-closes on.
        ]
    )
    pre_tx_snapshot = inventory.snapshot()
    store = _FlipHarnessStore(
        [
            _legacy_active_model(),
            _direct_grid_variant(
                model_id="direct_grid_m1",
                model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
                binding_checksum=M1_BINDING_CHECKSUM,
            ),
        ],
        inventory,
    )
    audit = _register_hook(store)

    with pytest.raises(StationFlagFlipError) as exc_info:
        store.model_lifecycle_operation(
            "direct_grid_m1",
            operation="activate",
            policy_decision=_decision("models.activate", "direct_grid_m1"),
            request_id="req-flip-rowcount-zero",
        )

    # The error carries basin_version_id + target model id + the 3
    # identity discriminators the module docstring pins.
    err = exc_info.value
    assert err.basin_version_id == BASIN_VERSION_ID
    assert err.target_model_id == "direct_grid_m1"
    assert err.model_input_package_id == M1_MODEL_INPUT_PACKAGE_ID
    assert err.binding_checksum == M1_BINDING_CHECKSUM
    assert err.grid_snapshot_id == GRID_SNAPSHOT_ID

    # Atomic rollback: station rows are byte-for-byte the pre-tx snapshot
    # (step 1's turn-off has been undone by the transaction rollback).
    assert inventory.snapshot() == pre_tx_snapshot
    # Lifecycle transition never ran — the hook aborted the tx BEFORE
    # ``_apply_model_lifecycle_transition`` was called, so no supersede+
    # activate swap fired.
    assert store._state_updates == []
    # Transaction observed the exception and did not commit.
    assert store._transactions[-1]["committed"] is False
    # The engaged path emits no audit skip (skip is only for gates 1 & 2
    # — the classifier engaged and the previous-active-model was present).
    assert audit.skips == []


# ============================================================================
# Defensive: the module also exposes StationFlagFlipError for the
# fail-closed rowcount branch (invoked when a direct-grid target has no
# registered mirror rows — a Change-4 registration invariant violation).
# Test (9) above locks the end-to-end rollback behavior; this test locks
# the public module contract so downstream tests can import the exception.
# ============================================================================


def test_station_flag_flip_error_is_public_module_contract() -> None:
    """The fail-closed exception is importable from the module.

    Downstream evidence and change-verification tests will key off
    :class:`StationFlagFlipError` to distinguish a mirror-absence
    failure from other pre-activation rollbacks. Locking the public
    export here prevents a silent rename.
    """
    assert issubclass(StationFlagFlipError, RuntimeError)
    err = StationFlagFlipError(
        basin_version_id=BASIN_VERSION_ID,
        target_model_id="direct_grid_m1",
        model_input_package_id=M1_MODEL_INPUT_PACKAGE_ID,
        binding_checksum=M1_BINDING_CHECKSUM,
        grid_snapshot_id=GRID_SNAPSHOT_ID,
    )
    assert err.basin_version_id == BASIN_VERSION_ID
    assert err.target_model_id == "direct_grid_m1"
    assert err.model_input_package_id == M1_MODEL_INPUT_PACKAGE_ID
    assert err.binding_checksum == M1_BINDING_CHECKSUM
    assert err.grid_snapshot_id == GRID_SNAPSHOT_ID
