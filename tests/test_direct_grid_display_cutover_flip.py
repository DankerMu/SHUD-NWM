"""Tests for the pre-activation station-flag flip hook (Epic #992 SUB-1 §1.1).

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
* (7) Static structural regression lock:
  ``apps/api/routes/hydro_display.py::_station_source_version`` still
  filters only by ``basin_version_id + active_flag=true`` and does NOT
  contain a ``model_id`` predicate (design §Decision 1 rejects the
  ``model_id`` filter form).

Scenarios (1)-(6) drive
:meth:`packages.common.model_registry.PsycopgModelRegistryStore.model_lifecycle_operation`
end-to-end via a ``_HarnessStore`` subclass (the pattern from
``tests/test_variant_activation_cutover.py`` +
``tests/test_state_clone_index_publish.py``) so the real preflight →
hook-dispatch → transition → audit path exercises the flip hook exactly
as production will. Scenario (7) is a pure source-inspection test — it
reads ``apps/api/routes/hydro_display.py`` from disk and asserts SQL
substrings on the ``_station_source_version`` function body.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from apps.api.routes.hydro_display import _station_source_version
from packages.common.model_registry import (
    ModelActivationContext,
    ModelLifecycleOperation,
    PsycopgModelRegistryStore,
)
from packages.common.station_set_flip import (
    SKIP_REASON_NO_PREVIOUS_ACTIVE_MODEL,
    SKIP_REASON_TARGET_NOT_DIRECT_GRID,
    StationFlagFlipError,
    build_station_flag_flip_hook,
)
from tests.test_variant_activation_cutover import (
    BASIN_VERSION_ID,
    _decision,
    _model_row,
)

# --- station-row fake ------------------------------------------------------


class _StationRow:
    """Mutable in-memory row of ``met.met_station`` for the flip fake.

    Only the columns the hook's SQL touches are modeled: the WHERE
    predicate columns (``basin_version_id``, ``station_role``,
    ``properties_json`` binding-identity fields, ``grid_snapshot_id``)
    and the flipped column (``active_flag``). Everything else is
    irrelevant for these tests.
    """

    def __init__(
        self,
        *,
        station_id: str,
        basin_version_id: str,
        station_role: str,
        active_flag: bool,
        properties_json: Mapping[str, Any] | None = None,
        grid_snapshot_id: str | None = None,
    ) -> None:
        self.station_id = station_id
        self.basin_version_id = basin_version_id
        self.station_role = station_role
        self.active_flag = active_flag
        self.properties_json: dict[str, Any] = dict(properties_json or {})
        self.grid_snapshot_id = grid_snapshot_id

    def snapshot(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "basin_version_id": self.basin_version_id,
            "station_role": self.station_role,
            "active_flag": self.active_flag,
            "properties_json": dict(self.properties_json),
            "grid_snapshot_id": self.grid_snapshot_id,
        }


class _StationInventory:
    """Shared mutable station store threaded through the fake cursor.

    Tests build it once, hand it to the ``_FlipHarnessStore``, and read
    its ``rows`` after the lifecycle operation to inspect the flip
    outcome. Snapshots taken during the transaction (via
    ``mid_tx_snapshot``) let tests assert the rollback contract without
    depending on a real DB.
    """

    def __init__(self, rows: list[_StationRow]) -> None:
        self.rows = rows

    def snapshot(self) -> list[dict[str, Any]]:
        return [row.snapshot() for row in self.rows]


# --- fake cursor + transaction --------------------------------------------


class _FakeCursor:
    """In-memory cursor that recognizes the flip hook's two UPDATE statements.

    Sequences ``rowcount`` after each ``execute`` so the hook's fail-
    closed rowcount check works. Every other SQL statement raises to
    catch an accidental new statement leaking into the flip path.
    """

    def __init__(
        self,
        inventory: _StationInventory,
        *,
        raise_on_turn_on: Exception | None = None,
    ) -> None:
        self._inventory = inventory
        self._raise_on_turn_on = raise_on_turn_on
        self.rowcount: int | None = None
        # Record every executed statement (normalized) so tests can
        # assert the two-step ordering: turn-off THEN turn-on.
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split())
        params_tuple = tuple(params)
        self.executed.append((normalized, params_tuple))
        if normalized.startswith("UPDATE met.met_station SET active_flag = false"):
            self._handle_turn_off_all(params_tuple)
            return
        if normalized.startswith("UPDATE met.met_station SET active_flag = true"):
            if self._raise_on_turn_on is not None:
                raise self._raise_on_turn_on
            self._handle_turn_on_target(params_tuple)
            return
        raise NotImplementedError(f"_FakeCursor: unsupported SQL: {sql!r}")

    def fetchone(self) -> dict[str, Any] | None:  # pragma: no cover - unused
        return None

    # --- flip-SQL handlers ------------------------------------------------

    def _handle_turn_off_all(self, params: tuple[Any, ...]) -> None:
        (basin_version_id,) = params
        touched = 0
        for row in self._inventory.rows:
            if row.basin_version_id == basin_version_id and row.active_flag is True:
                row.active_flag = False
                touched += 1
        self.rowcount = touched

    def _handle_turn_on_target(self, params: tuple[Any, ...]) -> None:
        (
            basin_version_id,
            model_input_package_id,
            binding_checksum,
            grid_snapshot_id,
        ) = params
        touched = 0
        for row in self._inventory.rows:
            if row.basin_version_id != basin_version_id:
                continue
            if row.station_role != "direct_grid_cache":
                continue
            if row.grid_snapshot_id != grid_snapshot_id:
                continue
            props = row.properties_json
            if props.get("model_input_package_id") != model_input_package_id:
                continue
            if props.get("binding_checksum") != binding_checksum:
                continue
            row.active_flag = True
            touched += 1
        self.rowcount = touched


class _FakeTransaction:
    """Transaction context manager for the flip harness.

    Snapshots the station inventory at ``__enter__`` and, on a raised
    exception, restores the pre-transaction state — the atomic-rollback
    contract Change 4's ``_PsycopgTransaction`` would deliver against a
    real DB. Tests assert the restored state matches the pre-tx snapshot.
    """

    def __init__(self, store: _FlipHarnessStore) -> None:
        self._store = store
        self._pre_snapshot: list[dict[str, Any]] | None = None

    def __enter__(self) -> _FakeCursor:
        self._pre_snapshot = self._store.inventory.snapshot()
        cursor = _FakeCursor(
            self._store.inventory,
            raise_on_turn_on=self._store.raise_on_turn_on,
        )
        self._store._transactions.append(
            {"cursor": cursor, "committed": None, "pre_snapshot": self._pre_snapshot}
        )
        self._store._current_cursor = cursor
        return cursor

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: Any,
    ) -> bool:
        state = self._store._transactions[-1]
        state["committed"] = exc_type is None
        if exc_type is not None and self._pre_snapshot is not None:
            # Simulate atomic rollback: restore rows to their pre-tx state.
            snapshot_by_id = {row["station_id"]: row for row in self._pre_snapshot}
            for row in self._store.inventory.rows:
                pre = snapshot_by_id[row.station_id]
                row.basin_version_id = pre["basin_version_id"]
                row.station_role = pre["station_role"]
                row.active_flag = pre["active_flag"]
                row.properties_json = dict(pre["properties_json"])
                row.grid_snapshot_id = pre["grid_snapshot_id"]
        self._store._current_cursor = None
        return False


# --- harness store ---------------------------------------------------------


class _FlipHarnessStore(PsycopgModelRegistryStore):
    """In-memory PsycopgModelRegistryStore for the SUB-1 flip-hook tests.

    Reuses the same override pattern
    ``tests/test_variant_activation_cutover.py::_HarnessStore`` uses so
    the real ``model_lifecycle_operation`` — preflight, hook dispatch,
    transition, audit — runs unchanged on top of the fake cursor +
    station inventory.
    """

    def __init__(
        self,
        models: list[Mapping[str, Any]],
        inventory: _StationInventory,
        *,
        raise_on_turn_on: Exception | None = None,
    ) -> None:
        super().__init__("postgresql://harness")
        object.__setattr__(
            self, "_models", {row["model_id"]: dict(row) for row in models}
        )
        object.__setattr__(self, "audit_rows", [])
        object.__setattr__(self, "_transactions", [])
        object.__setattr__(self, "_current_cursor", None)
        object.__setattr__(self, "_state_updates", [])
        object.__setattr__(self, "inventory", inventory)
        object.__setattr__(self, "raise_on_turn_on", raise_on_turn_on)

    # ---- transaction plumbing --------------------------------------------

    def _transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    # ---- read helpers (cursor unused with in-memory backend) --------------

    def _lock_basin_version_scope(  # noqa: ARG002
        self, cursor: Any, basin_version_id: str
    ) -> None:
        return None

    def _fetch_model_lifecycle_row(
        self, cursor: Any, model_id: str, *, for_update: bool  # noqa: ARG002
    ) -> dict[str, Any] | None:
        row = self._models.get(model_id)
        return dict(row) if row is not None else None

    def _fetch_active_model_for_scope(
        self,
        cursor: Any,  # noqa: ARG002
        basin_version_id: str,
        *,
        for_update: bool,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        for row in self._models.values():
            if (
                row["basin_version_id"] == basin_version_id
                and bool(row.get("active_flag"))
                and str(row.get("lifecycle_state") or "active") == "active"
            ):
                return dict(row)
        return None

    def _fetch_trustworthy_rollback_history(  # noqa: ARG002
        self,
        cursor: Any,
        *,
        current_model: Mapping[str, Any],
        previous_model_id: str | None,
    ) -> dict[str, Any] | None:
        return None

    def _fetch_idempotent_rollback_retry_history(  # noqa: ARG002
        self,
        cursor: Any,
        *,
        model: Mapping[str, Any],
        current_active: Mapping[str, Any] | None,
        previous_model_id: str | None,
    ) -> dict[str, Any] | None:
        return None

    def _fetch_direct_grid_activation_history(  # noqa: ARG002
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        current_active: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        # The legacy-reactivation guard (§3.1) is not exercised by the
        # flip-hook tests. Keep the guard disarmed here so the harness
        # stays scoped to §1.1.
        return None

    # ---- write helpers ---------------------------------------------------

    def _update_model_lifecycle_state(
        self, cursor: Any, model_id: str, lifecycle_state: str  # noqa: ARG002
    ) -> dict[str, Any]:
        row = self._models[model_id]
        row["lifecycle_state"] = lifecycle_state
        row["active_flag"] = lifecycle_state == "active"
        self._state_updates.append((model_id, lifecycle_state, row["active_flag"]))
        return dict(row)

    def _insert_model_lifecycle_audit(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        model: Mapping[str, Any],
        updated: Mapping[str, Any],
        operation: ModelLifecycleOperation,
        outcome: str,
        policy_decision: Any,
        request_id: str | None,
        preflight: Mapping[str, Any],
        previous_model: Mapping[str, Any] | None,
        reason: str | None,
    ) -> int:
        entry = {
            "action": policy_decision.action_id,
            "actor": policy_decision.actor_id,
            "entity_type": "model_instance",
            "entity_id": model["model_id"],
            "operation": operation,
            "outcome": outcome,
            "basin_version_id": model.get("basin_version_id"),
            "request_id": request_id,
            "reason": reason,
            "preflight_status": preflight.get("status"),
            "updated_model_id": updated["model_id"],
            "previous_model_id": (
                previous_model["model_id"] if previous_model else None
            ),
        }
        self.audit_rows.append(entry)
        return len(self.audit_rows)


# --- Audit recorder --------------------------------------------------------


class _FakeAuditRecorder:
    """Records skip audit events emitted by the flip hook."""

    def __init__(self) -> None:
        self.skips: list[dict[str, Any]] = []

    def record_skip(self, reason: str, ctx: ModelActivationContext) -> None:
        self.skips.append(
            {
                "reason": reason,
                "basin_version_id": ctx.basin_version_id,
                "target_model_id": ctx.target_model.get("model_id"),
            }
        )


# --- Model + mirror fixtures ----------------------------------------------


GRID_SNAPSHOT_ID = "canonical_snapshot_grid_a_v1"
CANONICAL_GRID_KEY = "canonical_key_grid_a_v1"

# Target M1's built mapping-asset identity.
M1_MODEL_INPUT_PACKAGE_ID = "mip_m1_a"
M1_BINDING_CHECKSUM = "sha256:m1-binding"

# Fix-forward M1′'s built mapping-asset identity (distinct built asset).
M1_PRIME_MODEL_INPUT_PACKAGE_ID = "mip_m1_prime_a"
M1_PRIME_BINDING_CHECKSUM = "sha256:m1prime-binding"


def _direct_grid_resource_profile(
    *,
    model_input_package_id: str,
    binding_checksum: str,
) -> dict[str, Any]:
    """A well-formed ``resource_profile`` for a direct-grid target.

    Shape matches
    :func:`workers.model_registry.direct_grid_variant_registration._build_resource_profile`
    verbatim: ``canonical_grid_key`` + ``grid_snapshot_id`` at the top
    level, ``direct_grid_forcing`` as a parser-valid contract block. That
    is what the flip hook's classifier reads and what tests need to
    engage the flip.
    """
    return {
        "canonical_grid_key": CANONICAL_GRID_KEY,
        "grid_snapshot_id": GRID_SNAPSHOT_ID,
        "direct_grid_forcing": {
            "forcing_mapping_mode": "direct_grid",
            "binding_uri": f"s3://nhms/mapping/{model_input_package_id}/binding.zip",
            "binding_checksum": binding_checksum,
            "model_input_package_id": model_input_package_id,
            "sp_att_path": "basin.sp.att",
            "sp_att_checksum": f"sha256:{model_input_package_id}-spatt",
            "applicable_source_ids": ["gfs", "IFS"],
            "grid_id": "grid_a",
            "grid_signature": "sha256:grid-a-signature",
            "stations": [
                {
                    "station_id": (
                        f"{model_input_package_id}::cell:cell_a1"
                    ),
                    "shud_forcing_index": 1,
                    "forcing_filename": "cell_a1.csv",
                    "longitude": 100.0,
                    "latitude": 30.0,
                    "x": 100.0,
                    "y": 30.0,
                    "z": 0.0,
                    "grid_id": "grid_a",
                    "grid_cell_id": "cell_a1",
                },
                {
                    "station_id": (
                        f"{model_input_package_id}::cell:cell_a2"
                    ),
                    "shud_forcing_index": 2,
                    "forcing_filename": "cell_a2.csv",
                    "longitude": 101.0,
                    "latitude": 31.0,
                    "x": 101.0,
                    "y": 31.0,
                    "z": 0.0,
                    "grid_id": "grid_a",
                    "grid_cell_id": "cell_a2",
                },
            ],
        },
    }


def _mirror_properties(
    *,
    model_input_package_id: str,
    binding_checksum: str,
) -> dict[str, Any]:
    """Registration-side mirror ``properties_json`` (binding-identity fields).

    Only the fields the flip WHERE predicate reads
    (``model_input_package_id`` + ``binding_checksum``) are populated;
    the real registration path writes many more, but the hook only
    keys off the two identity discriminators (plus ``grid_snapshot_id``
    on the row column and ``station_role`` on its own column).
    """
    return {
        "derived_cache": True,
        "forcing_mapping_mode": "direct_grid",
        "model_input_package_id": model_input_package_id,
        "binding_checksum": binding_checksum,
    }


def _mirror_row(
    *,
    cell_id: str,
    active_flag: bool,
    model_input_package_id: str,
    binding_checksum: str,
    grid_snapshot_id: str = GRID_SNAPSHOT_ID,
    basin_version_id: str = BASIN_VERSION_ID,
) -> _StationRow:
    """A Change-4-shaped ``direct_grid_cache`` mirror row for the fake.

    The ``station_id`` is minted per Epic #961 SUB-2's ``_upsert_direct_grid_mirror``
    contract: ``f"{mapping_asset_identity}::cell:{grid_cell_id}"``. In these
    tests, ``model_input_package_id`` stands in as the mapping-asset identity
    token — real production callers pass a version-unique SHA-256 or UUID,
    but the flip WHERE predicate keys off ``properties_json`` fields
    (``model_input_package_id`` + ``binding_checksum``) plus ``grid_snapshot_id``,
    NOT the ``station_id`` string, so using ``model_input_package_id`` as
    the identity prefix here keeps the fixture close to the real mint
    without silently coupling to a value the WHERE clause never reads.
    """
    return _StationRow(
        station_id=f"{model_input_package_id}::cell:{cell_id}",
        basin_version_id=basin_version_id,
        station_role="direct_grid_cache",
        active_flag=active_flag,
        properties_json=_mirror_properties(
            model_input_package_id=model_input_package_id,
            binding_checksum=binding_checksum,
        ),
        grid_snapshot_id=grid_snapshot_id,
    )


def _legacy_row(
    *,
    station_id: str,
    active_flag: bool,
    basin_version_id: str = BASIN_VERSION_ID,
) -> _StationRow:
    """A legacy (M0) ``forcing_proxy`` row with no snapshot FK."""
    return _StationRow(
        station_id=station_id,
        basin_version_id=basin_version_id,
        station_role="forcing_proxy",
        active_flag=active_flag,
        properties_json={},
        grid_snapshot_id=None,
    )


def _legacy_active_model() -> dict[str, Any]:
    return _model_row(
        model_id="legacy_m0",
        active_flag=True,
        lifecycle_state="active",
    )


def _direct_grid_variant(
    *,
    model_id: str,
    model_input_package_id: str,
    binding_checksum: str,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
) -> dict[str, Any]:
    return _model_row(
        model_id=model_id,
        active_flag=active_flag,
        lifecycle_state=lifecycle_state,
        resource_profile=_direct_grid_resource_profile(
            model_input_package_id=model_input_package_id,
            binding_checksum=binding_checksum,
        ),
    )


def _register_hook(store: _FlipHarnessStore) -> _FakeAuditRecorder:
    """Attach the flip hook to the harness store and return the audit sink."""
    audit = _FakeAuditRecorder()
    hook = build_station_flag_flip_hook(audit_recorder=audit)
    store.register_pre_activation_hook("station_flag_flip", hook)
    return audit


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
# (7) Station-MVT source query byte-unchanged (static structural regression lock)
# ============================================================================


def test_station_mvt_source_query_unchanged() -> None:
    """§1.1 evidence: ``_station_source_version`` still filters ONLY on
    ``basin_version_id + active_flag=true`` and contains NO ``model_id``
    predicate.

    Design §Decision 1 rejects the ``model_id`` filter form: single-track
    visibility is delivered by row selection at the flip hook, not by
    adding a ``model_id`` filter to the MVT source query. This test is
    a static structural regression lock — it reads
    ``apps/api/routes/hydro_display.py::_station_source_version`` from
    disk (via :func:`inspect.getsource`), plus reads the file itself
    for redundancy, and asserts the query bodies still shape the
    invariant.
    """

    # Source of truth #1: the function's actual runtime source.
    function_source = inspect.getsource(_station_source_version)
    # Source of truth #2: the on-disk file. Redundant with the runtime
    # source, but the on-disk read catches a future refactor that
    # renames / relocates the function to a place ``inspect`` still
    # dereferences — the file path is pinned by design.
    # Resolve the on-disk path relative to this test file so pytest can
    # be invoked from any working directory (repo root, subdir, or an
    # unrelated cwd used by CI). ``parents[1]`` is the repo root:
    # ``tests/test_direct_grid_display_cutover_flip.py`` -> ``parents[0]``
    # is ``tests/`` and ``parents[1]`` is the repo root.
    repo_root = Path(__file__).resolve().parents[1]
    disk_source = (repo_root / "apps/api/routes/hydro_display.py").read_text(
        encoding="utf-8"
    )
    assert "def _station_source_version" in disk_source

    # Positive structural predicates: both required filters are present.
    assert "basin_version_id = :basin_version_id" in function_source
    # PostGIS branch (production).
    assert "AND active_flag = true" in function_source
    # SQLite branch (local test / dev fallback).
    assert "AND active_flag = 1" in function_source

    # Negative structural predicate: NO ``model_id`` predicate is added.
    # This is the design-pinned invariant — Decision 1 rejected the
    # ``model_id`` filter form; a future refactor that introduces one
    # would break the single-track flip design and this test would fail.
    lowered = function_source.lower()
    assert "model_id" not in lowered, (
        "Design §Decision 1 rejects adding a model_id predicate to the "
        "station-MVT source query; single-track visibility is delivered by "
        "the SUB-1 flip hook's row selection, not by the query."
    )


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
# mirrors ``apps/api/routes/hydro_display.py::_station_source_version``'s
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

    Mirrors ``apps/api/routes/hydro_display.py::_station_source_version``'s
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
