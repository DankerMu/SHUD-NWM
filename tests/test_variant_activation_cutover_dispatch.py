"""Unit tests for §2.3 permanent retirement and §2.4 concurrency + idempotency (Epic #961 SUB-7, #968).

Partition of ``tests/test_variant_activation_cutover.py`` (#3165). The
in-memory harness, the ``two_models`` / ``variant_direct_grid_profile``
fixtures and ``_ManifestPublisherStub`` are in
``tests/variant_activation_cutover_helpers.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import pytest

from packages.common.model_registry import (
    PRE_ACTIVATION_HOOK_MOUNT_POINTS,
    ModelActivationContext,
)
from services.orchestrator.scheduler_file_providers import FileSchedulerModelRegistry
from tests.variant_activation_cutover_helpers import (
    BASIN_ID,
    BASIN_VERSION_ID,
    _decision,
    _HarnessStore,
    _ManifestPublisherStub,
    _RecordingCursor,
    two_models,
    variant_direct_grid_profile,
)

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
