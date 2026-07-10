"""Unit tests for §3.1 / §3.2 legacy-reactivation guard.

Covers Epic #961 / ``openspec/changes/source-specific-model-variant-routing``
tasks.md §3.1 (Legacy Reactivation Guard) and §3.2 (scope-to-activation-only).

The guard refuses any activation-class operation whose TARGET classifies
as legacy-mapping when the ``basin_version_id`` has direct-grid activation
history. Classification uses ONE classifier delegated to
``workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest``.
History is APPEND-ONLY (``ops.audit_log`` records of successful
activation-class transitions + the currently-active model classifier).

The harness follows the SUB-5 pattern (``tests/test_variant_activation_cutover.py``):
subclass :class:`PsycopgModelRegistryStore` with an in-memory
``_transaction()`` / audit log / model_instance table, so the real
``model_lifecycle_operation`` flows unchanged — preflight, hook dispatch,
transition, audit — over synthetic basins/models with no live TimescaleDB.

The ``-k`` selectors on each test name track the tasks.md evidence lines:

* ``-k "activate_legacy_refused or rollback_restored_legacy_refused or
  switch_legacy_refused or no_override"`` — §3.1 evidence line 1.
* ``-k "history_survives_deactivate or supersede_not_history"`` —
  §3.1 evidence line 2.
* ``-k "direct_to_direct_allowed or malformed_direct_contract_refused"`` —
  §3.1 evidence line 3.
* ``-k "inactive_only_not_armed or no_history_unaffected"`` —
  §3.1 evidence line 4.
* ``-k "offline_replay_not_blocked or scope_activation_only"`` —
  §3.2 evidence line.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from packages.common.auth_policy import trusted_internal_policy_decision
from packages.common.model_registry import (
    ModelLifecycleOperation,
    PostCommitPublishContext,
    PsycopgModelRegistryStore,
    _classify_forcing_mapping_mode,
)
from workers.forcing_producer.direct_grid_contract import (
    load_forcing_mapping_contract_from_manifest,
)

# --- test harness ----------------------------------------------------------


BASIN_VERSION_ID = "basin_v01"
BASIN_ID = "basin_a"


def _valid_direct_grid_forcing_block(
    *,
    model_id: str,
    grid_id: str = "grid_a",
    applicable_source_ids: tuple[str, ...] = ("gfs", "IFS"),
) -> dict[str, Any]:
    """Return a fully-populated ``direct_grid_forcing`` section that parses.

    Includes every field ``parse_direct_grid_forcing_contract`` requires so
    the classifier returns ``"direct_grid"`` (not ``"invalid_direct_grid"``).
    Station bindings satisfy uniqueness + contiguous-index invariants.
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

    Includes every field ``_activation_safety_evidence`` and
    ``_build_model_operation_preflight`` inspect so a preflight over this
    row lands ``status='ready'`` for activation-class operations when the
    §3.1 guard is not armed.
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
    basin_version_id: str = BASIN_VERSION_ID,
) -> dict[str, Any]:
    """A legacy-mapping model row: no ``direct_grid_forcing`` block."""
    return _model_row(
        model_id=model_id,
        active_flag=active_flag,
        lifecycle_state=lifecycle_state,
        resource_profile=None,
        basin_version_id=basin_version_id,
    )


def _direct_grid_model(
    model_id: str,
    *,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
    basin_version_id: str = BASIN_VERSION_ID,
    grid_id: str = "grid_a",
) -> dict[str, Any]:
    """A model row whose ``direct_grid_forcing`` parses cleanly."""
    return _model_row(
        model_id=model_id,
        active_flag=active_flag,
        lifecycle_state=lifecycle_state,
        resource_profile={
            "direct_grid_forcing": _valid_direct_grid_forcing_block(
                model_id=model_id, grid_id=grid_id
            ),
        },
        basin_version_id=basin_version_id,
    )


def _malformed_direct_grid_model(
    model_id: str,
    *,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
    basin_version_id: str = BASIN_VERSION_ID,
) -> dict[str, Any]:
    """A model row whose ``direct_grid_forcing`` declares direct-grid but fails.

    Missing ``binding_uri`` triggers ``DirectGridContractError`` inside the
    parser — the classifier returns ``"invalid_direct_grid"``, which the
    guard maps to the ``DIRECT_GRID_CONTRACT_INVALID`` blocker (distinct
    from ``LEGACY_REACTIVATION_BLOCKED``).
    """
    block = _valid_direct_grid_forcing_block(model_id=model_id)
    del block["binding_uri"]  # trip the parser fail-closed
    return _model_row(
        model_id=model_id,
        active_flag=active_flag,
        lifecycle_state=lifecycle_state,
        resource_profile={"direct_grid_forcing": block},
        basin_version_id=basin_version_id,
    )


class _RecordingCursor:
    """Fake cursor that records SQL passing through it (regression guard)."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:  # pragma: no cover - guard
        self.statements.append((statement, tuple(parameters)))

    def fetchone(self) -> dict[str, Any] | None:  # pragma: no cover - guard
        return None

    def fetchall(self) -> list[dict[str, Any]]:  # pragma: no cover - guard
        return []


class _FakeTransaction:
    """Context manager wrapping a :class:`_RecordingCursor`."""

    def __init__(self, harness: _HarnessStore) -> None:
        self._harness = harness

    def __enter__(self) -> _RecordingCursor:
        cursor = _RecordingCursor()
        self._harness._transactions.append({"cursor": cursor, "committed": None})
        return cursor

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, _tb: Any) -> bool:
        state = self._harness._transactions[-1]
        state["committed"] = exc_type is None
        return False


class _HarnessStore(PsycopgModelRegistryStore):
    """In-memory registry store for §3.1 / §3.2 guard tests.

    Overrides DB-touching helpers so the real ``model_lifecycle_operation``
    (preflight, hook dispatch, transition, audit) runs unchanged over an
    in-memory ``self._models`` map and ``self.audit_rows`` list. The
    ``_fetch_direct_grid_activation_history`` override applies the SAME
    classifier as production (``_classify_forcing_mapping_mode``) so the
    tests exercise the real classification logic on both the currently
    active model and past audit records.
    """

    def __init__(self, models: list[Mapping[str, Any]]) -> None:
        super().__init__("postgresql://harness")
        object.__setattr__(self, "_models", {row["model_id"]: dict(row) for row in models})
        object.__setattr__(self, "audit_rows", [])
        object.__setattr__(self, "_transactions", [])
        object.__setattr__(self, "_state_updates", [])
        object.__setattr__(self, "publisher_calls", [])

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
        self, cursor: Any, basin_version_id: str, *, for_update: bool  # noqa: ARG002
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
        current_model: Mapping[str, Any],
        previous_model_id: str | None,
    ) -> dict[str, Any] | None:
        # Fake trustworthy rollback history so ``rollback_version`` reaches
        # the §3.1 guard site instead of being short-circuited on missing
        # audit trail. The rollback guard is orthogonal to §3.1.
        if previous_model_id is None:
            return None
        return {
            "trusted": True,
            "prior_audit_log_id": 42,
            "matched_previous_model_id": previous_model_id,
            "basin_version_id": current_model.get("basin_version_id"),
        }

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
        """Mirror production predicate over in-memory audit rows + current active."""
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


# --- helpers ---------------------------------------------------------------


def _decision(action_id: str, target_id: str, *, roles: tuple[str, ...] = ("sys_admin",)) -> Any:
    return trusted_internal_policy_decision(
        action_id,
        target_type="model_instance",
        target_id=target_id,
        actor_id="test:harness",
        roles=roles,
    )


def _publisher_stub(store: _HarnessStore) -> list[PostCommitPublishContext]:
    """Register a recording publisher; return the list callers can inspect."""
    contexts: list[PostCommitPublishContext] = []

    def _record(ctx: PostCommitPublishContext) -> None:
        contexts.append(ctx)

    store.register_post_commit_manifest_publisher(_record)
    return contexts


def _preflight_blocker_codes(result: Mapping[str, Any]) -> list[str]:
    preflight = result.get("preflight") or {}
    return [blocker["code"] for blocker in (preflight.get("blockers") or [])]


def _seed_audit_event(
    store: _HarnessStore,
    *,
    updated_model_id: str,
    action: str = "models.activate",
    outcome: str = "allowed",
    basin_version_id: str = BASIN_VERSION_ID,
    operation: str = "activate",
) -> None:
    """Inject a synthetic successful activation-class audit record.

    Used by the ``inactive_only_not_armed`` / ``no_history_unaffected``
    negative tests to demonstrate that unrelated audit rows do NOT arm
    the guard.
    """
    store.audit_rows.append(
        {
            "log_id": len(store.audit_rows) + 1,
            "entity_type": "model_instance",
            "entity_id": updated_model_id,
            "action": action,
            "actor": "test:harness-seed",
            "operation": operation,
            "outcome": outcome,
            "basin_version_id": basin_version_id,
            "request_id": None,
            "reason": None,
            "preflight_status": "ready",
            "updated_model_id": updated_model_id,
            "previous_model_id": None,
        }
    )


# ============================================================================
# Classifier sanity checks
# ============================================================================


def test_classifier_returns_direct_grid_for_valid_contract() -> None:
    """A model whose ``direct_grid_forcing`` parses classifies as direct-grid."""
    model = _direct_grid_model("m1")
    assert _classify_forcing_mapping_mode(model) == "direct_grid"


def test_classifier_returns_legacy_for_no_direct_grid_block() -> None:
    """A model with no ``direct_grid_forcing`` block classifies as legacy."""
    model = _legacy_model("m0")
    assert _classify_forcing_mapping_mode(model) == "legacy"


def test_classifier_returns_invalid_direct_grid_for_malformed_block() -> None:
    """A malformed direct-grid contract classifies as ``invalid_direct_grid``."""
    model = _malformed_direct_grid_model("m2")
    assert _classify_forcing_mapping_mode(model) == "invalid_direct_grid"


def test_classifier_recognizes_direct_grid_contract_alt_section_key() -> None:
    """Alt section key ``direct_grid_contract`` must classify symmetrically.

    ``load_forcing_mapping_contract_from_manifest`` accepts any of
    ``direct_grid_forcing`` / ``direct_grid_contract`` /
    ``forcing_mapping_contract``. The classifier delegates and must NOT
    short-circuit on the specific key name — otherwise a model persisted
    with the alt key would be silently mis-classified as legacy and past
    activations would be silently NOT armed by the audit-log guard.
    """
    model = _model_row(
        model_id="alt_dgc",
        resource_profile={
            "direct_grid_contract": _valid_direct_grid_forcing_block(model_id="alt_dgc"),
        },
    )
    assert _classify_forcing_mapping_mode(model) == "direct_grid"


def test_classifier_recognizes_forcing_mapping_contract_alt_section_key() -> None:
    """Alt section key ``forcing_mapping_contract`` must classify symmetrically.

    Same symmetry invariant as ``direct_grid_contract``: any recognized
    parser section key must classify as ``"direct_grid"`` when the contract
    parses cleanly.
    """
    model = _model_row(
        model_id="alt_fmc",
        resource_profile={
            "forcing_mapping_contract": _valid_direct_grid_forcing_block(model_id="alt_fmc"),
        },
    )
    assert _classify_forcing_mapping_mode(model) == "direct_grid"


def test_classifier_recognizes_root_level_forcing_mapping_mode_direct_grid() -> None:
    """Root-level ``forcing_mapping_mode='direct_grid'`` classifies symmetrically.

    The parser (``allow_root_direct_grid=True`` default) accepts a manifest
    whose ``forcing_mapping_mode='direct_grid'`` sits at the root, using
    the manifest itself as the contract payload when no nested section is
    present. The classifier must delegate and honor that acceptance —
    ``"direct_grid"`` when the parser returns a valid contract.
    """
    root_contract = _valid_direct_grid_forcing_block(model_id="root_dg")
    model = _model_row(
        model_id="root_dg",
        resource_profile=root_contract,
    )
    assert _classify_forcing_mapping_mode(model) == "direct_grid"


def test_classifier_returns_legacy_for_resource_profile_without_direct_grid_declaration() -> None:
    """A resource_profile that never declares direct-grid classifies as legacy.

    Unchanged fail-closed default: absent any ``forcing_mapping_mode`` at
    root and any of the recognized parser section keys, the classifier
    returns ``"legacy"``.
    """
    model = _model_row(
        model_id="no_intent",
        resource_profile={"unrelated_field": "irrelevant"},
    )
    assert _classify_forcing_mapping_mode(model) == "legacy"


# ============================================================================
# §3.1 evidence line 1: activate/switch/rollback refusals + no-override
# ============================================================================


def test_activate_legacy_refused_on_direct_grid_history_basin() -> None:
    """activate on a legacy target is blocked when history is armed.

    Setup: a basin with a direct-grid currently-active row (arms the
    guard via ``current_active`` classifier) and a separate legacy row.
    Requesting ``activate`` on the legacy row must land ``blocked`` with
    the ``LEGACY_REACTIVATION_BLOCKED`` code — no state transition, no
    manifest re-publish.
    """
    store = _HarnessStore(
        [
            _direct_grid_model("direct_current", active_flag=True, lifecycle_state="active"),
            _legacy_model("legacy_m0", active_flag=False, lifecycle_state="inactive"),
        ]
    )
    publisher_contexts = _publisher_stub(store)

    result = store.model_lifecycle_operation(
        "legacy_m0",
        operation="activate",
        policy_decision=_decision("models.activate", "legacy_m0"),
        request_id="req-activate-legacy-refused",
    )

    assert result["status"] == "blocked"
    assert "LEGACY_REACTIVATION_BLOCKED" in _preflight_blocker_codes(result)
    # No state transition.
    assert store._state_updates == []
    # No manifest re-publish.
    assert publisher_contexts == []
    # The direct-grid model stays active; the legacy target stays inactive.
    assert store._models["direct_current"]["lifecycle_state"] == "active"
    assert store._models["legacy_m0"]["lifecycle_state"] == "inactive"


def test_switch_legacy_refused_on_direct_grid_history_basin() -> None:
    """switch_version to a legacy target is blocked when history is armed.

    Setup identical to the activate refusal: direct-grid current active
    + legacy inactive candidate. ``switch_version`` addressed at the
    legacy candidate must be refused with ``LEGACY_REACTIVATION_BLOCKED``.
    """
    store = _HarnessStore(
        [
            _direct_grid_model("direct_current", active_flag=True, lifecycle_state="active"),
            _legacy_model("legacy_m0", active_flag=False, lifecycle_state="inactive"),
        ]
    )
    publisher_contexts = _publisher_stub(store)

    result = store.model_lifecycle_operation(
        "legacy_m0",
        operation="switch_version",
        policy_decision=_decision("models.switch_version", "legacy_m0"),
        request_id="req-switch-legacy-refused",
    )

    assert result["status"] == "blocked"
    assert "LEGACY_REACTIVATION_BLOCKED" in _preflight_blocker_codes(result)
    assert store._state_updates == []
    assert publisher_contexts == []


def test_rollback_restored_legacy_refused_when_previous_is_legacy() -> None:
    """rollback_version whose RESTORED model is legacy is refused.

    Setup: ``current_active`` is direct-grid (arms the guard via the
    current-active classifier), ``restored_previous`` is legacy. Rollback
    addressed at the currently-active direct-grid model with
    ``previous_model_id='restored_previous'`` must be refused with the
    ``LEGACY_REACTIVATION_BLOCKED`` code because the guard classifies the
    RESTORED model (not the addressed active model).
    """
    store = _HarnessStore(
        [
            _direct_grid_model(
                "current_direct", active_flag=True, lifecycle_state="active"
            ),
            _legacy_model(
                "restored_previous", active_flag=False, lifecycle_state="superseded"
            ),
        ]
    )
    publisher_contexts = _publisher_stub(store)

    result = store.model_lifecycle_operation(
        "current_direct",
        operation="rollback_version",
        policy_decision=_decision("models.rollback_version", "current_direct"),
        request_id="req-rollback-restored-legacy",
        previous_model_id="restored_previous",
    )

    assert result["status"] == "blocked"
    blockers = _preflight_blocker_codes(result)
    assert "LEGACY_REACTIVATION_BLOCKED" in blockers
    # No state transition, no manifest re-publish.
    assert store._state_updates == []
    assert publisher_contexts == []
    # Both models retain their pre-op state — the refusal is inert on state.
    assert store._models["current_direct"]["lifecycle_state"] == "active"
    assert store._models["restored_previous"]["lifecycle_state"] == "superseded"


def test_no_override_flag_reason_role_admits_legacy_target() -> None:
    """No override flag/reason/role combination admits a legacy target.

    Per the 2026-07-09 grill decision, the guard is a fixed product
    decision with no admitting path. Even sys_admin, an explicit reason,
    and ``override_missing_active=True`` (the only override supported by
    lifecycle operations, and only for ``deactivate``) do not defeat the
    guard when the target is legacy on a basin with direct-grid history.
    """
    store = _HarnessStore(
        [
            _direct_grid_model("direct_current", active_flag=True, lifecycle_state="active"),
            _legacy_model("legacy_m0", active_flag=False, lifecycle_state="inactive"),
        ]
    )

    # Try ``activate`` with a sys_admin role, an explicit reason, and the
    # override flag set. The guard MUST still refuse.
    result = store.model_lifecycle_operation(
        "legacy_m0",
        operation="activate",
        policy_decision=_decision(
            "models.activate", "legacy_m0", roles=("sys_admin", "registry_admin")
        ),
        request_id="req-no-override-legacy",
        override_missing_active=True,
        reason="operator: force legacy reactivation attempt",
    )

    assert result["status"] == "blocked"
    assert "LEGACY_REACTIVATION_BLOCKED" in _preflight_blocker_codes(result)
    # The refusal is not shadowed by ``ALREADY_CURRENT`` or another admit.
    assert store._state_updates == []


# ============================================================================
# §3.1 evidence line 2: history survives deactivate; supersede does not arm
# ============================================================================


def test_history_survives_deactivate_and_optional_deprecate() -> None:
    """Direct-grid activation history persists across deactivate + deprecate.

    Flow:
      1. Activate direct M1 on a basin whose baseline is legacy. This
         supersedes legacy_m0 and writes an ``models.activate`` audit
         row with ``updated_model_id='direct_m1'`` — the record shape
         the append-only history predicate reads.
      2. Deactivate M1 via the sys_admin missing-active override. Now
         both rows are non-active, but the append-only audit row from
         step 1 persists.
      3. (Optional) Deprecate M1. Still no audit-log mutation.
      4. Attempt to activate legacy_m0. The guard still refuses because
         the direct-grid activation audit row from step 1 arms history.
    """
    store = _HarnessStore(
        [
            _legacy_model("legacy_m0", active_flag=True, lifecycle_state="active"),
            _direct_grid_model("direct_m1", active_flag=False, lifecycle_state="inactive"),
        ]
    )

    # Step 1: activate direct_m1. Guard is armed the moment the swap
    # commits (direct_m1 becomes active + an audit row is written).
    step1 = store.model_lifecycle_operation(
        "direct_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_m1"),
        request_id="req-history-step1",
    )
    assert step1["status"] == "allowed"
    activation_audit = [r for r in store.audit_rows if r["outcome"] == "allowed"]
    assert any(r["updated_model_id"] == "direct_m1" for r in activation_audit)

    # Step 2: deactivate direct_m1 (sys_admin missing-active override).
    step2 = store.model_lifecycle_operation(
        "direct_m1",
        operation="deactivate",
        policy_decision=_decision("models.deactivate", "direct_m1"),
        request_id="req-history-step2",
        override_missing_active=True,
        reason="test: prove history survives deactivate",
    )
    assert step2["status"] == "allowed"
    assert store._models["direct_m1"]["lifecycle_state"] == "inactive"

    # Step 3 (optional): deprecate direct_m1. Audit record from step 1
    # persists regardless.
    step3 = store.model_lifecycle_operation(
        "direct_m1",
        operation="deprecate",
        policy_decision=_decision("models.deactivate", "direct_m1"),
        request_id="req-history-step3",
    )
    assert step3["status"] == "allowed"
    assert store._models["direct_m1"]["lifecycle_state"] == "deprecated"

    # Step 4: attempt to activate legacy_m0. Guard MUST still refuse
    # because the append-only step-1 audit record is still there.
    step4 = store.model_lifecycle_operation(
        "legacy_m0",
        operation="activate",
        policy_decision=_decision("models.activate", "legacy_m0"),
        request_id="req-history-step4",
    )
    assert step4["status"] == "blocked"
    assert "LEGACY_REACTIVATION_BLOCKED" in _preflight_blocker_codes(step4)
    # legacy_m0 is not activated by step 4.
    assert store._models["legacy_m0"]["active_flag"] is False


def test_supersede_not_history_on_never_activated_inactive_direct_grid_variant() -> None:
    """Supersede of an inactive-only direct-grid variant does NOT arm history.

    Setup: legacy model is active; a direct-grid variant is registered
    but INACTIVE and NEVER activated. Supersede the direct-grid variant
    from inactive to superseded. No successful activation-class audit
    record exists for the direct-grid variant, so the guard is not
    armed. A subsequent legacy ``switch_version`` on the basin's other
    legacy candidate proceeds unchanged.
    """
    store = _HarnessStore(
        [
            _legacy_model("legacy_active", active_flag=True, lifecycle_state="active"),
            _legacy_model("legacy_candidate", active_flag=False, lifecycle_state="inactive"),
            _direct_grid_model(
                "direct_registered_only", active_flag=False, lifecycle_state="inactive"
            ),
        ]
    )

    # Supersede the inactive direct-grid variant (mechanism-only change).
    supersede = store.model_lifecycle_operation(
        "direct_registered_only",
        operation="supersede",
        policy_decision=_decision("models.supersede", "direct_registered_only"),
        request_id="req-supersede-direct",
    )
    assert supersede["status"] == "allowed"
    assert store._models["direct_registered_only"]["lifecycle_state"] == "superseded"

    # Guard is NOT armed: no successful activation-class audit record for
    # a direct-grid model exists (supersede audit rows carry
    # action='models.supersede', which the guard predicate excludes).
    # Legacy switch_version on the basin's other legacy candidate must
    # proceed exactly as before this change.
    result = store.model_lifecycle_operation(
        "legacy_candidate",
        operation="switch_version",
        policy_decision=_decision("models.switch_version", "legacy_candidate"),
        request_id="req-legacy-switch-after-supersede",
    )
    assert result["status"] == "allowed"
    assert "LEGACY_REACTIVATION_BLOCKED" not in _preflight_blocker_codes(result)
    assert store._models["legacy_candidate"]["lifecycle_state"] == "active"


# ============================================================================
# §3.1 evidence line 3: direct-to-direct allowed; malformed refused distinctly
# ============================================================================


def test_direct_to_direct_allowed_on_direct_history_basin() -> None:
    """Activating a valid direct-grid variant on a direct-history basin is allowed.

    Setup: a currently-active direct-grid model (arms the guard) and a
    second valid direct-grid variant. Fix-forward direct → direct' is
    the sole admitted transition on an armed basin.
    """
    store = _HarnessStore(
        [
            _direct_grid_model("direct_v1", active_flag=True, lifecycle_state="active"),
            _direct_grid_model(
                "direct_v2",
                active_flag=False,
                lifecycle_state="inactive",
                grid_id="grid_b",
            ),
        ]
    )
    publisher_contexts = _publisher_stub(store)

    result = store.model_lifecycle_operation(
        "direct_v2",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_v2"),
        request_id="req-direct-to-direct",
    )

    assert result["status"] == "allowed"
    # No legacy-reactivation blocker fired.
    assert "LEGACY_REACTIVATION_BLOCKED" not in _preflight_blocker_codes(result)
    assert "DIRECT_GRID_CONTRACT_INVALID" not in _preflight_blocker_codes(result)
    # Real swap happened; publisher fired once.
    assert store._models["direct_v1"]["lifecycle_state"] == "superseded"
    assert store._models["direct_v2"]["lifecycle_state"] == "active"
    assert len(publisher_contexts) == 1


def test_malformed_direct_contract_refused_with_distinct_blocker_code() -> None:
    """A malformed direct-grid target is refused with DIRECT_GRID_CONTRACT_INVALID.

    Setup: a currently-active direct-grid model arms the guard; the
    activation target declares ``forcing_mapping_mode='direct_grid'``
    but its contract fails the parser (missing ``binding_uri``). The
    guard refuses the operation with a DISTINCT code
    (``DIRECT_GRID_CONTRACT_INVALID``), NOT ``LEGACY_REACTIVATION_BLOCKED``,
    so an operator can distinguish "broken fix-forward candidate" from
    "genuine legacy target".
    """
    store = _HarnessStore(
        [
            _direct_grid_model("direct_current", active_flag=True, lifecycle_state="active"),
            _malformed_direct_grid_model(
                "direct_broken", active_flag=False, lifecycle_state="inactive"
            ),
        ]
    )
    publisher_contexts = _publisher_stub(store)

    result = store.model_lifecycle_operation(
        "direct_broken",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_broken"),
        request_id="req-malformed-direct",
    )

    assert result["status"] == "blocked"
    blocker_codes = _preflight_blocker_codes(result)
    assert "DIRECT_GRID_CONTRACT_INVALID" in blocker_codes
    # And crucially NOT the legacy-reactivation code — the distinction
    # is load-bearing per §3.1 evidence line 3.
    assert "LEGACY_REACTIVATION_BLOCKED" not in blocker_codes
    # No state transition, no publish.
    assert store._state_updates == []
    assert publisher_contexts == []


# ============================================================================
# §3.1 evidence line 4: inactive-only doesn't arm; no-history unaffected
# ============================================================================


def test_inactive_only_not_armed_when_variant_never_activated() -> None:
    """An inactive-only direct-grid variant does not arm the guard.

    Setup: legacy model is active; a direct-grid variant is INACTIVE
    and has NEVER been activated (no audit record). The guard is not
    armed. Requesting ``activate`` on a legacy candidate proceeds
    exactly as before this change.
    """
    store = _HarnessStore(
        [
            _legacy_model("legacy_current", active_flag=True, lifecycle_state="active"),
            _legacy_model("legacy_candidate", active_flag=False, lifecycle_state="inactive"),
            _direct_grid_model(
                "direct_registered_only", active_flag=False, lifecycle_state="inactive"
            ),
        ]
    )
    # Sanity: no audit rows seeded, no successful activation-class record
    # exists for a direct-grid model. Also seed an UNRELATED audit row
    # (e.g. a supersede on the direct-grid variant) that the guard MUST
    # NOT read as arming the history — only allowed/rollback outcomes on
    # activation-class actions count.
    _seed_audit_event(
        store,
        updated_model_id="direct_registered_only",
        action="models.supersede",
        outcome="allowed",
        operation="supersede",
    )

    result = store.model_lifecycle_operation(
        "legacy_candidate",
        operation="switch_version",
        policy_decision=_decision("models.switch_version", "legacy_candidate"),
        request_id="req-inactive-only-not-armed",
    )

    assert result["status"] == "allowed"
    assert "LEGACY_REACTIVATION_BLOCKED" not in _preflight_blocker_codes(result)
    # Real switch happened.
    assert store._models["legacy_candidate"]["lifecycle_state"] == "active"
    assert store._models["legacy_current"]["lifecycle_state"] == "superseded"


def test_no_history_unaffected_13_live_basin_safety() -> None:
    """A basin with no direct-grid variant behaves exactly as before.

    Setup: legacy active + legacy candidate on a basin that has never
    seen any direct-grid variant. The 13-live-basin production surface
    matches this shape. The guard is inert: legacy activation,
    switch_version, and rollback_version all proceed unchanged, with
    no new blocker.
    """
    store = _HarnessStore(
        [
            _legacy_model("legacy_active", active_flag=True, lifecycle_state="active"),
            _legacy_model("legacy_candidate", active_flag=False, lifecycle_state="inactive"),
        ]
    )
    publisher_contexts = _publisher_stub(store)

    # activate on the legacy candidate (via switch_version, since a
    # currently-active exists).
    result = store.model_lifecycle_operation(
        "legacy_candidate",
        operation="switch_version",
        policy_decision=_decision("models.switch_version", "legacy_candidate"),
        request_id="req-no-history-safety",
    )
    assert result["status"] == "allowed"
    assert _preflight_blocker_codes(result) == []
    assert store._models["legacy_candidate"]["lifecycle_state"] == "active"
    assert store._models["legacy_active"]["lifecycle_state"] == "superseded"
    # Publisher DID fire for this real swap — behavior is unchanged.
    assert len(publisher_contexts) == 1


# ============================================================================
# §3.2 evidence line: offline replay not blocked; scope-activation-only
# ============================================================================


def test_offline_replay_not_blocked_when_legacy_package_used_directly() -> None:
    """Offline replay/calibration using the legacy package is not blocked.

    §3.2 scope proof: the guard lives inside
    ``_build_model_operation_preflight``'s activation-class branch. An
    offline replay or calibration path that reads a legacy model's
    package DIRECTLY — via
    ``load_forcing_mapping_contract_from_manifest`` returning ``None``
    (no direct-grid contract, hence legacy IDW) — does NOT invoke the
    lifecycle preflight, so the guard cannot fire.

    Concretely: the classifier returns ``"legacy"`` for a legacy
    resource_profile, and the direct-grid contract loader returns
    ``None`` (no exception, no blocker, no state change). A store with
    armed history that is NEVER handed a lifecycle operation records
    zero state updates and zero audit rows.
    """
    store = _HarnessStore(
        [
            _direct_grid_model("direct_current", active_flag=True, lifecycle_state="active"),
            _legacy_model("legacy_offline", active_flag=False, lifecycle_state="inactive"),
        ]
    )
    # Sanity: history WOULD be armed if any lifecycle operation touched
    # the legacy model. Prove it — the current_active is direct-grid.
    assert _classify_forcing_mapping_mode(
        store._models["direct_current"]
    ) == "direct_grid"

    # Simulate the offline path: load the legacy resource_profile via
    # the direct-grid contract loader. It must return ``None`` (no
    # direct-grid contract needed for offline legacy replay), not raise
    # a guard blocker.
    legacy_row = store._models["legacy_offline"]
    contract = load_forcing_mapping_contract_from_manifest(legacy_row["resource_profile"])
    assert contract is None, (
        "the offline replay path reads the legacy manifest directly; the "
        "direct-grid loader must return None (contract-not-applicable), "
        "not raise a guard blocker."
    )
    # Classifier returns legacy — no direct-grid contract to enforce.
    assert _classify_forcing_mapping_mode(legacy_row) == "legacy"

    # No lifecycle op was called → no state updates and no audit rows.
    # The guard is dormant until an activation-class operation runs.
    assert store._state_updates == []
    assert store.audit_rows == []


def test_scope_activation_only_deactivate_and_deprecate_not_blocked_on_direct_history() -> None:
    """deactivate and deprecate on a direct-grid model with armed history proceed.

    §3.2 scope proof: the guard scopes to activation-class operations
    only (``activate`` / ``switch_version`` / ``rollback_version``).
    ``deactivate`` and ``deprecate`` on a direct-grid model with armed
    history are NOT blocked by the guard — they are inert on the
    ``LEGACY_REACTIVATION_BLOCKED`` predicate because they do not
    activate a target.
    """
    store = _HarnessStore(
        [
            _direct_grid_model("direct_current", active_flag=True, lifecycle_state="active"),
        ]
    )
    # Arm the history via a synthetic prior activation audit row (the
    # currently-active row is direct-grid, so history is ALSO armed via
    # the current-active predicate — but this seeded record proves the
    # audit-log arm survives future deactivate/deprecate).
    _seed_audit_event(
        store,
        updated_model_id="direct_current",
        action="models.activate",
        outcome="allowed",
        operation="activate",
    )

    # deactivate on the direct-grid model — must succeed (guard silent).
    deactivate = store.model_lifecycle_operation(
        "direct_current",
        operation="deactivate",
        policy_decision=_decision("models.deactivate", "direct_current"),
        request_id="req-scope-deactivate",
        override_missing_active=True,
        reason="test: prove guard scopes to activation-class only",
    )
    assert deactivate["status"] == "allowed"
    assert "LEGACY_REACTIVATION_BLOCKED" not in _preflight_blocker_codes(deactivate)
    assert store._models["direct_current"]["lifecycle_state"] == "inactive"

    # deprecate on the same model — must also succeed (guard silent).
    deprecate = store.model_lifecycle_operation(
        "direct_current",
        operation="deprecate",
        policy_decision=_decision("models.deactivate", "direct_current"),
        request_id="req-scope-deprecate",
    )
    assert deprecate["status"] == "allowed"
    assert "LEGACY_REACTIVATION_BLOCKED" not in _preflight_blocker_codes(deprecate)
    assert store._models["direct_current"]["lifecycle_state"] == "deprecated"


# ============================================================================
# Argument compatibility guard — ensure fixtures accept as expected
# ============================================================================


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("direct_grid", "direct_grid"),
        ("legacy", "legacy"),
        ("invalid_direct_grid", "invalid_direct_grid"),
    ],
)
def test_classifier_matches_expected_state(state: str, expected: str) -> None:
    """Parametrized fixture sanity check for the three classification paths."""
    if state == "direct_grid":
        model = _direct_grid_model("sanity_direct")
    elif state == "invalid_direct_grid":
        model = _malformed_direct_grid_model("sanity_broken")
    else:
        model = _legacy_model("sanity_legacy")
    assert _classify_forcing_mapping_mode(model) == expected
