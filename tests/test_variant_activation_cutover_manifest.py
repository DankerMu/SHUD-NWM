"""Unit tests for the §2.2 post-commit manifest re-publish trigger (Epic #961 SUB-6, #967).

Partition of ``tests/test_variant_activation_cutover.py`` (#3165). The
in-memory harness, the ``two_models`` / ``variant_direct_grid_profile``
fixtures and ``_ManifestPublisherStub`` are in
``tests/variant_activation_cutover_helpers.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from packages.common.model_registry import (
    ModelActivationContext,
    ModelLifecycleOperation,
    _default_no_op_manifest_publisher,
    _should_publish_manifest_after_commit,
)
from tests.variant_activation_cutover_helpers import (
    BASIN_VERSION_ID,
    _decision,
    _HarnessStore,
    _ManifestPublisherStub,
    _model_row,
    two_models,
    variant_direct_grid_profile,
)

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
