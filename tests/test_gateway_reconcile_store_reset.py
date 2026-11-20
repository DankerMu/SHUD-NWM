"""Cached reconcile-store reset/rollback semantics.

The SQL reconcile-store build tests (bounded connect timeout, malformed
``database_url`` swallowed) were deleted with R2b (#3255): that build is the
non-DB-free arm of ``scheduler_runtime._restart_reconcile_store``, which now
fails closed with ``SchedulerModeInvariantError``
(``tests/test_scheduler_dbfree_only_mode.py``). R2c deletes the arm and the
reset/dispose logic pinned below.
"""

from __future__ import annotations

from typing import Any

# --- FINDING-1: cached reconcile session rollback on crash recovery ------------


def _reconcile_store_shell(store: Any) -> Any:
    """A minimal carrier exposing only ``_reconcile_store`` so the unbound
    ProductionScheduler method can be bound onto it without the heavy ctor.
    """

    import types

    from services.orchestrator.scheduler import ProductionScheduler

    shell = types.SimpleNamespace(_reconcile_store=store)
    ProductionScheduler._reset_reconcile_store_after_error.__get__(
        shell, ProductionScheduler
    )()
    return shell


def test_reset_reconcile_store_after_error_rolls_back_session() -> None:
    """A failed commit leaves the cached session pending-rollback; recovery rolls
    it back so the connection stays reusable, and KEEPS the cached store (the
    common, recoverable case — no needless rebuild).
    """

    import types

    rollback_calls: list[int] = []
    session = types.SimpleNamespace(rollback=lambda: rollback_calls.append(1))
    store = types.SimpleNamespace(session=session)

    shell = _reconcile_store_shell(store)

    assert rollback_calls == [1]  # rolled back exactly once.
    assert shell._reconcile_store is store  # cache preserved, not dropped.


def test_reset_reconcile_store_after_error_drops_store_when_rollback_fails() -> None:
    """If rollback itself raises (the connection is truly dead) the cache is
    dropped so the next pass rebuilds a clean store via _restart_reconcile_store.
    """

    import types

    def _boom() -> None:
        raise RuntimeError("connection dead")

    session = types.SimpleNamespace(rollback=_boom)
    store = types.SimpleNamespace(session=session)

    shell = _reconcile_store_shell(store)

    assert shell._reconcile_store is None  # poisoned/dead → dropped.


def test_reset_reconcile_store_after_error_noop_when_no_store() -> None:
    """No cached store → the reset is a clean no-op (no attribute access, no
    raise). Guards the early-return guard.
    """

    shell = _reconcile_store_shell(None)

    assert shell._reconcile_store is None
