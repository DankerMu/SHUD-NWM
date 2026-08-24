"""Cached reconcile-store reset/rollback semantics and best-effort
reconcile-store build on malformed database URLs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

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


def test_restart_reconcile_store_bounds_db_connect_timeout(monkeypatch: Any) -> None:
    """_restart_reconcile_store must build its engine with a bounded
    connect_timeout so a misconfigured/unreachable database_url fails fast
    instead of hanging the daemon at pass start. Patches sqlalchemy.create_engine
    at the source (the method does a local ``from sqlalchemy import create_engine``)
    and asserts the connect_args carry the bound."""
    from sqlalchemy import create_engine as _real_create_engine

    from services.orchestrator.scheduler import (
        RECONCILE_DB_CONNECT_TIMEOUT_SECONDS,
        RECONCILE_DB_STATEMENT_TIMEOUT_MS,
        ProductionScheduler,
    )

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake_create_engine(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        # Return a real, side-effect-free engine so PipelineStore(Session(engine))
        # constructs without touching the (fake) postgres URL.
        return _real_create_engine("sqlite://")

    monkeypatch.setattr("sqlalchemy.create_engine", _fake_create_engine)

    class _Config:
        database_url = "postgresql://u:p@db.invalid:5432/x"

    class _Shell:
        config = _Config()
        _reconcile_store = None

    shell = _Shell()
    ProductionScheduler._restart_reconcile_store.__get__(shell, ProductionScheduler)()

    assert len(calls) == 1
    _args, kwargs = calls[0]
    assert "connect_args" in kwargs
    connect_timeout = kwargs["connect_args"]["connect_timeout"]
    assert connect_timeout == RECONCILE_DB_CONNECT_TIMEOUT_SECONDS
    assert isinstance(connect_timeout, int) and connect_timeout > 0
    # Post-connect slow-query bound: a reachable-but-slow DB must not stall the
    # pass at reconcile time.
    options = kwargs["connect_args"]["options"]
    assert f"statement_timeout={RECONCILE_DB_STATEMENT_TIMEOUT_MS}" in options
    assert "statement_timeout=10000" in options


# --- FINDING-2: reconcile store build is best-effort to ANY database_url ------
# A malformed/unbuildable database_url makes SQLAlchemy's make_url() raise
# synchronously inside create_engine. That exception must NEVER propagate out of
# _restart_reconcile_store / _run_restart_reconcile (which run at pass start,
# before the submit-path DB-host preflight). It is swallowed as a best-effort
# skip; the preflight still runs. Zero-leak: no raw error message (DSN incl.
# password) may surface — only the exception class name.


def _malformed_url_shell(database_url: str) -> Any:
    """A minimal carrier exposing the attributes _restart_reconcile_store and
    _run_restart_reconcile touch, so the unbound methods can be bound without the
    heavy ctor. Mirrors the duck-typed shells used elsewhere in this file.
    """

    import types

    config = types.SimpleNamespace(
        database_url=database_url,
        dry_run=False,
        restart_reconcile_enabled=True,
    )
    return types.SimpleNamespace(
        config=config,
        _reconcile_store=None,
        _reconcile_store_build_error=None,
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://nhms:secret@bad::host/nhms",
        "postgresql://nhms:secret@[::1/nhms",
    ],
)
def test_restart_reconcile_store_swallows_malformed_database_url(
    database_url: str,
) -> None:
    """A malformed database_url must make _restart_reconcile_store return None
    (best-effort skip) WITHOUT raising, and must not stash the raw error message
    (which embeds the password) — only the exception class name."""
    from services.orchestrator.scheduler import ProductionScheduler

    shell = _malformed_url_shell(database_url)
    store = ProductionScheduler._restart_reconcile_store.__get__(
        shell, ProductionScheduler
    )()

    assert store is None
    assert shell._reconcile_store is None
    # Class name only — provably secret-free.
    assert shell._reconcile_store_build_error is not None
    assert "secret" not in shell._reconcile_store_build_error


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://nhms:secret@bad::host/nhms",
        "postgresql://nhms:secret@[::1/nhms",
    ],
)
def test_run_restart_reconcile_skips_on_malformed_database_url(
    database_url: str,
) -> None:
    """_run_restart_reconcile must not propagate a malformed-url build failure:
    it returns a best-effort skip dict the pass tolerates, and that dict carries
    zero credentials (zero-leak by construction — error_type is a class name)."""

    from services.orchestrator.scheduler import ProductionScheduler

    shell = _malformed_url_shell(database_url)
    # _run_restart_reconcile calls self._restart_reconcile_store() internally, so
    # bind that helper onto the shell too.
    shell._restart_reconcile_store = ProductionScheduler._restart_reconcile_store.__get__(
        shell, ProductionScheduler
    )
    result = ProductionScheduler._run_restart_reconcile.__get__(
        shell, ProductionScheduler
    )()

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == "reconcile_store_build_failed"
    assert "error_type" in result
    assert "secret" not in json.dumps(result)
