"""Shared harness of the direct-grid station-flag flip suites (Epic #992 SUB-1/SUB-2).

Non-collectible support module (#2527 partition of the 1814-line / 15-case
tests/test_direct_grid_display_cutover_flip.py).
It holds every definition both partitions consume: the ``met.met_station`` row
fake and inventory (``_StationRow``, ``_StationInventory``), the fake cursor and
transaction, the ``_FlipHarnessStore`` subclass of
:class:`packages.common.model_registry.PsycopgModelRegistryStore` that drives the
real preflight -> hook-dispatch -> transition -> audit path, the
``_FakeAuditRecorder``, and the model / mirror fixtures and factories
(``_direct_grid_resource_profile``, ``_mirror_row``, ``_legacy_row``,
``_direct_grid_variant``, ``_register_hook``, the M1 / M1' identity constants).

The ``tests.test_variant_activation_cutover`` names the harness needs are imported
here AND directly by each partition that uses them: the selector's suite-importer
index (#1561) walks suite files only, so the partitions' own module-scope edges are
what route a change to that suite to them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.common.model_registry import (
    ModelActivationContext,
    ModelLifecycleOperation,
    PsycopgModelRegistryStore,
)
from packages.common.station_set_flip import build_station_flag_flip_hook
from tests.test_variant_activation_cutover import (
    BASIN_VERSION_ID,
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
