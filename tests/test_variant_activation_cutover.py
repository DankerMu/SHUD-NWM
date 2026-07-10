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

import threading
from collections.abc import Mapping
from typing import Any

import pytest

from packages.common.auth_policy import trusted_internal_policy_decision
from packages.common.model_registry import (
    PRE_ACTIVATION_HOOK_MOUNT_POINTS,
    InvalidPayloadError,
    ModelActivationContext,
    ModelLifecycleOperation,
    PostCommitPublishContext,
    PsycopgModelRegistryStore,
    _default_no_op_hook,
    _default_no_op_manifest_publisher,
    _extract_source_scope,
    _should_publish_manifest_after_commit,
    _would_be_already_current,
)
from services.orchestrator.scheduler_file_providers import FileSchedulerModelRegistry

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


# ============================================================================
# §2.2 — Post-commit manifest re-publish trigger (Epic #961 SUB-6, #967)
# ============================================================================
#
# Locks Epic #961 tasks.md §2.2: wire
# ``publish_scheduler_registry_manifest`` as the uniform post-commit tail
# of every successful dispatch-set-changing lifecycle transition
# (``activate`` / ``switch_version`` / ``rollback_version`` / a
# ``deactivate`` that removes the currently-active model via the
# sys_admin missing-active override). Re-publish MUST NOT fire on
# preflight-blocked, hook-aborted, already-current, or supersede-of-
# active (blocked by MISSING_ACTIVE_RISK) paths.
#
# Test taxonomy (function names include the ``-k`` selector tokens the
# tasks.md evidence line pins):
#   * ``manifest_republished`` — one positive test per operation type
#   * ``manifest_not_republished`` — one negative test per skip rule
#   * ``republish_per_operation`` — parametrized invariant proving
#     "exactly once per successful operation, orthogonal to op type"


class _ManifestPublisherStub:
    """Recording stub for the post-commit manifest publisher.

    Captures every :class:`PostCommitPublishContext` the seam hands over,
    plus a call counter for the ``exactly-once`` assertion. A single
    stub instance can be reused across a parametrized test because
    each parametrization instantiates a fresh store + stub pair.
    """

    def __init__(self) -> None:
        self.contexts: list[PostCommitPublishContext] = []

    @property
    def call_count(self) -> int:
        return len(self.contexts)

    def __call__(self, ctx: PostCommitPublishContext) -> None:
        self.contexts.append(ctx)


def _two_models_switch_ready(
    variant_direct_grid_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Two models with a currently-active legacy row + inactive direct-grid."""
    return [
        _model_row(model_id="legacy_m0", active_flag=True, lifecycle_state="active"),
        _model_row(
            model_id="direct_grid_m1",
            active_flag=False,
            lifecycle_state="inactive",
            resource_profile=variant_direct_grid_profile,
        ),
    ]


def _rollback_ready_models(
    variant_direct_grid_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Setup for ``rollback_version``: current active + prior superseded.

    Rollback semantics (see ``_apply_model_lifecycle_transition``):
    the addressed model is the CURRENTLY-active model; ``previous_model``
    is the row rollback restores. Preflight requires:
      * current_active_id == model_id (rollback_current_stale)
      * previous_model.lifecycle_state in {inactive, superseded}
      * previous_model.basin_version_id == model.basin_version_id
      * a trustworthy rollback_history (harness overrides below).
    """
    return [
        _model_row(
            model_id="current_active",
            active_flag=True,
            lifecycle_state="active",
            resource_profile=variant_direct_grid_profile,
        ),
        _model_row(
            model_id="restored_previous",
            active_flag=False,
            lifecycle_state="superseded",
        ),
    ]


class _RollbackReadyHarnessStore(_HarnessStore):
    """Harness variant that fakes a trustworthy rollback history.

    Reuses SUB-5's in-memory harness plumbing so the §2.2 tests exercise
    the SAME ``model_lifecycle_operation`` control flow as production;
    only the rollback-history evidence stub differs so preflight admits
    ``rollback_version``.
    """

    def _fetch_trustworthy_rollback_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        current_model: Mapping[str, Any],
        previous_model_id: str | None,
    ) -> dict[str, Any] | None:
        if previous_model_id is None:
            return None
        return {
            "trusted": True,
            "prior_audit_log_id": 42,
            "matched_previous_model_id": previous_model_id,
            "basin_version_id": current_model.get("basin_version_id"),
        }


# --- Group A: positive `manifest_republished` cases ------------------------


def test_manifest_republished_on_activate_commit(
    two_models: list[dict[str, Any]],
) -> None:
    """Successful ``activate`` commit invokes the publisher exactly once."""
    store = _HarnessStore(two_models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-manifest-activate",
    )

    assert result["status"] == "allowed"
    # Exactly-once fire on the successful commit.
    assert stub.call_count == 1
    published = stub.contexts[0]
    # Assert the context carries operation type + target so a future
    # refactor that flips the wrong operation is caught by tests.
    assert published.operation_type == "activate"
    assert published.target_model_id == "direct_grid_m1"
    assert published.basin_version_id == BASIN_VERSION_ID
    assert published.source_scope == ("gfs", "IFS")


def test_manifest_republished_on_switch_version_commit(
    two_models: list[dict[str, Any]],
) -> None:
    """Successful ``switch_version`` commit invokes the publisher exactly once."""
    store = _HarnessStore(two_models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="switch_version",
        policy_decision=_decision("models.switch_version", "direct_grid_m1"),
        request_id="req-manifest-switch",
    )

    assert result["status"] == "allowed"
    assert stub.call_count == 1
    published = stub.contexts[0]
    assert published.operation_type == "switch_version"
    assert published.target_model_id == "direct_grid_m1"
    assert published.basin_version_id == BASIN_VERSION_ID
    # Lock the scope carried on switch_version so a future refactor that
    # passes the wrong model (e.g. ``current_active`` instead of the
    # activated target) is caught in-place.
    assert published.source_scope == ("gfs", "IFS")


def test_manifest_republished_on_rollback_version_commit(
    variant_direct_grid_profile: dict[str, Any],
) -> None:
    """Successful ``rollback_version`` commit invokes the publisher exactly once.

    Uses ``_RollbackReadyHarnessStore`` so the trustworthy rollback
    history evidence check passes and preflight admits the operation.
    """
    models = _rollback_ready_models(variant_direct_grid_profile)
    store = _RollbackReadyHarnessStore(models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    result = store.model_lifecycle_operation(
        "current_active",
        operation="rollback_version",
        policy_decision=_decision("models.rollback_version", "current_active"),
        request_id="req-manifest-rollback",
        previous_model_id="restored_previous",
    )

    assert result["status"] == "rollback"
    assert stub.call_count == 1
    published = stub.contexts[0]
    assert published.operation_type == "rollback_version"
    # For rollback, the target_model_id is the RESTORED previous model.
    assert published.target_model_id == "restored_previous"
    assert published.basin_version_id == BASIN_VERSION_ID
    # Lock the scope on rollback: the publisher reads from ``transition["model"]``
    # which is the RESTORED previous model. ``restored_previous`` has no
    # ``direct_grid_forcing`` block, so the extracted scope is None. This
    # catches a future refactor that mistakenly extracts scope from the
    # superseded ``current_active`` instead of the restored previous.
    assert published.source_scope is None


def test_manifest_republished_on_deactivate_of_active_commit(
    two_models: list[dict[str, Any]],
) -> None:
    """Successful ``deactivate``-of-active via sys_admin missing-active override.

    This is the §11.2 step-4 pause-production lever: deactivating the
    currently-active model with ``override_missing_active=True`` and a
    non-empty reason (sys_admin role required, satisfied by the harness
    ``_decision`` factory). The dispatch set changes (active → none), so
    the publisher must fire.
    """
    # Only the legacy active model so the deactivate would remove the
    # active without a replacement — the override lever's contract.
    store = _HarnessStore([two_models[0]])
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    result = store.model_lifecycle_operation(
        "legacy_m0",
        operation="deactivate",
        policy_decision=_decision("models.deactivate", "legacy_m0"),
        request_id="req-manifest-deactivate-active",
        override_missing_active=True,
        reason="test: pause production via §11.2 step-4",
    )

    assert result["status"] == "allowed"
    assert stub.call_count == 1
    published = stub.contexts[0]
    assert published.operation_type == "deactivate"
    assert published.target_model_id == "legacy_m0"
    assert published.basin_version_id == BASIN_VERSION_ID
    # Lock the scope on deactivate: ``legacy_m0`` is a legacy IDW baseline
    # with no ``direct_grid_forcing`` block, so the extracted scope is
    # None. Catches a refactor that leaks a stale scope into the publisher
    # context for the pause-production lever.
    assert published.source_scope is None


# --- Group B: negative `manifest_not_republished` cases --------------------


def test_manifest_not_republished_when_preflight_blocked(
    two_models: list[dict[str, Any]],
) -> None:
    """A preflight-blocked activate must NOT invoke the publisher.

    Same poison as SUB-5's ``test_hooks_skipped_when_blocked``: point
    the target's ``model_package_uri`` at an unsupported scheme so
    ``OBJECT_URI_PREFIX_INVALID`` fires. The transaction never commits
    the transition — the publisher must stay untouched.
    """
    two_models[1]["model_package_uri"] = "ftp://unsafe/package"
    store = _HarnessStore(two_models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-manifest-blocked",
    )

    assert result["status"] == "blocked"
    assert stub.call_count == 0


def test_manifest_not_republished_when_pre_activation_hook_raises(
    two_models: list[dict[str, Any]],
) -> None:
    """A raising pre-activation hook rolls back — the publisher must NOT fire.

    The hook raises BEFORE the transition commits (SUB-5 fail-closed
    invariant), so the transaction rolls back and the publisher, which
    is staged only AFTER audit persistence succeeds inside the same
    transaction, is never reached.
    """
    store = _HarnessStore(two_models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    class _HookAbort(RuntimeError):
        pass

    def _raise(_cursor: Any, _ctx: ModelActivationContext) -> None:
        raise _HookAbort("test-hook injected fail-closed abort")

    store.register_pre_activation_hook("state_clone", _raise)

    with pytest.raises(_HookAbort):
        store.model_lifecycle_operation(
            "direct_grid_m1",
            operation="activate",
            policy_decision=_decision("models.activate", "direct_grid_m1"),
            request_id="req-manifest-hook-raises",
        )

    # Transaction rolled back — publisher never invoked.
    assert stub.call_count == 0
    # Belt-and-braces: also assert the transaction context saw the raise.
    assert store._transactions[-1]["committed"] is False


def test_manifest_not_republished_when_already_current(
    two_models: list[dict[str, Any]],
) -> None:
    """Activating an already-active target does NOT invoke the publisher.

    ``_apply_model_lifecycle_transition`` short-circuits to
    ``outcome='already_current'`` when the target is already active
    (`_apply_model_lifecycle_transition:2266-2268`). No dispatch-set
    change → no re-publish.
    """
    # Direct-grid variant is already the active model.
    two_models[0]["active_flag"] = False
    two_models[0]["lifecycle_state"] = "superseded"
    two_models[1]["active_flag"] = True
    two_models[1]["lifecycle_state"] = "active"
    store = _HarnessStore(two_models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-manifest-already-current",
    )

    assert result["status"] == "already_current"
    assert stub.call_count == 0


def test_manifest_not_republished_on_supersede_of_active_blocked(
    two_models: list[dict[str, Any]],
) -> None:
    """Standalone ``supersede`` of the currently-active model is preflight-blocked.

    ``_build_model_operation_preflight:2312-2324`` appends
    ``MISSING_ACTIVE_RISK`` for supersede on the currently-active model
    regardless of ``override_missing_active`` (the override is
    deactivate-only). The operation never commits a state change, so
    the publisher must NOT fire — defensive future-proofing per §2.2's
    "uniform post-commit tail" contract.
    """
    store = _HarnessStore(two_models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    result = store.model_lifecycle_operation(
        "legacy_m0",  # the currently-active model
        operation="supersede",
        policy_decision=_decision("models.supersede", "legacy_m0"),
        request_id="req-manifest-supersede-blocked",
    )

    assert result["status"] == "blocked"
    # The MISSING_ACTIVE_RISK blocker is present.
    blocker_codes = [b["code"] for b in result["preflight"]["blockers"]]
    assert "MISSING_ACTIVE_RISK" in blocker_codes
    # No state changes committed.
    assert store._state_updates == []
    # No manifest re-publish.
    assert stub.call_count == 0


class _AllowedDeactivateHarnessStore(_HarnessStore):
    """Harness variant that forces ``deactivate`` to land ``outcome='allowed'``.

    The natural harness path for deactivating a
    ``lifecycle_state='inactive', active_flag=False`` row lands
    ``outcome='already_current'``
    (`_apply_model_lifecycle_transition:2631-2632`) — the §2.2 predicate
    then rejects the operation via its ``transition_outcome in {'allowed',
    'rollback'}`` short-circuit before ever reaching the
    ``current_active_before.model_id == model.model_id`` guard that this
    negative test is trying to lock. This subclass forces the deactivate
    branch to return ``outcome='allowed'`` so the predicate's own guard is
    the decisive gate at the integration seam.
    """

    def _apply_model_lifecycle_transition(
        self,
        cursor: Any,
        *,
        model: Mapping[str, Any],
        current_active: Mapping[str, Any] | None,
        operation: ModelLifecycleOperation,
        previous_model: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if operation == "deactivate":
            updated = self._update_model_lifecycle_state(
                cursor, str(model["model_id"]), "inactive"
            )
            return {"outcome": "allowed", "model": updated, "previous_model": current_active}
        return super()._apply_model_lifecycle_transition(
            cursor,
            model=model,
            current_active=current_active,
            operation=operation,
            previous_model=previous_model,
        )


def test_manifest_not_republished_on_deactivate_of_non_active_model(
    two_models: list[dict[str, Any]],
) -> None:
    """Deactivating a NON-currently-active model does NOT invoke the publisher.

    ``two_models`` seeds ``legacy_m0`` as the currently-active dispatch
    target and ``direct_grid_m1`` as inactive. Deactivating
    ``direct_grid_m1`` — a row that is NOT the current dispatch target —
    does not change the dispatch set, so §2.2's contract requires no
    re-publish. This test locks the
    ``current_active_before.model_id == model.model_id`` guard in
    :func:`_should_publish_manifest_after_commit` at the integration
    seam: if that guard is deleted, the predicate returns True on
    ``outcome='allowed'`` regardless of which model was deactivated, the
    publisher fires, and this test's ``stub.call_count == 0`` assertion
    fails.

    Uses :class:`_AllowedDeactivateHarnessStore` so the transition lands
    ``outcome='allowed'`` rather than the natural
    ``outcome='already_current'`` (which the predicate rejects via its
    earlier ``transition_outcome`` check, bypassing the guard we want to
    lock).
    """
    store = _AllowedDeactivateHarnessStore(two_models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",  # NOT the currently-active model in scope
        operation="deactivate",
        policy_decision=_decision("models.deactivate", "direct_grid_m1"),
        request_id="req-manifest-deactivate-non-active",
    )

    assert result["status"] == "allowed"
    # Sanity: preflight resolved current active as legacy_m0 (the actual
    # dispatch target), while the deactivate targeted direct_grid_m1.
    # The predicate's guard rejects this combination.
    assert store._models["legacy_m0"]["active_flag"] is True
    assert store._models["legacy_m0"]["lifecycle_state"] == "active"
    # Publisher must NOT fire — direct_grid_m1 was not the dispatch target.
    assert stub.call_count == 0


# --- Group C: exactly-once invariant across operation types ----------------


@pytest.mark.parametrize(
    "operation, target_id, expected_outcome, kwargs_builder",
    [
        pytest.param(
            "activate",
            "direct_grid_m1",
            "allowed",
            lambda: {},
            id="activate",
        ),
        pytest.param(
            "switch_version",
            "direct_grid_m1",
            "allowed",
            lambda: {},
            id="switch_version",
        ),
        pytest.param(
            "deactivate",
            "legacy_m0",
            "allowed",
            lambda: {
                "override_missing_active": True,
                "reason": "test: pause production via §11.2 step-4",
            },
            id="deactivate_of_active",
        ),
    ],
)
def test_republish_per_operation_exactly_once_across_operation_types(
    variant_direct_grid_profile: dict[str, Any],
    operation: ModelLifecycleOperation,
    target_id: str,
    expected_outcome: str,
    kwargs_builder: Any,
) -> None:
    """Exactly-once fire per operation, orthogonal to operation type.

    Parametrized across activate / switch_version / deactivate-of-active
    (rollback_version is covered by its own positive test above because
    it needs the ``_RollbackReadyHarnessStore`` subclass). For every
    parametrization: one successful commit → exactly one publisher call
    carrying the correct ``operation_type``.
    """
    if operation == "deactivate":
        # Deactivate-of-active runs with only the active model in scope.
        models = [
            _model_row(
                model_id="legacy_m0",
                active_flag=True,
                lifecycle_state="active",
            )
        ]
    else:
        models = _two_models_switch_ready(variant_direct_grid_profile)
    store = _HarnessStore(models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    action_id = {
        "activate": "models.activate",
        "switch_version": "models.switch_version",
        "deactivate": "models.deactivate",
    }[operation]
    result = store.model_lifecycle_operation(
        target_id,
        operation=operation,
        policy_decision=_decision(action_id, target_id),
        request_id=f"req-per-op-{operation}",
        **kwargs_builder(),
    )
    assert result["status"] == expected_outcome
    # Exactly ONE publisher call regardless of operation type.
    assert stub.call_count == 1
    assert stub.contexts[0].operation_type == operation


def test_republish_per_operation_idempotent_second_call_does_not_double_fire(
    two_models: list[dict[str, Any]],
) -> None:
    """Second identical activate hits the already-current path → still one fire total.

    First ``activate`` commits and re-publishes (count=1). Second
    ``activate`` on the SAME target hits the already-current short
    circuit at ``_apply_model_lifecycle_transition:2266-2268`` — no
    dispatch-set change, so the publisher is not invoked again.
    The exactly-once invariant is over "operation commits", not "method
    calls".
    """
    store = _HarnessStore(two_models)
    stub = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(stub)

    result_first = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-per-op-first",
    )
    assert result_first["status"] == "allowed"
    assert stub.call_count == 1

    # Second activate: model is now already active.
    result_second = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-per-op-second",
    )
    assert result_second["status"] == "already_current"
    # Still ONE total invocation — the first commit's fire.
    assert stub.call_count == 1


# --- helper predicate coverage --------------------------------------------


def test_should_publish_manifest_after_commit_predicate_matrix() -> None:
    """Lock the dispatch-set-changing predicate contract.

    Covers every combination the seam evaluates so a future refactor
    that flips the trigger for a wrong operation fails a test.
    """
    active = {"model_id": "active_m", "active_flag": True, "lifecycle_state": "active"}
    other = {"model_id": "other_m", "active_flag": False, "lifecycle_state": "inactive"}

    # Positive: activate/switch/rollback with allowed/rollback outcome.
    for op, outcome in (
        ("activate", "allowed"),
        ("switch_version", "allowed"),
        ("rollback_version", "rollback"),
    ):
        assert _should_publish_manifest_after_commit(
            operation=op,
            transition_outcome=outcome,
            model=other,
            current_active_before=active,
        ) is True

    # Positive: deactivate-of-active with allowed outcome.
    assert _should_publish_manifest_after_commit(
        operation="deactivate",
        transition_outcome="allowed",
        model=active,
        current_active_before=active,
    ) is True

    # Negative: activate/switch/rollback with already_current outcome.
    for op in ("activate", "switch_version", "rollback_version"):
        assert _should_publish_manifest_after_commit(
            operation=op,
            transition_outcome="already_current",
            model=active,
            current_active_before=active,
        ) is False

    # Negative: deactivate of a non-current-active model.
    assert _should_publish_manifest_after_commit(
        operation="deactivate",
        transition_outcome="allowed",
        model=other,
        current_active_before=active,
    ) is False

    # Negative: deactivate with no active model to remove.
    assert _should_publish_manifest_after_commit(
        operation="deactivate",
        transition_outcome="allowed",
        model=active,
        current_active_before=None,
    ) is False

    # Negative: supersede/deprecate never publish (defensive; today
    # they are preflight-blocked when addressing active).
    for op in ("supersede", "deprecate"):
        assert _should_publish_manifest_after_commit(
            operation=op,
            transition_outcome="allowed",
            model=active,
            current_active_before=active,
        ) is False


def test_default_post_commit_manifest_publisher_is_noop(
    two_models: list[dict[str, Any]],
) -> None:
    """The default publisher is the module-level no-op.

    Locks the "no behavior change until registered" invariant so a
    future refactor cannot silently swap in a real publisher and
    reintroduce NFS side-effects into every unrelated test.
    """
    store = _HarnessStore(two_models)
    # Default publisher is the module-level no-op (byte-for-byte equal).
    assert store._post_commit_manifest_publisher is _default_no_op_manifest_publisher


# ============================================================================
# §2.3 — Permanent retirement (Epic #961 SUB-7, #968)
# ============================================================================
#
# Locks Epic #961 tasks.md §2.3: the dispatch candidate filter at
# ``services/orchestrator/scheduler_file_providers.py:119`` excludes any
# model whose ``lifecycle_state != 'active'`` — this is the primary
# defense that a *superseded* model can never re-enter production
# dispatch. Also locks the retention invariant: the superseded row is
# NOT destroyed by the cutover — it remains queryable for lineage/audit
# ("retired, not destroyed").
#
# Function names include the ``-k`` selector tokens tasks.md pins:
#   * ``superseded_exits_dispatch`` — post-cutover row absent from dispatch
#   * ``dispatch_filter_regression`` — synthetic non-active row never a candidate
#   * ``retired_not_destroyed`` — superseded row retained after cutover commit


def _seed_file_registry(rows: list[Mapping[str, Any]]) -> FileSchedulerModelRegistry:
    """Return a :class:`FileSchedulerModelRegistry` pre-seeded with ``rows``.

    Skips :meth:`FileSchedulerModelRegistry._load_once` (which parses a
    real manifest + verifies checksums) by pre-setting ``_loaded=True``
    and populating ``_models`` / ``_model_by_id`` directly. The dispatch
    filter at :meth:`FileSchedulerModelRegistry.list_models` (line 119)
    is the code under test — the loader is out of scope for §2.3.
    """
    registry = FileSchedulerModelRegistry("file:///dev/null-not-loaded")
    registry._loaded = True
    seeded: list[dict[str, Any]] = [dict(row) for row in rows]
    registry._models = seeded
    registry._model_by_id = {str(row["model_id"]): dict(row) for row in seeded}
    return registry


def test_superseded_exits_dispatch_candidate_set() -> None:
    """§2.3: a superseded row is filtered out of the active dispatch set.

    Seeds a scope with:
      * ``M1`` — currently active (post-activate cycle)
      * ``M2`` — previously active, now retired via supersede

    The dispatch filter at
    ``scheduler_file_providers.py:119`` requires BOTH
    ``active_flag != False`` AND
    ``str(row.get('lifecycle_state') or 'active') == 'active'`` —
    so ``M2`` is excluded even if a residual ``active_flag`` bit
    were still True on the row.
    """
    rows = [
        {
            "model_id": "M1",
            "basin_id": BASIN_ID,
            "basin_version_id": BASIN_VERSION_ID,
            "active_flag": True,
            "lifecycle_state": "active",
        },
        {
            "model_id": "M2",
            "basin_id": BASIN_ID,
            "basin_version_id": BASIN_VERSION_ID,
            "active_flag": False,
            "lifecycle_state": "superseded",
        },
    ]
    registry = _seed_file_registry(rows)

    page = registry.list_models(
        basin_version_id=BASIN_VERSION_ID,
        active=True,
        limit=10,
        offset=0,
    )

    ids = [row["model_id"] for row in page["items"]]
    assert ids == ["M1"], f"expected M1 alone, got {ids!r}"
    assert page["total"] == 1


@pytest.mark.parametrize(
    "non_active_state",
    ["inactive", "superseded", "deprecated"],
)
def test_dispatch_filter_regression_synthetic_non_active_row(
    non_active_state: str,
) -> None:
    """§2.3: any non-``active`` lifecycle_state is never a dispatch candidate.

    Even with a residual ``active_flag=True`` (an inconsistent state
    that a future refactor might inadvertently produce), a row whose
    ``lifecycle_state`` is one of ``inactive`` / ``superseded`` /
    ``deprecated`` MUST NOT appear in ``list_models(active=True)``.
    If a future refactor collapses the filter to only check
    ``active_flag``, this test fails on all three parametrizations,
    catching the regression at the exact seam.
    """
    rows = [
        {
            "model_id": f"synth_{non_active_state}",
            "basin_id": BASIN_ID,
            "basin_version_id": BASIN_VERSION_ID,
            # Intentionally inconsistent with lifecycle_state — the
            # ``lifecycle_state`` check must be authoritative.
            "active_flag": True,
            "lifecycle_state": non_active_state,
        },
    ]
    registry = _seed_file_registry(rows)

    page = registry.list_models(
        basin_version_id=BASIN_VERSION_ID,
        active=True,
        limit=10,
        offset=0,
    )

    assert page["items"] == [], (
        f"lifecycle_state={non_active_state!r} must NEVER appear in the "
        "active dispatch candidate set (filter is authoritative)."
    )
    assert page["total"] == 0


def test_retired_not_destroyed_superseded_row_retained_after_cutover(
    two_models: list[dict[str, Any]],
) -> None:
    """§2.3: after activate M2 supersedes M1, the M1 row is retained.

    Runs a real activation cycle over the SUB-5 harness. After the
    commit, the prior-active ``legacy_m0`` row must:
      * still exist in the ``core.model_instance`` store (retained,
        not deleted — critical for lineage/audit/rollback),
      * carry ``lifecycle_state='superseded'`` (immutable retirement
        marker),
      * carry ``active_flag=False`` (no longer dispatchable).
    """
    store = _HarnessStore(two_models)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-retired-not-destroyed",
    )

    assert result["status"] == "allowed"

    # Prior-active row RETAINED in the store — the cutover records a
    # supersede transition, not a destroy.
    assert "legacy_m0" in store._models, (
        "prior-active row must be retained for lineage/audit — cutover is "
        "supersede, not delete."
    )
    superseded = store._models["legacy_m0"]
    assert superseded["lifecycle_state"] == "superseded"
    assert superseded["active_flag"] is False

    # Post-condition on the newly-active row for symmetry.
    new_active = store._models["direct_grid_m1"]
    assert new_active["lifecycle_state"] == "active"
    assert new_active["active_flag"] is True


# ============================================================================
# §2.4 — Concurrency + idempotency (Epic #961 SUB-7, #968)
# ============================================================================
#
# Locks Epic #961 tasks.md §2.4: the cutover is safe under two
# racing activation requests for the same
# ``(basin_id, basin_version_id)`` scope, and repeating a completed
# activation is a true no-op — no duplicate transition, no second hook
# fire, no manifest re-publish, no second ``allowed``/``rollback``
# audit row.
#
# The production authority is the ``FOR UPDATE`` scope lock acquired at
# ``packages/common/model_registry.py`` line 1786 (via
# ``_lock_basin_version_scope``) — the harness models this with a real
# ``threading.Lock`` held across the whole transaction context so two
# threads faithfully serialize on scope.
#
# Function names include the ``-k`` selector tokens tasks.md pins:
#   * ``concurrent_activation`` — two-thread race → exactly one active
#   * ``repeat_already_current`` — second identical call is a true no-op


class _SerializingFakeTransaction:
    """Transaction context that holds the harness scope lock end-to-end.

    Approximates the production ``FOR UPDATE`` semantics: the row lock
    is taken as soon as we enter the transaction and released when the
    transaction context exits (commit or rollback). A racing thread
    waiting on the same scope blocks on ``__enter__`` until the holder
    exits, so the second caller re-reads state ONLY after the first
    caller has committed.
    """

    def __init__(self, harness: _SerializingHarnessStore) -> None:
        self._harness = harness

    def __enter__(self) -> _RecordingCursor:
        # Acquire BEFORE any state mutation so racing threads serialize
        # cleanly on ``_transactions.append`` / ``_current_cursor`` and
        # on subsequent reads of ``self._models``.
        self._harness._scope_lock.acquire()
        cursor = _RecordingCursor()
        self._harness._transactions.append({"cursor": cursor, "committed": None})
        self._harness._current_cursor = cursor
        return cursor

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: Any,
    ) -> bool:
        state = self._harness._transactions[-1]
        state["committed"] = exc_type is None
        self._harness._current_cursor = None
        self._harness._scope_lock.release()
        return False


class _SerializingHarnessStore(_HarnessStore):
    """Harness variant that holds a real ``threading.Lock`` per scope.

    Models the production ``FOR UPDATE`` semantics: the scope lock is
    held for the entire transaction (from ``_transaction().__enter__``
    to ``__exit__``), so a second racing thread on the same scope
    blocks until the first thread commits, then re-reads the freshly
    committed state.
    """

    def __init__(self, models: list[Mapping[str, Any]]) -> None:
        super().__init__(models)
        object.__setattr__(self, "_scope_lock", threading.Lock())
        object.__setattr__(self, "_scope_lock_acquisitions", [])

    def _transaction(self) -> _SerializingFakeTransaction:
        return _SerializingFakeTransaction(self)

    def _lock_basin_version_scope(self, cursor: Any, basin_version_id: str) -> None:  # noqa: ARG002
        # The transaction context already holds ``_scope_lock`` — this
        # method's job in production is to acquire the row-level FOR
        # UPDATE, which we've folded into the transaction wrapper. Just
        # record the call for evidence.
        self._scope_lock_acquisitions.append(basin_version_id)


def test_concurrent_activation_leaves_exactly_one_active_and_stable_loser(
    two_models: list[dict[str, Any]],
) -> None:
    """§2.4: two racing activate calls → exactly one active + stable loser.

    Two threads simultaneously call
    ``model_lifecycle_operation(operation='activate')`` targeting
    ``direct_grid_m1`` on the same
    ``(basin_id, basin_version_id)`` scope. The
    :class:`_SerializingHarnessStore` scope lock (modeling production
    FOR UPDATE at ``model_registry.py:1786``) serializes them:

      * The winning thread commits the swap: ``legacy_m0`` becomes
        ``superseded``, ``direct_grid_m1`` becomes ``active`` — outcome
        ``allowed``.
      * The losing thread waits, then re-reads under the lock, sees
        ``direct_grid_m1`` already active, and returns
        ``already_current`` — the stable idempotent losing outcome.

    Post-condition: EXACTLY ONE model has
    ``active_flag=True and lifecycle_state='active'``; both threads
    acquired the scope lock; the outcomes are the deterministic
    ``{allowed, already_current}`` pair.
    """
    store = _SerializingHarnessStore(two_models)
    barrier = threading.Barrier(2)
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, BaseException] = {}

    def _caller(name: str) -> None:
        try:
            # Force both threads to reach the call site as close in
            # time as possible so the race is realistic.
            barrier.wait(timeout=5)
            results[name] = store.model_lifecycle_operation(
                "direct_grid_m1",
                operation="activate",
                policy_decision=_decision("models.activate", "direct_grid_m1"),
                request_id=f"req-concurrent-{name}",
            )
        except BaseException as exc:  # pragma: no cover - surfaced via join assertion
            errors[name] = exc

    t_a = threading.Thread(target=_caller, args=("A",), name="activate-A")
    t_b = threading.Thread(target=_caller, args=("B",), name="activate-B")
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert not t_a.is_alive() and not t_b.is_alive(), "activate threads deadlocked"
    assert errors == {}, f"unexpected thread errors: {errors!r}"

    # Both callers acquired the scope lock — serialization proof.
    assert store._scope_lock_acquisitions == [BASIN_VERSION_ID, BASIN_VERSION_ID]

    # Two deterministic outcomes: exactly one 'allowed', exactly one
    # 'already_current'.
    statuses = sorted(r["status"] for r in results.values())
    assert statuses == ["allowed", "already_current"], (
        f"expected one winner + one stable loser, got {statuses!r}"
    )

    # Exactly ONE model is active in the store — the invariant the
    # scope lock exists to protect.
    active_rows = [
        row
        for row in store._models.values()
        if bool(row.get("active_flag")) and str(row.get("lifecycle_state")) == "active"
    ]
    assert len(active_rows) == 1
    assert active_rows[0]["model_id"] == "direct_grid_m1"

    # Prior-active row was superseded exactly once (winner's swap);
    # the loser saw the already-current state and did NOT re-supersede.
    assert store._models["legacy_m0"]["lifecycle_state"] == "superseded"
    # State updates capture exactly one supersede + one activate — the
    # loser produced no state transition.
    assert store._state_updates == [
        ("legacy_m0", "superseded", False),
        ("direct_grid_m1", "active", True),
    ]


def test_repeat_already_current_no_second_transition_no_hook_no_manifest_no_audit(
    two_models: list[dict[str, Any]],
) -> None:
    """§2.4: repeat activate of the already-current target is a true no-op.

    Contract locked (per tasks.md §2.4 "Cutover is concurrency-safe
    and idempotent"):

      1. First call: successful transition — pre-activation hooks
         fire once, manifest publisher fires once, one
         ``outcome IN ('allowed','rollback')`` audit row appended,
         ``active_flag=True`` on the target.
      2. Second call (same target, now already-active):
         * returns ``already_current``,
         * ``_state_updates`` unchanged (no duplicate supersede+activate),
         * pre-activation hook counter unchanged (no second fire),
         * manifest publisher ``call_count`` unchanged (no re-publish),
         * NO additional ``outcome IN ('allowed','rollback')`` audit
           row (an already-current audit row MAY be appended, but that
           has a distinct outcome and is not a second "success" row).
    """
    store = _HarnessStore(two_models)
    publisher = _ManifestPublisherStub()
    store.register_post_commit_manifest_publisher(publisher)

    hook_calls: list[str] = []

    def _spy(name: str) -> Any:
        def _hook(_cursor: Any, _ctx: ModelActivationContext) -> None:
            hook_calls.append(name)

        return _hook

    store.register_pre_activation_hook("state_clone", _spy("state_clone"))
    store.register_pre_activation_hook("station_flag_flip", _spy("station_flag_flip"))

    # --- Call 1: real transition -------------------------------------
    result_1 = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-repeat-first",
    )
    assert result_1["status"] == "allowed"
    assert store._models["direct_grid_m1"]["active_flag"] is True
    assert store._state_updates == [
        ("legacy_m0", "superseded", False),
        ("direct_grid_m1", "active", True),
    ]
    # Both hooks fired exactly once, in declared order.
    assert hook_calls == list(PRE_ACTIVATION_HOOK_MOUNT_POINTS)
    # Manifest publisher fired exactly once.
    assert publisher.call_count == 1
    # Exactly one ``allowed``/``rollback`` audit row.
    first_success_rows = [
        row for row in store.audit_rows if row["outcome"] in {"allowed", "rollback"}
    ]
    assert len(first_success_rows) == 1

    # Snapshot post-call-1 state so the assertions below are anchored
    # to the exact evidence signatures, not to hardcoded counts.
    state_updates_after_1 = list(store._state_updates)
    hook_calls_after_1 = list(hook_calls)
    publisher_calls_after_1 = publisher.call_count
    success_audit_after_1 = len(first_success_rows)

    # --- Call 2: idempotent already-current path ---------------------
    result_2 = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-repeat-second",
    )
    assert result_2["status"] == "already_current"

    # No duplicate supersede+activate transition.
    assert store._state_updates == state_updates_after_1
    # No second pre-activation hook fire (the already-current gate at
    # ``model_lifecycle_operation`` short-circuits the hook chain).
    assert hook_calls == hook_calls_after_1
    # No manifest re-publish (already-current is not a
    # dispatch-set-changing commit; see SUB-6 predicate).
    assert publisher.call_count == publisher_calls_after_1
    # No additional ``outcome IN ('allowed','rollback')`` audit row.
    # An already-current audit row MAY have been appended, but it has
    # a distinct outcome — the spec's "no second activation-success
    # audit row" claim maps precisely to this filter.
    success_rows_after_2 = [
        row for row in store.audit_rows if row["outcome"] in {"allowed", "rollback"}
    ]
    assert len(success_rows_after_2) == success_audit_after_1
