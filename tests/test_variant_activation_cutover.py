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

The tests use a subclass of :class:`PsycopgModelRegistryStore` that
overrides the DB-touching helpers with in-memory equivalents so the
real ``model_lifecycle_operation`` logic — preflight, transition, audit —
flows unchanged. This locks the seam without requiring a live TimescaleDB.

#3165 split this suite under the 1000-line guard. This base path keeps the
transaction and helper-predicate cases; the §2.2 manifest re-publish cases
live in ``tests/test_variant_activation_cutover_manifest.py`` and the §2.3/§2.4
dispatch, retirement and concurrency cases in
``tests/test_variant_activation_cutover_dispatch.py``. The shared harness, the
``two_models`` / ``variant_direct_grid_profile`` fixtures and the manifest
publisher stub are in ``tests/variant_activation_cutover_helpers.py``.
"""

from __future__ import annotations

import inspect
from typing import Any

from packages.common.model_registry import (
    PsycopgModelRegistryStore,
    _extract_source_scope,
)
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
    # Exactly ONE audit row with ``outcome='allowed'``.
    assert [row["outcome"] for row in store.audit_rows] == ["allowed"]
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


# --- helper predicates -----------------------------------------------------


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
