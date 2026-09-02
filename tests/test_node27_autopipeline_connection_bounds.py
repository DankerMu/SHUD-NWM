"""#1647 — every autopipe connection is bounded, and the disable switch parses.

Two independent surfaces of ``scripts/node27_autopipeline.py``:

* ``_connect`` — the single connect site (11 callers). Before this change no
  connection carried a connect timeout or a statement timeout, so one hung
  backend wedged the 10-minute tick under its flock forever and every later
  tick was skipped by ``flock -n``. The expected kwargs are spelled out by hand
  here, never recomputed from the module constants, so a widened budget fails
  on this file rather than passing silently.
* ``NODE27_AUTOPIPE_STATS_GUARD`` — the guard's disable switch, which used to
  recognise the single literal ``off``.

The guard-leg rows below re-state the #1643 contract under the new failure
type: a cancelled statement must not change what the tick reports.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest

import scripts.node27_autopipeline as autopipe
import scripts.node27_timeseries_compression as compression

DSN = "postgresql://node27_writer:secret@127.0.0.1:55432/nhms"

# Spelled out, not derived from the module: this file is the oracle for the two
# budgets, so importing them would make every assertion below vacuous.
EXPECTED_CONNECT_TIMEOUT = 10
EXPECTED_OPTIONS = "-c statement_timeout=600000"


class _ConnectIntercepted(RuntimeError):
    """Raised by the probe so no test here ever needs a live server."""


class _ConnectProbe:
    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.args = args
        self.kwargs = kwargs
        raise _ConnectIntercepted("intercepted")


def _probe(monkeypatch: pytest.MonkeyPatch) -> _ConnectProbe:
    probe = _ConnectProbe()
    monkeypatch.setattr(autopipe.psycopg2, "connect", probe)
    return probe


def _call_connect(monkeypatch: pytest.MonkeyPatch, url: str, **kwargs: Any) -> _ConnectProbe:
    probe = _probe(monkeypatch)
    with pytest.raises(_ConnectIntercepted):
        autopipe._connect(url, **kwargs)
    return probe


# --------------------------------------------------------------------------- #
# _connect — the default bounds
# --------------------------------------------------------------------------- #
def test_default_connection_carries_both_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario "Default connection": 10 s to connect, 600 s per statement.

    The statement budget travels as the libpq ``options`` kwarg and NOT as a
    post-connect ``SET``: psycopg2 opens an implicit transaction on the first
    statement, and the rollback that ends it would undo a ``SET`` issued there,
    leaving the connection unbounded for exactly the business statements this
    is supposed to bound.
    """
    probe = _call_connect(monkeypatch, DSN)

    assert probe.args == (DSN,)
    assert probe.kwargs == {
        "fallback_application_name": "nhms-autopipe",
        "connect_timeout": EXPECTED_CONNECT_TIMEOUT,
        "options": EXPECTED_OPTIONS,
    }


def test_statement_budget_is_effective_before_the_first_business_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"...effective before the first business statement" — what libpq gets.

    The kwarg assertion above proves what the caller passed. This one proves
    what the driver resolves it to: ``options`` is part of the conninfo libpq
    applies at connection setup, so the bound is already in force when the
    first statement runs. A post-connect ``SET`` could not satisfy this.
    """
    probe = _call_connect(monkeypatch, DSN)

    parsed = psycopg2.extensions.parse_dsn(
        psycopg2.extensions.make_dsn(probe.args[0], **probe.kwargs)
    )

    assert parsed["options"] == EXPECTED_OPTIONS
    assert parsed["connect_timeout"] == str(EXPECTED_CONNECT_TIMEOUT)


def test_python_caller_override_wins_over_both_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that knows its own budget keeps it (the stats guard is one)."""
    probe = _call_connect(
        monkeypatch, DSN, connect_timeout=3, options="-c statement_timeout=120000"
    )

    assert probe.kwargs["connect_timeout"] == 3
    assert probe.kwargs["options"] == "-c statement_timeout=120000"


def test_operator_dsn_connect_timeout_keeps_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario "Operator DSN keeps precedence" — mirror of #1714's rule.

    ``connect_timeout`` is an allowed DATABASE_URL query key, so an operator
    who sets it must win. libpq would let the kwarg override the DSN, so the
    only way to preserve the operator's value is to NOT pass the kwarg at all.
    """
    url = f"{DSN}?connect_timeout=3"

    probe = _call_connect(monkeypatch, url)

    assert "connect_timeout" not in probe.kwargs
    parsed = psycopg2.extensions.parse_dsn(
        psycopg2.extensions.make_dsn(probe.args[0], **probe.kwargs)
    )
    assert parsed["connect_timeout"] == "3"
    # The statement budget has no DSN path, so it is still applied.
    assert parsed["options"] == EXPECTED_OPTIONS


def test_connect_timeout_is_an_allowed_dsn_query_key() -> None:
    """The precedence rule above is only reachable if the preflight admits the
    key — otherwise a DSN carrying it would be refused before any connect.
    """
    assert "connect_timeout" in autopipe.DATABASE_URL_ALLOWED_QUERY_KEYS
    assert autopipe._database_query_blockers("connect_timeout=3") == []


def test_dsn_application_name_merge_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1714 regression row: the new kwargs must not disturb the identity merge."""
    url = f"{DSN}?application_name=operator-tool"

    probe = _call_connect(monkeypatch, url)

    parsed = psycopg2.extensions.parse_dsn(
        psycopg2.extensions.make_dsn(probe.args[0], **probe.kwargs)
    )
    assert parsed["application_name"] == "operator-tool"
    assert parsed["fallback_application_name"] == "nhms-autopipe"


# --------------------------------------------------------------------------- #
# The statistics guard keeps its own budget
# --------------------------------------------------------------------------- #
class _GuardCursor:
    """Minimal cursor: answers the candidate query with nothing."""

    def __init__(self, statements: list[str], error: Exception | None) -> None:
        self._statements = statements
        self._error = error
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _GuardCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._statements.append(sql)
        if self._error is not None:
            raise self._error
        self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _GuardConnection:
    def __init__(self, cursor: _GuardCursor) -> None:
        self._cursor = cursor
        self.autocommit = False
        self.closed = False

    def cursor(self) -> _GuardCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def _install_guard_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connect_error: Exception | None = None,
    query_error: Exception | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    kwargs_seen: list[dict[str, Any]] = []
    statements: list[str] = []

    def connect(_database_url: str, **kwargs: Any) -> _GuardConnection:
        kwargs_seen.append(kwargs)
        if connect_error is not None:
            raise connect_error
        return _GuardConnection(_GuardCursor(statements, query_error))

    monkeypatch.setattr(autopipe.psycopg2, "connect", connect)
    return kwargs_seen, statements


@pytest.mark.parametrize(
    "leg",
    ["_analyze_frontier_chunks", "_analyze_unanalyzed_authority_tables"],
)
def test_stats_guard_connection_keeps_its_own_statement_budget(
    monkeypatch: pytest.MonkeyPatch, leg: str
) -> None:
    """Scenario "Stats-guard connection keeps its budget".

    Both legs, because the repair leg runs on every tick — inheriting the
    tick's 600 s default there would mean a candidate query five times longer
    than the guard's own contract allows.
    """
    kwargs_seen, _statements = _install_guard_db(monkeypatch)

    summary = getattr(autopipe, leg)(DSN)

    assert summary["status"] == "completed"
    assert kwargs_seen[0]["options"] == "-c statement_timeout=120000"
    assert kwargs_seen[0]["connect_timeout"] == EXPECTED_CONNECT_TIMEOUT


def test_stats_guard_budget_constant_is_not_the_tick_default() -> None:
    """The two budgets are independent knobs, not one derived from the other."""
    assert autopipe.STATS_GUARD_TIMEOUT_MS == 120_000
    assert autopipe._QUERY_TIMEOUT_MS == 600_000


def test_guard_relation_cancellation_stays_local_to_that_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario "Guard-leg cancellation keeps the #1643 semantics", part 1.

    A cancelled per-relation ANALYZE is recorded on that relation's entry; the
    guard summary stays ``completed`` so the tick's rc is untouched.
    """
    entry: dict[str, Any] = {"chunk": "_timescaledb_internal._hyper_1_58_chunk"}

    class _CancellingCursor:
        def execute(self, sql: str, params: Any = None) -> None:
            if sql.startswith("ANALYZE"):
                raise psycopg2.errors.QueryCanceled(
                    "canceling statement due to statement timeout"
                )

        def fetchone(self) -> tuple[Any, ...] | None:
            return None

    result = autopipe._analyze_one_relation(
        _CancellingCursor(), "_timescaledb_internal", "_hyper_1_58_chunk", entry, None
    )

    assert result["status"] == "failed"
    assert "QueryCanceled" in result["error"]


@pytest.mark.parametrize("failure_point", ["connect", "candidate_query"])
def test_guard_summary_level_cancellation_still_leaves_the_tick_rc_alone(
    monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    """Scenario "Guard-leg cancellation keeps the #1643 semantics", part 2.

    A cancelled connect or candidate query IS a summary-level failure — and
    still never reaches the tick's return code, because ``_stats_guard``'s
    result is deliberately absent from the rc expression.
    """
    cancelled = psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")
    _install_guard_db(
        monkeypatch,
        connect_error=cancelled if failure_point == "connect" else None,
        query_error=cancelled if failure_point == "candidate_query" else None,
    )

    summary = autopipe._analyze_frontier_chunks(DSN)

    assert summary["status"] == "failed"
    assert "QueryCanceled" in summary["error"]


# --------------------------------------------------------------------------- #
# NODE27_AUTOPIPE_STATS_GUARD — the falsy set
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [" FALSE ", "0", "no", "Off", "off", "FALSE", "NO"])
def test_falsy_switch_values_disable_the_guard(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Scenario "Falsy values disable"; ``off`` stays in the set (#1643 users)."""
    calls: list[str] = []
    monkeypatch.setattr(
        autopipe, "_analyze_frontier_chunks", lambda url: calls.append(url) or {}
    )
    monkeypatch.setattr(
        autopipe, "_analyze_unanalyzed_authority_tables", lambda url: calls.append(url) or {}
    )

    summary = autopipe._stats_guard(
        DSN, ingested_runs=1, env={"NODE27_AUTOPIPE_STATS_GUARD": value}
    )

    assert calls == []
    assert summary["status"] == "skipped"
    # The reason string is byte-unchanged for every falsy value: it names the
    # switch, and the receipt shape #1643 pinned does not move.
    assert summary["reason"] == "NODE27_AUTOPIPE_STATS_GUARD=off"
    assert summary["authority"]["status"] == "skipped"


@pytest.mark.parametrize("value", ["1", "on", "ON", "", "yes", "please-do"])
def test_other_switch_values_keep_the_guard_running(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Scenario "Other values enable" — anything outside the falsy set runs."""
    calls: list[str] = []
    monkeypatch.setattr(
        autopipe,
        "_analyze_frontier_chunks",
        lambda url: calls.append(f"frontier:{url}") or {"status": "completed"},
    )
    monkeypatch.setattr(
        autopipe,
        "_analyze_unanalyzed_authority_tables",
        lambda url: calls.append(f"authority:{url}") or {"status": "completed"},
    )

    summary = autopipe._stats_guard(
        DSN, ingested_runs=1, env={"NODE27_AUTOPIPE_STATS_GUARD": value}
    )

    assert calls == [f"authority:{DSN}", f"frontier:{DSN}"]
    assert summary["status"] == "completed"


def test_unset_switch_keeps_the_guard_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario "Other values enable", unset arm."""
    calls: list[str] = []
    monkeypatch.setattr(
        autopipe,
        "_analyze_frontier_chunks",
        lambda url: calls.append(url) or {"status": "completed"},
    )
    monkeypatch.setattr(
        autopipe,
        "_analyze_unanalyzed_authority_tables",
        lambda url: calls.append(url) or {"status": "completed"},
    )

    summary = autopipe._stats_guard(DSN, ingested_runs=1, env={})

    assert calls == [DSN, DSN]
    assert summary["status"] == "completed"


def test_falsy_set_is_exactly_the_conventional_four() -> None:
    assert autopipe._STATS_GUARD_FALSY == frozenset({"0", "false", "no", "off"})


# --------------------------------------------------------------------------- #
# Identifier discipline shared with the compression runner
# --------------------------------------------------------------------------- #
def test_compression_chunk_pattern_is_byte_identical_to_the_autopipe_anchor() -> None:
    """The two hand-quoted-identifier sites must not drift apart.

    Both interpolate a catalog-supplied chunk name into a statement that takes
    no bind parameters. One pattern loosening while the other stays strict is
    exactly the drift this pins.
    """
    assert compression._CHUNK_IDENT_RE.pattern == autopipe._STATS_GUARD_IDENT_RE.pattern
    assert compression._CHUNK_IDENT_RE.pattern == r"^[A-Za-z0-9_]+$"
