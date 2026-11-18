"""Guard-scope tests (e) / (f) for the §5.1 replay lineage resolver.

Covers Epic #982 SUB-8 (``mapping-variant-state-compatibility`` task
5.1) evidence:

* Test (e): an offline ``M0`` replay resolution for a basin with
  direct-grid activation history makes ZERO lifecycle calls and is not
  intercepted by Change 4's activation-scoped legacy-reactivation guard.
* Test (f): an actual ``activate(M0)`` request for the SAME basin is
  still refused by the guard — proves the guard-scope invariant is
  preserved orthogonally to the resolver.

The resolver tests (a) / (b) / (c) / (d) are in
``tests/test_replay_lineage.py``; the fixture constants and in-memory
reader fakes both modules use are in ``tests/replay_lineage_helpers.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from packages.common.auth_policy import trusted_internal_policy_decision
from packages.common.model_registry import (
    ModelLifecycleOperation,
    PsycopgModelRegistryStore,
    _classify_forcing_mapping_mode,
)
from services.orchestrator.replay_lineage import resolve_replay_lineage
from tests.replay_lineage_helpers import (
    BASIN_VERSION_ID,
    CUTOVER_VALID_TIME,
    _FakeAuditReader,
    _FakeCloneReader,
)

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------


BASIN_ID = "basin_a"


# ===========================================================================
# Guard-scope tests (e) and (f) — the resolver DOES NOT call the lifecycle
# path, and the guard STILL refuses an actual ``activate(M0)`` request.
# ===========================================================================


def _valid_direct_grid_forcing_block(
    *,
    model_id: str,
    grid_id: str = "grid_a",
    applicable_source_ids: tuple[str, ...] = ("gfs", "IFS"),
) -> dict[str, Any]:
    """Return a fully-populated ``direct_grid_forcing`` section that parses.

    Same shape as ``tests/test_legacy_reactivation_guard.py`` — includes
    every field ``parse_direct_grid_forcing_contract`` requires so
    ``_classify_forcing_mapping_mode`` returns ``"direct_grid"``.
    """
    return {
        "forcing_mapping_mode": "direct_grid",
        "binding_uri": f"s3://nhms/models/{model_id}/binding.json",
        "binding_checksum": f"sha256:{model_id}-binding",
        "model_input_package_id": f"{model_id}_input",
        "sp_att_path": f"s3://nhms/models/{model_id}/sp_att.csv",
        "sp_att_checksum": f"sha256:{model_id}-spatt",
        "applicable_source_ids": list(applicable_source_ids),
        "grid_id": grid_id,
        "grid_signature": f"{grid_id}_v1",
        "station_bindings": [
            {
                "station_id": f"{model_id}_stn_1",
                "shud_forcing_index": 1,
                "forcing_filename": f"{model_id}_X0Y0.csv",
                "longitude": 100.0,
                "latitude": 30.0,
                "x": 100.0,
                "y": 30.0,
                "z": 0.0,
                "grid_id": grid_id,
                "grid_cell_id": f"{grid_id}_cell_1",
            }
        ],
    }


def _model_row(
    *,
    model_id: str,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
    resource_profile: dict[str, Any] | None = None,
    basin_version_id: str = BASIN_VERSION_ID,
    basin_id: str = BASIN_ID,
) -> dict[str, Any]:
    """Return a model row shaped like ``_fetch_model_lifecycle_row`` output.

    Mirrors the fixture shape used by
    ``tests/test_legacy_reactivation_guard.py`` so preflight sees a
    valid activation-ready row.
    """
    profile = {
        "manifest_uri": f"s3://nhms/models/{model_id}/manifest.json",
        "package_checksum": f"sha256:{model_id}-package",
        "copied_root_status": "verified",
        "package_checksum_verified": True,
        **(resource_profile or {}),
    }
    return {
        "model_id": model_id,
        "model_name": model_id,
        "basin_id": basin_id,
        "basin_name": basin_id.upper(),
        "basin_version_id": basin_version_id,
        "river_network_version_id": f"{basin_id}_rivnet_v01",
        "mesh_version_id": f"{basin_id}_mesh_v01",
        "calibration_version_id": f"{basin_id}_cal_v01",
        "shud_code_version": "2.0",
        "mesh_uri": f"s3://nhms/models/{model_id}/mesh.sp.mesh",
        "mesh_checksum": f"sha256:{model_id}-mesh",
        "model_package_uri": f"s3://nhms/models/{model_id}/package/",
        "package_checksum": f"sha256:{model_id}-package",
        "manifest_uri": f"s3://nhms/models/{model_id}/manifest.json",
        "source_inventory_checksum": None,
        "basin_slug": basin_id.replace("_", "-"),
        "shud_input_name": basin_id,
        "segment_count": 1,
        "basin_checksum": f"sha256:{basin_id}-basin",
        "river_network_checksum": f"sha256:{basin_id}-rivnet",
        "mesh_properties_json": {},
        "active_flag": active_flag,
        "lifecycle_state": lifecycle_state,
        "resource_profile": profile,
        "created_at": "2026-07-10T00:00:00Z",
    }


def _legacy_model(
    model_id: str,
    *,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
) -> dict[str, Any]:
    return _model_row(
        model_id=model_id,
        active_flag=active_flag,
        lifecycle_state=lifecycle_state,
    )


def _direct_grid_model(
    model_id: str,
    *,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
) -> dict[str, Any]:
    return _model_row(
        model_id=model_id,
        active_flag=active_flag,
        lifecycle_state=lifecycle_state,
        resource_profile={
            "direct_grid_forcing": _valid_direct_grid_forcing_block(model_id=model_id),
        },
    )


class _RecordingCursor:
    """Fake cursor recording SQL passing through it (regression guard)."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(  # pragma: no cover - regression guard
        self, statement: str, parameters: tuple[Any, ...] = ()
    ) -> None:
        self.statements.append((statement, tuple(parameters)))

    def fetchone(self) -> dict[str, Any] | None:  # pragma: no cover
        return None

    def fetchall(self) -> list[dict[str, Any]]:  # pragma: no cover
        return []


class _FakeTransaction:
    def __init__(self, harness: _LifecycleCallRecorder) -> None:
        self._harness = harness

    def __enter__(self) -> _RecordingCursor:
        cursor = _RecordingCursor()
        self._harness._transactions.append({"cursor": cursor, "committed": None})
        return cursor

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: Any,
    ) -> bool:
        self._harness._transactions[-1]["committed"] = exc_type is None
        return False


class _LifecycleCallRecorder(PsycopgModelRegistryStore):
    """In-memory ``PsycopgModelRegistryStore`` recording lifecycle calls.

    Every call to :meth:`model_lifecycle_operation` is appended to
    ``self.lifecycle_calls``. Tests (e) and (f) use this to prove:

    * Test (e): a resolver call produces an empty ``lifecycle_calls``
      list (zero-lifecycle-call invariant).
    * Test (f): a subsequent real ``activate(M0)`` call for the same
      basin is refused by the Change 4 legacy-reactivation guard
      (``LEGACY_REACTIVATION_BLOCKED`` blocker). The recorder confirms
      the request reached the lifecycle path.

    The audit-log guard predicate
    (:meth:`_fetch_direct_grid_activation_history`) is overridden to
    mirror production classification over the in-memory audit rows so
    the guard fires correctly without a live DB.
    """

    def __init__(self, models: list[Mapping[str, Any]]) -> None:
        super().__init__("postgresql://harness-replay-lineage")
        object.__setattr__(
            self, "_models", {row["model_id"]: dict(row) for row in models}
        )
        object.__setattr__(self, "audit_rows", [])
        object.__setattr__(self, "_transactions", [])
        object.__setattr__(self, "_state_updates", [])
        object.__setattr__(self, "lifecycle_calls", [])

    # ---- lifecycle-call recorder -----------------------------------------

    def model_lifecycle_operation(  # type: ignore[override]
        self,
        model_id: str,
        *,
        operation: ModelLifecycleOperation,
        policy_decision: Any,
        request_id: str,
        override_missing_active: bool = False,
        reason: str | None = None,
        previous_model_id: str | None = None,
    ) -> dict[str, Any]:
        self.lifecycle_calls.append(
            {
                "model_id": model_id,
                "operation": operation,
                "request_id": request_id,
                "previous_model_id": previous_model_id,
            }
        )
        return super().model_lifecycle_operation(
            model_id,
            operation=operation,
            policy_decision=policy_decision,
            request_id=request_id,
            override_missing_active=override_missing_active,
            reason=reason,
            previous_model_id=previous_model_id,
        )

    # ---- transaction plumbing --------------------------------------------

    def _transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    # ---- read helpers ----------------------------------------------------

    def _lock_basin_version_scope(self, cursor: Any, basin_version_id: str) -> None:  # noqa: ARG002
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

    def _fetch_trustworthy_rollback_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        current_model: Mapping[str, Any],  # noqa: ARG002
        previous_model_id: str | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    def _fetch_idempotent_rollback_retry_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        model: Mapping[str, Any],  # noqa: ARG002
        current_active: Mapping[str, Any] | None,  # noqa: ARG002
        previous_model_id: str | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    def _fetch_direct_grid_activation_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        basin_version_id: str,
        current_active: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Mirror production predicate over in-memory audit rows.

        Applies the SAME classifier as production
        (:func:`_classify_forcing_mapping_mode`) so the Change 4 guard
        fires on the audit-log arm without a live DB. Test (f) relies
        on this: it seeds an audit row for the direct-grid activation
        of ``direct_m1`` and then attempts ``activate(legacy_m0)`` —
        this predicate returns non-None (arming the guard), preflight
        emits ``LEGACY_REACTIVATION_BLOCKED``, and the operation is
        refused.
        """
        if current_active is not None:
            if _classify_forcing_mapping_mode(current_active) == "direct_grid":
                return {
                    "source": "current_active",
                    "model_id": str(current_active.get("model_id")),
                    "basin_version_id": basin_version_id,
                }
        for row in self.audit_rows:
            if row.get("entity_type") != "model_instance":
                continue
            if row.get("action") not in {
                "models.activate",
                "models.switch_version",
                "models.rollback_version",
            }:
                continue
            if row.get("outcome") not in {"allowed", "rollback"}:
                continue
            if row.get("basin_version_id") != basin_version_id:
                continue
            updated = self._models.get(row.get("updated_model_id"))
            if updated is None:
                continue
            if _classify_forcing_mapping_mode(updated) == "direct_grid":
                return {
                    "source": "audit_log",
                    "log_id": row.get("log_id"),
                    "model_id": row.get("updated_model_id"),
                    "basin_version_id": basin_version_id,
                }
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
            "log_id": len(self.audit_rows) + 1,
            "entity_type": "model_instance",
            "entity_id": model["model_id"],
            "action": policy_decision.action_id,
            "actor": policy_decision.actor_id,
            "operation": operation,
            "outcome": outcome,
            "basin_version_id": model.get("basin_version_id"),
            "request_id": request_id,
            "reason": reason,
            "preflight_status": preflight.get("status"),
            "updated_model_id": updated["model_id"],
            "previous_model_id": previous_model["model_id"] if previous_model else None,
        }
        self.audit_rows.append(entry)
        return entry["log_id"]


def _decision(action_id: str, target_id: str) -> Any:
    return trusted_internal_policy_decision(
        action_id,
        target_type="model_instance",
        target_id=target_id,
        actor_id="test:replay-lineage",
        roles=("sys_admin",),
    )


def _seed_basin_with_direct_grid_activation_history() -> _LifecycleCallRecorder:
    """Build a store with a real direct-grid activation history.

    Seeds two models on ``BASIN_VERSION_ID``:

    * ``legacy_m0`` — legacy-mapping, inactive/superseded.
    * ``direct_m1`` — direct-grid, active (arms the guard via the
      currently-active-model classifier + carries a matching audit row).

    Also injects a synthetic ``models.activate`` audit row for the M1
    activation so the guard's audit-log arm is armed even if the
    current-active classifier is bypassed in a future refactor.
    """
    store = _LifecycleCallRecorder(
        [
            _legacy_model("legacy_m0", active_flag=False, lifecycle_state="superseded"),
            _direct_grid_model(
                "direct_m1", active_flag=True, lifecycle_state="active"
            ),
        ]
    )
    # Seed a synthetic activation audit row so the audit-log arm is
    # populated (belt-and-suspenders — the current-active classifier
    # also arms the guard in this fixture).
    store.audit_rows.append(
        {
            "log_id": 1,
            "entity_type": "model_instance",
            "entity_id": "direct_m1",
            "action": "models.activate",
            "actor": "test:seed",
            "operation": "activate",
            "outcome": "allowed",
            "basin_version_id": BASIN_VERSION_ID,
            "request_id": None,
            "reason": None,
            "preflight_status": "ready",
            "updated_model_id": "direct_m1",
            "previous_model_id": "legacy_m0",
        }
    )
    return store


def test_pre_cutover_m0_replay_makes_no_lifecycle_call_not_intercepted_by_guard() -> None:
    """Evidence line (e): offline M0 replay makes ZERO lifecycle calls.

    On a basin with direct-grid activation history, resolving a pre-
    cutover replay must NOT touch ``model_lifecycle_operation``. The
    Change 4 legacy-reactivation guard is activation-scoped and cannot
    mis-fire on offline replay because the resolver never reaches an
    activation path.
    """
    store = _seed_basin_with_direct_grid_activation_history()
    # Sanity: no lifecycle calls yet.
    assert store.lifecycle_calls == []

    audit_reader = _FakeAuditReader(
        activations_by_basin={
            BASIN_VERSION_ID: {
                "m1_model_id": "direct_m1",
                "m0_model_id": "legacy_m0",
                "audit_log_id": 1,
            }
        }
    )
    clone_reader = _FakeCloneReader(
        valid_times_by_model={"direct_m1": {CUTOVER_VALID_TIME}}
    )

    plan = resolve_replay_lineage(
        basin_version_id=BASIN_VERSION_ID,
        interval_start=datetime(2026, 5, 1, tzinfo=UTC),
        interval_end=datetime(2026, 5, 15, tzinfo=UTC),
        audit_reader=audit_reader,
        clone_reader=clone_reader,
    )

    # The resolver returned a pre-cutover M0 plan.
    assert plan.classification == "entirely_pre_cutover"
    assert plan.sub_intervals[0].lineage == "M0"
    assert plan.sub_intervals[0].model_id == "legacy_m0"

    # Zero-lifecycle-call invariant: the store recorded NO
    # ``model_lifecycle_operation`` calls, so the Change 4 guard
    # (activation-scoped) cannot have fired.
    assert store.lifecycle_calls == []
    # No state updates either.
    assert store._state_updates == []
    # No audit rows written beyond the seeded one.
    assert len(store.audit_rows) == 1


def test_actual_activate_m0_for_same_basin_still_refused_by_guard() -> None:
    """Evidence line (f): actual ``activate(M0)`` is refused by the guard.

    Uses the SAME basin fixture as test (e) — a direct-grid activation
    history on ``basin_v01``. Calling
    ``store.model_lifecycle_operation('legacy_m0', operation='activate',
    ...)`` reaches the Change 4 legacy-reactivation guard, which
    classifies ``legacy_m0`` as legacy-mapping on a direct-grid-history
    basin and refuses with ``LEGACY_REACTIVATION_BLOCKED``.

    Proves that the resolver's zero-lifecycle-call invariant does NOT
    weaken the guard — the guard remains enforced on real activation
    requests for the same basin (orthogonality of offline replay and
    activation-path guarding).
    """
    store = _seed_basin_with_direct_grid_activation_history()

    result = store.model_lifecycle_operation(
        "legacy_m0",
        operation="activate",
        policy_decision=_decision("models.activate", "legacy_m0"),
        request_id="req-real-activate-legacy",
    )

    # The call reached the lifecycle path (recorded).
    assert len(store.lifecycle_calls) == 1
    assert store.lifecycle_calls[0]["model_id"] == "legacy_m0"
    assert store.lifecycle_calls[0]["operation"] == "activate"

    # And it was refused by the Change 4 guard.
    assert result["status"] == "blocked"
    preflight = result.get("preflight") or {}
    blocker_codes = [b["code"] for b in (preflight.get("blockers") or [])]
    assert "LEGACY_REACTIVATION_BLOCKED" in blocker_codes

    # No state transition, no additional audit row (only the seeded
    # audit row remains — the refusal is inert on state).
    assert store._state_updates == []
    # ``legacy_m0`` was not activated; ``direct_m1`` stays active.
    assert store._models["legacy_m0"]["active_flag"] is False
    assert store._models["direct_m1"]["active_flag"] is True
