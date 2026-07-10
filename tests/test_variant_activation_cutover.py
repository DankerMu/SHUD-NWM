"""Unit tests for §2.1 pre-activation extension-point contract.

Covers Epic #961 / ``openspec/changes/source-specific-model-variant-routing``
tasks.md §2.1 "Variant Activation Cutover" evidence:

* ``-k "hooks_ordered or hooks_empty or hook_raises_aborts"`` proves the
  empty hooks run in declared order before the supersede+activate swap
  and mutate no state, and that a deliberately raising test hook rolls
  back the whole transaction with no model activated, no model
  superseded, and no manifest re-published.
* The single-transaction reuse is proven by asserting the swap still
  supersedes the prior active model and activates the target on the
  same cursor with exactly one ``ops.audit_log`` row and no additional
  table writes beyond ``core.model_instance`` and ``ops.audit_log``.
* Skip rules assert hooks do NOT fire when: preflight is blocked, the
  operation is already-current, or the operation is not one of
  ``activate`` / ``switch_version`` / ``rollback_version``.

The tests use a subclass of :class:`PsycopgModelRegistryStore` that
overrides the DB-touching helpers with in-memory equivalents so the
real ``model_lifecycle_operation`` logic — preflight, hook dispatch,
transition, audit — flows unchanged. This locks the seam without
requiring a live TimescaleDB.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from packages.common.auth_policy import trusted_internal_policy_decision
from packages.common.model_registry import (
    PRE_ACTIVATION_HOOK_MOUNT_POINTS,
    InvalidPayloadError,
    ModelActivationContext,
    ModelLifecycleOperation,
    PsycopgModelRegistryStore,
    _default_no_op_hook,
    _extract_source_scope,
    _would_be_already_current,
)

# --- test harness ----------------------------------------------------------


BASIN_VERSION_ID = "basin_v01"
BASIN_ID = "basin_a"


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
    row lands ``status='ready'`` for activation-class operations.
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
        "created_at": "2026-05-07T00:00:00Z",
    }


class _RecordingCursor:
    """Fake cursor that records every SQL statement passed through it.

    Used as the transaction cursor for the harness store; the harness
    overrides every ``_fetch_*`` / ``_update_*`` / ``_insert_*`` helper
    so no statements should reach this cursor. Recording still catches
    an accidental raw ``cursor.execute`` regression.
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self._pending: dict[str, Any] | None = None

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:  # pragma: no cover - regression guard
        self.statements.append((statement, tuple(parameters)))

    def fetchone(self) -> dict[str, Any] | None:  # pragma: no cover - regression guard
        result = self._pending
        self._pending = None
        return result


class _FakeTransaction:
    """Context manager wrapper around a :class:`_RecordingCursor`.

    Tracks enter/exit so the tests can assert the swap and audit happen
    in ONE transaction (single-transaction reuse, §2.1 spec scenario
    "Cutover reuses the existing single-transaction activation
    lifecycle").
    """

    def __init__(self, harness: _HarnessStore) -> None:
        self._harness = harness

    def __enter__(self) -> _RecordingCursor:
        cursor = _RecordingCursor()
        self._harness._transactions.append({"cursor": cursor, "committed": None})
        self._harness._current_cursor = cursor
        return cursor

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, _tb: Any) -> bool:
        state = self._harness._transactions[-1]
        state["committed"] = exc_type is None
        self._harness._current_cursor = None
        return False


class _HarnessStore(PsycopgModelRegistryStore):
    """In-memory PsycopgModelRegistryStore for §2.1 hook-contract tests.

    Overrides the DB-touching helpers so the real
    ``model_lifecycle_operation`` — preflight, hook dispatch, transition,
    audit — runs unchanged over ``self._models`` and ``self.audit_rows``.
    """

    def __init__(self, models: list[Mapping[str, Any]]) -> None:
        super().__init__("postgresql://harness")
        # Frozen dataclass: bypass with ``object.__setattr__``.
        object.__setattr__(self, "_models", {row["model_id"]: dict(row) for row in models})
        object.__setattr__(self, "audit_rows", [])
        object.__setattr__(self, "_transactions", [])
        object.__setattr__(self, "_current_cursor", None)
        object.__setattr__(self, "_state_updates", [])  # (model_id, lifecycle_state, active_flag)

    # ---- transaction plumbing --------------------------------------------

    def _transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    # ---- read helpers (all cursor arg unused for in-memory backend) ------

    def _lock_basin_version_scope(self, cursor: Any, basin_version_id: str) -> None:  # noqa: ARG002
        # basin_version_id is presumed to exist for the harness.
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
            "previous_model_id": previous_model["model_id"] if previous_model else None,
        }
        self.audit_rows.append(entry)
        return len(self.audit_rows)


# --- fixtures --------------------------------------------------------------


def _decision(action_id: str, target_id: str) -> Any:
    return trusted_internal_policy_decision(
        action_id,
        target_type="model_instance",
        target_id=target_id,
        actor_id="test:harness",
        roles=("sys_admin",),
    )


@pytest.fixture
def variant_direct_grid_profile() -> dict[str, Any]:
    """A direct-grid ``resource_profile`` extras dict for the target model.

    Locks the source-scope extraction path used by the hook context.
    """
    return {
        "canonical_grid_key": "canonical_key_grid_a_v1",
        "direct_grid_forcing": {
            "forcing_mapping_mode": "direct_grid",
            "applicable_source_ids": ["gfs", "IFS"],
            "grid_id": "grid_a",
        },
    }


@pytest.fixture
def two_models(variant_direct_grid_profile: dict[str, Any]) -> list[dict[str, Any]]:
    """A legacy IDW active baseline + an inactive direct-grid variant."""

    return [
        _model_row(
            model_id="legacy_m0",
            active_flag=True,
            lifecycle_state="active",
        ),
        _model_row(
            model_id="direct_grid_m1",
            active_flag=False,
            lifecycle_state="inactive",
            resource_profile=variant_direct_grid_profile,
        ),
    ]


# --- Group A: hook order, empty defaults, raising hook aborts --------------


def test_hooks_ordered_run_in_declared_order_before_swap(
    two_models: list[dict[str, Any]],
) -> None:
    """The hook chain fires in ``PRE_ACTIVATION_HOOK_MOUNT_POINTS`` order.

    Registered spies record the mount-point name; the swap runs only
    after both hooks return (verified by asserting the swap happened
    AFTER the hooks did).
    """
    store = _HarnessStore(two_models)
    call_log: list[tuple[str, ModelActivationContext, int]] = []

    def _spy(name: str) -> Any:
        def _hook(_cursor: Any, ctx: ModelActivationContext) -> None:
            # Assert the swap has NOT run yet (still legacy_m0 active).
            state_at_hook_time = len(store._state_updates)
            call_log.append((name, ctx, state_at_hook_time))

        return _hook

    store.register_pre_activation_hook("state_clone", _spy("state_clone"))
    store.register_pre_activation_hook("station_flag_flip", _spy("station_flag_flip"))

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-hooks-ordered",
    )

    assert result["status"] == "allowed"
    # Two hooks fired, in declared order.
    assert [entry[0] for entry in call_log] == list(PRE_ACTIVATION_HOOK_MOUNT_POINTS)
    # Both hooks saw ZERO state updates — the swap fires AFTER the last hook.
    assert [entry[2] for entry in call_log] == [0, 0]
    # Post-condition: the swap did fire (2 updates: supersede legacy, activate direct).
    assert len(store._state_updates) == 2

    # The activation context carries the expected fields.
    first_ctx = call_log[0][1]
    assert first_ctx.basin_version_id == BASIN_VERSION_ID
    assert first_ctx.previous_active_model is not None
    assert first_ctx.previous_active_model["model_id"] == "legacy_m0"
    assert first_ctx.target_model["model_id"] == "direct_grid_m1"
    assert first_ctx.source_scope == ("gfs", "IFS")


def test_hooks_empty_default_preserves_lifecycle_behavior(
    two_models: list[dict[str, Any]],
) -> None:
    """With default no-op hooks, activation supersedes and activates as before.

    Locks the "no-behavior-change" invariant: exactly one audit row,
    the target activates, the prior active is superseded, and no
    external mutation happens on either hook mount point.
    """
    store = _HarnessStore(two_models)
    # Sanity: default hooks are exactly the module-level no-op.
    for mount_point in PRE_ACTIVATION_HOOK_MOUNT_POINTS:
        assert store._pre_activation_hooks[mount_point] is _default_no_op_hook

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-hooks-empty",
    )

    assert result["status"] == "allowed"
    # The direct-grid variant is now active; the legacy baseline is superseded.
    assert store._models["direct_grid_m1"]["active_flag"] is True
    assert store._models["direct_grid_m1"]["lifecycle_state"] == "active"
    assert store._models["legacy_m0"]["active_flag"] is False
    assert store._models["legacy_m0"]["lifecycle_state"] == "superseded"
    # Exactly ONE audit row, with outcome ``allowed``.
    assert len(store.audit_rows) == 1
    assert store.audit_rows[0]["outcome"] == "allowed"
    assert store.audit_rows[0]["operation"] == "activate"


def test_hook_raises_aborts_whole_transaction(
    two_models: list[dict[str, Any]],
) -> None:
    """A raising hook rolls the whole transaction back — no partial state.

    Locks §2.1 scenario "A raising hook aborts the whole transaction":
    the raising hook fires at ``state_clone``; the second hook must NOT
    fire; the target does NOT activate; the prior active is NOT
    superseded; no ``outcome IN ('allowed','rollback')`` audit row is
    written; and the transaction context manager sees the exception at
    exit (committed=False).
    """
    store = _HarnessStore(two_models)

    class _HookAbort(RuntimeError):
        pass

    second_hook_calls: list[Any] = []

    def _raise(_cursor: Any, _ctx: ModelActivationContext) -> None:
        raise _HookAbort("test-hook injected fail-closed abort")

    def _second_spy(cursor: Any, ctx: ModelActivationContext) -> None:  # pragma: no cover - guard
        second_hook_calls.append((cursor, ctx))

    store.register_pre_activation_hook("state_clone", _raise)
    store.register_pre_activation_hook("station_flag_flip", _second_spy)

    with pytest.raises(_HookAbort, match="test-hook injected fail-closed abort"):
        store.model_lifecycle_operation(
            "direct_grid_m1",
            operation="activate",
            policy_decision=_decision("models.activate", "direct_grid_m1"),
            request_id="req-hook-raises",
        )

    # Second hook must NOT have fired (ordered dispatch stops at the raise).
    assert second_hook_calls == []
    # No state updates — the transaction rolled back.
    assert store._state_updates == []
    # Target stays inactive; prior active stays active.
    assert store._models["direct_grid_m1"]["active_flag"] is False
    assert store._models["direct_grid_m1"]["lifecycle_state"] == "inactive"
    assert store._models["legacy_m0"]["active_flag"] is True
    assert store._models["legacy_m0"]["lifecycle_state"] == "active"
    # No allowed/rollback audit row — the transaction rolled back before
    # ``_insert_model_lifecycle_audit`` for a successful outcome. Blocked
    # audit rows can exist for preflight-failed operations but not here.
    allowed_or_rollback = [row for row in store.audit_rows if row["outcome"] in {"allowed", "rollback"}]
    assert allowed_or_rollback == []
    # The transaction context manager sees the raise (committed=False).
    assert store._transactions[-1]["committed"] is False


# --- Group B: single-transaction reuse -----------------------------------


def test_single_transaction_swap_and_audit(
    two_models: list[dict[str, Any]],
) -> None:
    """Cutover reuses ONE transaction: swap + audit on the same cursor.

    Locks the spec scenario "Activating the variant supersedes the
    prior active model in one transaction": exactly ONE transaction
    open+commit, exactly TWO state updates (supersede legacy, activate
    direct), exactly ONE audit row with an ``allowed`` outcome.
    """
    store = _HarnessStore(two_models)

    # Also register spy hooks so we can verify the sequence:
    # hooks-before-swap AND both-on-the-same-cursor.
    seen_cursors: list[Any] = []

    def _spy_cursor(cursor: Any, _ctx: ModelActivationContext) -> None:
        seen_cursors.append(cursor)

    store.register_pre_activation_hook("state_clone", _spy_cursor)
    store.register_pre_activation_hook("station_flag_flip", _spy_cursor)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-single-tx",
    )

    assert result["status"] == "allowed"
    # Exactly ONE transaction, committed.
    assert len(store._transactions) == 1
    assert store._transactions[0]["committed"] is True
    committed_cursor = store._transactions[0]["cursor"]
    # Both hooks saw the SAME cursor as the transaction cursor.
    assert seen_cursors == [committed_cursor, committed_cursor]
    # Two lifecycle-state updates: legacy -> superseded, then direct -> active.
    assert store._state_updates == [
        ("legacy_m0", "superseded", False),
        ("direct_grid_m1", "active", True),
    ]
    # Exactly ONE audit row with ``outcome='allowed'``.
    assert [row["outcome"] for row in store.audit_rows] == ["allowed"]


# --- Group C: skip rules -------------------------------------------------


def test_hooks_skipped_when_blocked(
    two_models: list[dict[str, Any]],
) -> None:
    """A preflight-blocked activation does NOT run hooks.

    Contrive a blocker by pointing the target's ``model_package_uri``
    at an unsupported scheme so the ``OBJECT_URI_PREFIX_INVALID``
    blocker fires (``_activation_safety_evidence:2888``). Hooks must
    not fire; the swap must not run; a blocked audit row is written.
    """
    # Poison the direct-grid variant's model_package_uri.
    two_models[1]["model_package_uri"] = "ftp://unsafe/package"
    store = _HarnessStore(two_models)

    hook_calls: list[str] = []

    def _spy(name: str) -> Any:
        def _hook(_cursor: Any, _ctx: ModelActivationContext) -> None:
            hook_calls.append(name)

        return _hook

    store.register_pre_activation_hook("state_clone", _spy("state_clone"))
    store.register_pre_activation_hook("station_flag_flip", _spy("station_flag_flip"))

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-blocked",
    )

    assert result["status"] == "blocked"
    # Hooks did NOT fire.
    assert hook_calls == []
    # No lifecycle state updates.
    assert store._state_updates == []
    # A ``blocked`` audit row was written.
    assert [row["outcome"] for row in store.audit_rows] == ["blocked"]


def test_hooks_skipped_when_already_current(
    two_models: list[dict[str, Any]],
) -> None:
    """Activating an already-active target does NOT run hooks (idempotent path).

    Locks §2.1 scenario "Hooks do not run on the already-current path".
    """
    # Make the direct-grid variant already the active model.
    two_models[0]["active_flag"] = False
    two_models[0]["lifecycle_state"] = "superseded"
    two_models[1]["active_flag"] = True
    two_models[1]["lifecycle_state"] = "active"
    store = _HarnessStore(two_models)

    hook_calls: list[str] = []

    def _spy(name: str) -> Any:
        def _hook(_cursor: Any, _ctx: ModelActivationContext) -> None:
            hook_calls.append(name)

        return _hook

    store.register_pre_activation_hook("state_clone", _spy("state_clone"))
    store.register_pre_activation_hook("station_flag_flip", _spy("station_flag_flip"))

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-already-current",
    )

    assert result["status"] == "already_current"
    # Hooks did NOT fire.
    assert hook_calls == []
    # No lifecycle state updates (already-current path).
    assert store._state_updates == []
    # No allowed/rollback audit row for the already-current path.
    allowed_or_rollback = [row for row in store.audit_rows if row["outcome"] in {"allowed", "rollback"}]
    assert allowed_or_rollback == []


def test_hooks_skipped_on_deactivate(
    two_models: list[dict[str, Any]],
) -> None:
    """``deactivate`` never runs hooks — no swap → no clone/flip target.

    Uses the sys_admin missing-active override with a non-empty reason
    so the deactivate reaches the transition site (per D5 the override
    is deactivate-only). The hook chain must remain untouched — no
    activation-class swap is happening.
    """
    # Only the legacy active model — deactivating it removes the current
    # active without a replacement, so we set override_missing_active=True.
    store = _HarnessStore([two_models[0]])

    hook_calls: list[str] = []

    def _spy(name: str) -> Any:
        def _hook(_cursor: Any, _ctx: ModelActivationContext) -> None:
            hook_calls.append(name)

        return _hook

    store.register_pre_activation_hook("state_clone", _spy("state_clone"))
    store.register_pre_activation_hook("station_flag_flip", _spy("station_flag_flip"))

    result = store.model_lifecycle_operation(
        "legacy_m0",
        operation="deactivate",
        policy_decision=_decision("models.deactivate", "legacy_m0"),
        request_id="req-deactivate",
        override_missing_active=True,
        reason="test: prove hooks skip on deactivate",
    )

    assert result["status"] == "allowed"
    # Hooks did NOT fire — this is a deactivate, no clone/flip target.
    assert hook_calls == []
    # State updated once (legacy_m0 -> inactive).
    assert store._state_updates == [("legacy_m0", "inactive", False)]


# --- helper predicates -----------------------------------------------------


def test_would_be_already_current_matches_transition_check() -> None:
    """The hook-skip predicate mirrors ``_apply_model_lifecycle_transition``.

    Locks the shared invariant so a future refactor that changes the
    already-current check on one side without the other fails a test.
    """
    active_model = {"active_flag": True, "lifecycle_state": "active"}
    inactive_model = {"active_flag": False, "lifecycle_state": "inactive"}
    superseded_model = {"active_flag": False, "lifecycle_state": "superseded"}

    for op in ("activate", "switch_version"):
        assert _would_be_already_current(active_model, op) is True
        assert _would_be_already_current(inactive_model, op) is False
        assert _would_be_already_current(superseded_model, op) is False

    # Non-activation-class operations never gate on this helper.
    for op in ("deactivate", "supersede", "deprecate", "rollback_version"):
        assert _would_be_already_current(active_model, op) is False


def test_extract_source_scope_handles_legacy_and_direct_grid() -> None:
    """Source scope: tuple of ids for direct-grid; None for legacy IDW."""
    legacy = {"resource_profile": {"manifest_uri": "s3://legacy"}}
    assert _extract_source_scope(legacy) is None

    direct_grid = {
        "resource_profile": {
            "direct_grid_forcing": {
                "applicable_source_ids": ["gfs", "IFS"],
            }
        }
    }
    scope = _extract_source_scope(direct_grid)
    assert scope == ("gfs", "IFS")
    assert isinstance(scope, tuple)

    # Malformed ``applicable_source_ids`` shape → None (fail-closed).
    malformed = {
        "resource_profile": {"direct_grid_forcing": {"applicable_source_ids": "gfs"}}
    }
    assert _extract_source_scope(malformed) is None


def test_register_pre_activation_hook_rejects_unknown_mount_point(
    two_models: list[dict[str, Any]],
) -> None:
    """Typoed mount points raise instead of silently registering nothing."""
    store = _HarnessStore(two_models)

    def _hook(_cursor: Any, _ctx: ModelActivationContext) -> None:
        return None

    with pytest.raises(InvalidPayloadError, match="Unknown pre-activation mount point"):
        store.register_pre_activation_hook("misspelled_mount", _hook)
