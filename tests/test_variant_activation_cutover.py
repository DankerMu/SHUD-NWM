"""Unit tests for the variant activation cutover transaction.

Covers Epic #961 / ``openspec/changes/source-specific-model-variant-routing``
tasks.md §2.1 "Variant Activation Cutover" evidence that outlived #3189's
retirement of the activation hook chain:

* The single-transaction reuse is proven by asserting the swap supersedes the
  prior active model and activates the target on the transaction's own cursor,
  with exactly one ``ops.audit_log`` row written on that same cursor and no
  other statement reaching it (no ``hydro.state_snapshot`` row).
* The lifecycle operation takes no state-clone input and its result carries
  no state-clone block (``atomic-cutover-transaction`` "Model activation SHALL
  NOT clone state").
* A preflight-blocked activation writes one ``blocked`` audit row and no
  lifecycle state update.
* A committed activation publishes nothing to the node-22 scheduler file plane
  and the store exposes no publisher registration
  (``variant-activation-cutover`` "A model lifecycle transition SHALL NOT
  publish the scheduler file plane", #3204); a standalone ``supersede`` of the
  active model is preflight-blocked with no state transition; an
  audit-persistence failure rolls the transition back.

The tests use a subclass of :class:`PsycopgModelRegistryStore` that
overrides the DB-touching helpers with in-memory equivalents so the
real ``model_lifecycle_operation`` logic — preflight, transition, audit —
flows unchanged. This locks the seam without requiring a live TimescaleDB.

#3165 split this suite under the 1000-line guard. This base path keeps the
transaction cases; the §2.3/§2.4 dispatch, retirement and concurrency cases
live in ``tests/test_variant_activation_cutover_dispatch.py``. The shared
harness and the ``two_models`` / ``variant_direct_grid_profile`` fixtures are
in ``tests/variant_activation_cutover_helpers.py``.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from packages.common.model_registry import PsycopgModelRegistryStore
from tests.variant_activation_cutover_helpers import (
    _decision,
    _HarnessStore,
    two_models,
    variant_direct_grid_profile,
)

# --- single-transaction reuse --------------------------------------------


def test_single_transaction_swap_and_audit(
    two_models: list[dict[str, Any]],
) -> None:
    """Cutover reuses ONE transaction: swap + audit on the same cursor.

    Locks the spec scenario "Activating the variant supersedes the
    prior active model in one transaction": exactly ONE transaction
    open+commit, exactly TWO state updates (supersede legacy, activate
    direct), exactly ONE audit row with an ``allowed`` outcome, and every
    one of those writes landing on the committed transaction's cursor.
    """
    store = _HarnessStore(two_models)

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
    # The swap's two state updates and the audit insert all ran on the
    # committed transaction's cursor, in that order.
    assert [(kind, cursor is committed_cursor) for kind, cursor in store._write_cursors] == [
        ("state_update", True),
        ("state_update", True),
        ("audit", True),
    ]
    # Two lifecycle-state updates: legacy -> superseded, then direct -> active.
    assert store._state_updates == [
        ("legacy_m0", "superseded", False),
        ("direct_grid_m1", "active", True),
    ]
    # Exactly ONE audit row with ``outcome='allowed'``, for the activation.
    assert [row["outcome"] for row in store.audit_rows] == ["allowed"]
    assert store.audit_rows[0]["operation"] == "activate"
    # Nothing else reached the cursor: the activation writes no
    # ``hydro.state_snapshot`` row (or any other raw statement).
    assert committed_cursor.statements == []


def test_lifecycle_operation_signature_and_result_carry_no_state_clone_input(
    two_models: list[dict[str, Any]],
) -> None:
    """The lifecycle operation takes no cold-start input and returns no such block.

    Locks ``atomic-cutover-transaction`` "Model activation SHALL NOT clone
    state", scenario "The lifecycle operation takes no cold-start approval":
    the parameter list and the success result's keys are pinned exactly.
    """
    parameters = set(inspect.signature(PsycopgModelRegistryStore.model_lifecycle_operation).parameters)
    assert parameters == {
        "self",
        "model_id",
        "operation",
        "policy_decision",
        "trusted_internal",
        "request_id",
        "previous_model_id",
        "override_missing_active",
        "reason",
    }

    store = _HarnessStore(two_models)
    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-result-shape",
    )

    assert result["status"] == "allowed"
    assert set(result) == {"status", "operation", "model", "previous_model", "preflight", "audit_reference"}


# --- blocked preflight ---------------------------------------------------


def test_preflight_blocked_activation_writes_blocked_audit_and_no_state_update(
    two_models: list[dict[str, Any]],
) -> None:
    """A preflight-blocked activation writes a ``blocked`` audit row and no swap.

    Contrive a blocker by pointing the target's ``model_package_uri``
    at an unsupported scheme so the ``OBJECT_URI_PREFIX_INVALID``
    blocker fires (``_activation_safety_evidence:2888``). The swap must
    not run; a blocked audit row is written.
    """
    # Poison the direct-grid variant's model_package_uri.
    two_models[1]["model_package_uri"] = "ftp://unsafe/package"
    store = _HarnessStore(two_models)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-blocked",
    )

    assert result["status"] == "blocked"
    # No lifecycle state updates.
    assert store._state_updates == []
    # A ``blocked`` audit row was written.
    assert [row["outcome"] for row in store.audit_rows] == ["blocked"]


# --- no scheduler file-plane publish (#3204) ------------------------------


def test_committed_activation_publishes_nothing_and_store_has_no_publisher_seam(
    two_models: list[dict[str, Any]],
) -> None:
    """A committed activation returns its result with no publisher plumbing.

    Locks ``variant-activation-cutover`` "A model lifecycle transition SHALL
    NOT publish the scheduler file plane", scenario "A committed activation
    publishes nothing": #3204 retired the manifest and state-index
    publisher seams, so neither the store class nor a store instance carries
    any post-commit registration, dispatch method or seeded publisher
    attribute, before or after a committed transition.
    """
    store = _HarnessStore(two_models)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-no-file-plane-publish",
    )

    assert result["status"] == "allowed"
    assert len(store._transactions) == 1
    assert store._transactions[0]["committed"] is True
    assert set(result) == {"status", "operation", "model", "previous_model", "preflight", "audit_reference"}
    assert [name for name in dir(PsycopgModelRegistryStore) if "post_commit" in name] == []
    assert [name for name in dir(store) if "post_commit" in name] == []


# --- lifecycle transitions that change nothing ------------------------------


def test_supersede_of_active_model_is_preflight_blocked_with_no_state_change(
    two_models: list[dict[str, Any]],
) -> None:
    """Standalone ``supersede`` of the currently-active model is preflight-blocked.

    ``_build_model_operation_preflight`` appends ``MISSING_ACTIVE_RISK`` for
    supersede on the currently-active model regardless of
    ``override_missing_active`` (the override is deactivate-only). The
    operation never commits a state change.
    """
    store = _HarnessStore(two_models)

    result = store.model_lifecycle_operation(
        "legacy_m0",  # the currently-active model
        operation="supersede",
        policy_decision=_decision("models.supersede", "legacy_m0"),
        request_id="req-supersede-active-blocked",
    )

    assert result["status"] == "blocked"
    # The MISSING_ACTIVE_RISK blocker is present.
    blocker_codes = [b["code"] for b in result["preflight"]["blockers"]]
    assert "MISSING_ACTIVE_RISK" in blocker_codes
    # No state changes committed.
    assert store._state_updates == []


def test_audit_persistence_failure_rolls_back_the_transition(
    two_models: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An audit-persistence failure rolls the transition back.

    The audit insert raises AFTER the transition ran inside the same
    transaction, so the lifecycle operation surfaces the
    ``LIFECYCLE_AUDIT_PERSISTENCE_FAILED`` blocked result and the transaction
    does not commit.
    """
    store = _HarnessStore(two_models)

    def _raise(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("test-injected audit insert failure")

    monkeypatch.setattr(store, "_insert_model_lifecycle_audit", _raise)

    result = store.model_lifecycle_operation(
        "direct_grid_m1",
        operation="activate",
        policy_decision=_decision("models.activate", "direct_grid_m1"),
        request_id="req-audit-persistence-fails",
    )

    assert result["status"] == "blocked"
    assert result["preflight"]["blockers"][-1]["code"] == "LIFECYCLE_AUDIT_PERSISTENCE_FAILED"
    # The transaction context saw the raise: the transition is not committed.
    assert store._transactions[-1]["committed"] is False
