"""Durable two-phase reservation idempotency: unique-key races, the 8-party
barrier harness, and its source-mutation guards (#1645).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import (
    create_engine,
    event,
)
from sqlalchemy.orm import Session

import tests.gateway_reconcile_helpers as gateway_reconcile_helpers
from services.orchestrator.persistence import (
    Base,
    PipelineJob,
    PipelineStore,
)
from services.orchestrator.reconcile import SacctRecord
from tests.gateway_reconcile_helpers import (
    _make_idempotency_attempt_worker,
    _store_repo,
    _StoreRepo,
)
from tests.gateway_reconcile_writer_helpers import _start_attempt_threads

# --- M24 §3A: durable two-phase reservation + crash-window reconcile ---------


def test_idempotency_key_unique_constraint() -> None:
    """Reserving the same idempotency_key twice does NOT create a second row."""

    from services.orchestrator.reservation import reserve_candidate

    store = _store_repo()
    common = dict(
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    first = reserve_candidate(store, idempotency_key="gfs:cyc:basin:forcing", job_id="job_a", **common)
    second = reserve_candidate(store, idempotency_key="gfs:cyc:basin:forcing", job_id="job_b", **common)

    assert first.created is True
    assert second.created is False  # reused, not a new row.
    assert second.job_id == "job_a"
    # Exactly one durable row carries that key.
    rows = [j for j in store.store.session.query(PipelineJob).all() if j.idempotency_key == "gfs:cyc:basin:forcing"]
    assert len(rows) == 1


def test_idempotency_key_unique_constraint_concurrent(tmp_path: Any) -> None:
    """Concurrent reserve of the SAME key (each thread its own session against a
    shared SQLite file + unique index): exactly one wins (created=True), exactly
    one durable row exists.

    Counterfactual: if reserve_pipeline_job returned the existing row instead of
    None on conflict (losing the DB RETURNING win/lose signal), >1 pass would
    report created=True and the ``exactly one created`` assertion goes red.
    """

    import threading

    from services.orchestrator.reservation import reserve_candidate

    # File-backed engine so each thread holds an independent connection/session
    # contending on the SAME physical unique idempotency_key index.
    db_path = tmp_path / "reserve_race.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute(f"ATTACH DATABASE '{db_path}' AS ops")

    Base.metadata.create_all(engine)

    key = "gfs:cyc:basin:forcing"
    common = dict(
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    n = 8
    # Generous hang backstop, NOT a performance SLA (design D2): its only job is
    # to turn a pre-arrival worker exception into BrokenBarrierError so peers
    # release instead of stranding non-daemon threads at interpreter shutdown.
    # The parent join is STRICTLY LARGER (65 > 60) so a parent wait can never
    # expire before the Barrier bound, which would otherwise leave a peer
    # momentarily alive or record a parent TimeoutError before the peer's own
    # BrokenBarrierError surfaces.
    barrier = threading.Barrier(n, timeout=60)
    results: list[Any] = [None] * n
    errors: list[tuple[int, BaseException]] = []

    # The ONE shipping worker body (Phase 2 gaps 3/4): injection tests reuse it.
    _attempt = _make_idempotency_attempt_worker(
        engine=engine,
        barrier=barrier,
        results=results,
        errors=errors,
        key=key,
        common=common,
        reserve=reserve_candidate,
    )

    # Transactional launch (task 5.2): only successfully started threads are
    # tracked; if a later start() raises, the Barrier is aborted so waiting
    # peers release, every started peer is joined against one absolute cleanup
    # deadline, and the ORIGINAL launch cause is re-raised rather than masked
    # by a peer BrokenBarrierError.
    started = _start_attempt_threads(_attempt, n, barrier, join_timeout=65)

    # Attribute worker failure/broken barrier BEFORE inspecting race results:
    # a worker that died before reserving leaves `results` missing and the
    # symptom would otherwise mask the cause (design D3).
    assert not errors, errors
    assert all(not thread.is_alive() for thread in started)

    created = [r for r in results if r is not None and r.created]
    assert len(created) == 1, f"exactly one creator expected, got {len(created)}"
    # And every loser observes the same winning row id.
    winner_job_id = created[0].job_id
    losers = [r for r in results if r is not None and not r.created]
    assert len(losers) == n - 1
    assert all(r.job_id == winner_job_id for r in losers)
    # Exactly one durable row carries that key (unique constraint held).
    verify = PipelineStore(Session(engine))
    try:
        rows = [
            j
            for j in verify.session.query(PipelineJob).all()
            if j.idempotency_key == key
        ]
        assert len(rows) == 1
    finally:
        verify.session.close()


def _test_function_source(function_name: str, *, owning_file: Path | None = None) -> str:
    """Read ``function_name``'s exact source from this file (or ``owning_file``) via AST.

    ``inspect.getsource`` is unreliable under pytest's assertion rewriting when
    the module is fully loaded (it can return a shifted/other block), so the
    anti-vacuity mutants are derived from the AST segment of the exact live
    test function instead of mutating the tracked working tree.

    Only module-level definitions bind (task 5.5): ``ast.Module.body`` is
    examined, exactly one top-level ``FunctionDef`` must match, and nested
    same-name definitions are never selected. Zero or duplicate top-level
    owners fail deterministically instead of first-match binding. The
    uniqueness logic lives in ``_module_level_function_segment`` so the
    synthetic decoy/zero/duplicate cases execute the exact code the shipping
    extractor uses (Phase 2 gap 2).
    """

    text = (owning_file or Path(__file__)).read_text()
    return _module_level_function_segment(text, function_name)


def _module_level_function_segment(text: str, function_name: str) -> str:
    """Return the exact source of the single module-level ``function_name`` in ``text``.

    The ONE uniqueness implementation (task 5.5): only ``ast.Module.body`` is
    examined, exactly one module-level ``FunctionDef`` must match, and nested
    same-name definitions are ignored. ``_test_function_source`` delegates here
    so the synthetic nested/zero/duplicate cases exercise the shipping logic
    (Phase 2 gap 2).
    """

    import ast

    tree = ast.parse(text)
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one module-level {function_name!r}, got {len(matches)}"
        )
    segment = ast.get_source_segment(text, matches[0])
    assert segment is not None, function_name
    return segment


def test_ast_guards_require_exactly_one_module_level_function_definition() -> None:
    """AST source extraction must bind one top-level owner, not a nested decoy (#1645, task 5.5).

    A nested SAME-NAME function is the exact decoy shape: it must never be
    selected, and the shipped original must still resolve to the single
    module-level owner. A nested-only definition yields zero module-level
    owners and must fail deterministically, as must duplicate top-level
    owners.
    """

    # (a) Exactly one top-level owner containing a same-name nested decoy:
    # the extractor must select the OUTER definition, not the inner one.
    same_name_nested = (
        "def target():\n"
        "    def target():\n"
        "        return 'inner'\n"
        "    return 'outer'\n"
    )
    segment = _module_level_function_segment(same_name_nested, "target")
    assert segment == (
        "def target():\n"
        "    def target():\n"
        "        return 'inner'\n"
        "    return 'outer'"
    )
    assert segment.count("def target()") == 2  # outer + the nested decoy
    assert "return 'outer'" in segment

    # (b) Nested-only definition -> zero module-level owners, deterministic
    # failure (the nested target must not be selected).
    nested_only = (
        "def outer():\n"
        "    def target():\n"
        "        return 'nested'\n"
        "    return target()\n"
    )
    try:
        _module_level_function_segment(nested_only, "target")
    except AssertionError:
        pass
    else:
        raise AssertionError("nested-only target must fail deterministically (zero module-level owners)")

    # Zero module-level owners -> deterministic failure.
    zero_owner = "def other():\n    return 1\n"
    try:
        _module_level_function_segment(zero_owner, "target")
    except AssertionError:
        pass
    else:
        raise AssertionError("zero top-level owners must fail deterministically")

    # Duplicate module-level owners -> deterministic failure, never first-match.
    duplicate = (
        "def target():\n    return 'first'\n"
        "def target():\n    return 'second'\n"
    )
    try:
        _module_level_function_segment(duplicate, "target")
    except AssertionError:
        pass
    else:
        raise AssertionError("duplicate top-level owners must fail deterministically")

    # The shipped extractor is exercised on the real module so the real guard
    # target still resolves exactly once.
    assert _test_function_source("test_idempotency_key_unique_constraint_concurrent")


def test_idempotency_barrier_source_legs_each_fail_bounded_and_distinct() -> None:
    """Source-mutation guards: each repair leg is a distinct string-removal target (#1645).

    These are STRING-COMPARISON guards, not executed semantic mutants: each
    asserts that removing one shipped repair line actually changes the derived
    source, so the removal target is real and not dead text. The behavioral red
    observables they protect are exercised by the failure-injection tests and
    the bounded subprocess mutant proof, not here.
    """

    source = _test_function_source("test_idempotency_key_unique_constraint_concurrent")

    # Leg 1: remove the barrier bound -> a pre-arrival worker exception strands peers.
    unbounded = source.replace("threading.Barrier(n, timeout=60)", "threading.Barrier(n)")
    assert source != unbounded, "unbounded mutant must differ from source"
    assert "threading.Barrier(n)" in unbounded and "threading.Barrier(n, timeout=60)" not in unbounded

    # Leg 2: remove the liveness assertion -> a live peer goes undetected.
    no_liveness = source.replace(
        "assert all(not thread.is_alive() for thread in started)",
        "del thread  # liveness assertion removed",
    )
    assert source != no_liveness, "liveness-removed mutant must differ from source"
    assert "not thread.is_alive()" not in no_liveness

    # Leg 3: remove the error propagation -> a worker failure goes unreported.
    no_errors = source.replace("errors: list[tuple[int, BaseException]] = []", "errors = None")
    assert source != no_errors, "error-removed mutant must differ from source"
    assert "errors: list[tuple[int, BaseException]]" not in no_errors

    # The worker-body legs (4, 5, 5b) target the SHIPPING worker factory, whose
    # source the original and the injection tests both consume (Phase 2 gaps
    # 3/4).
    worker = _test_function_source(
        "_make_idempotency_attempt_worker",
        owning_file=Path(gateway_reconcile_helpers.__file__),
    )

    # Leg 4: move session creation back OUTSIDE the catch-all scope, so a
    # constructor/session failure would escape attribution. This is the exact
    # issue trigger and must be load-bearing, not a raised-after-success shape.
    outside_construction = worker.replace(
        "        try:\n"
        "            if pre_body is not None:\n"
        "                # A pre-arrival injected failure is captured by the SAME\n"
        "                # catch-all as the shipped worker's own failures.\n"
        "                pre_body(index)\n"
        "            session = Session(engine)\n",
        "        session = Session(engine)\n"
        "        try:\n"
        "            if pre_body is not None:\n"
        "                pre_body(index)\n",
    )
    assert worker != outside_construction, "outside-construction mutant must differ from source"
    assert "session = Session(engine)" in outside_construction
    assert (
        "        try:\n"
        "            if pre_body is not None:\n"
        "                # A pre-arrival injected failure is captured by the SAME\n"
        "                # catch-all as the shipped worker's own failures.\n"
        "                pre_body(index)\n"
        "            session = Session(engine)"
    ) in worker

    # Leg 5: collapse the session identity back into the wrapper close, so a
    # Session created successfully but followed by a PipelineStore/_StoreRepo
    # construction failure is NOT closed (resource leak on the failure path).
    partial_cleanup = worker.replace(
        (
            "        finally:\n"
            "            if session is not None:\n"
            "                try:\n"
            "                    session.close()\n"
            "                except BaseException as cleanup_error:"
        ),
        "        finally:\n            if repo is not None:\n                repo.store.session.close()",
    )
    assert worker != partial_cleanup, "partial-cleanup mutant must differ from source"
    assert (
        "        finally:\n"
        "            if session is not None:\n"
        "                try:\n"
        "                    session.close()"
    ) in worker

    # Leg 5b: remove the cleanup error capture -> a close failure would escape
    # only as a PytestUnhandledThreadExceptionWarning (task 5.3).
    no_cleanup_capture = worker.replace(
        "                except BaseException as cleanup_error:\n"
        "                    # A close failure after a body error must stay indexed and\n"
        "                    # ordered AFTER the body error, never escape only as a\n"
        "                    # PytestUnhandledThreadExceptionWarning (task 5.3).\n"
        "                    errors.append((index, cleanup_error))",
        "                except BaseException as cleanup_error:\n"
        "                    raise cleanup_error",
    )
    assert worker != no_cleanup_capture, "cleanup-capture-removed mutant must differ from source"
    assert "errors.append((index, cleanup_error))" in worker

    # Leg 6: the launch must go through the transactional helper with an
    # absolute-deadline join (task 5.2) -- an inline per-peer join loop would
    # multiply the parent wait by the peer count.
    assert (
        "    started = _start_attempt_threads(_attempt, n, barrier, join_timeout=65)" in source
    )
    inline_join = source.replace(
        "    started = _start_attempt_threads(_attempt, n, barrier, join_timeout=65)",
        (
            "    started = []\n"
            "    for i in range(n):\n"
            "        t = threading.Thread(target=_attempt, args=(i,))\n"
            "        t.start()\n"
            "        started.append(t)\n"
            "    for t in started:\n"
            "        t.join(timeout=65)"
        ),
    )
    assert source != inline_join, "inline-join mutant must differ from source"
    assert "_start_attempt_threads(_attempt, n, barrier, join_timeout=65)" in source

    # Exact-site ordering pins: the worker error handler lives in the SHIPPING
    # factory, and the error/liveness assertions must precede the substantive
    # results in the original test.
    assert "        except BaseException as error:\n            errors.append((index, error))" in worker
    assert "    assert not errors, errors" in source

    no_handler = worker.replace(
        "        except BaseException as error:\n            errors.append((index, error))",
        "        # handler removed",
    )
    assert worker != no_handler, "handler-removed mutant must differ from source"

    # The error and liveness assertions must precede the `created = ...`
    # substantive-result block. The direct index assertions catch an actual
    # move; the mutation below only demonstrates the removal target is real.
    assert source.index("assert not errors, errors") < source.index("created = [r for r in results")
    assert source.index("assert all(not thread.is_alive() for thread in started)") < source.index(
        "created = [r for r in results"
    )
    assertions_removed_before_results = source.replace(
        "    assert not errors, errors\n    assert all(not thread.is_alive() for thread in started)\n\n    created = [",
        "    created = [",
    )
    assert source != assertions_removed_before_results, "assertion-removal mutant must differ from source"

    # The legs are pairwise distinct source mutations across both sources.
    assert len(
        {
            source,
            worker,
            unbounded,
            no_liveness,
            no_errors,
            outside_construction,
            partial_cleanup,
            no_cleanup_capture,
            inline_join,
            no_handler,
            assertions_removed_before_results,
        }
    ) == 11


def test_array_stage_kill_before_bind_reconciles_by_comment() -> None:
    """Array-stage crash after sbatch (array master accepted, comment recorded)
    but before bind: reconcile recovers the array master slurm_job_id by the
    idempotency comment and binds it — no array resubmission.

    Counterfactual: if the array submit path did NOT thread ``--comment`` (item 2
    BLOCKER), accounting could not be matched back by idempotency_key, the guard
    would mark the reservation reservation_lost, and the ``action == 'bound'``
    assertion goes red.
    """

    from services.orchestrator.reconcile import (
        SacctRecord,
        reconcile_reserved_unbound_jobs,
    )
    from services.orchestrator.reservation import reserve_candidate, slurm_comment_for

    store = _store_repo()
    key = "gfs:cyc:basin:run_shud_forecast_array"
    reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_array_crash",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="run_shud_forecast_array",
        model_id="model_1",
        stage="run_shud_forecast_array",
    )
    assert store.query_candidate_state(key)["slurm_job_id"] is None

    # The array master sbatch accepted (it recorded our comment). Array job ids
    # take the ``<master>`` form in sacct for the master record.
    def _comment_query(idem: str) -> SacctRecord | None:
        if idem == key:
            return SacctRecord(
                slurm_job_id="77042",
                raw_state="RUNNING",
                job_name="nhms_run_shud_forecast_array",
                comment=slurm_comment_for(key),
            )
        return None

    outcomes = reconcile_reserved_unbound_jobs(store.store, comment_query=_comment_query)

    assert len(outcomes) == 1
    assert outcomes[0].action == "bound"
    assert outcomes[0].slurm_job_id == "77042"
    bound = store.query_candidate_state(key)
    assert bound["slurm_job_id"] == "77042"
    assert bound["status"] == "submitted"


class InjectedReservationFailure(RuntimeError):
    """A pre-arrival worker failure with an identity of its own (#1645)."""


def test_idempotency_barrier_harness_fails_bounded_when_construction_raises_before_arrival(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-arrival failure must break peers bounded and leave no live thread (#1645).

    The explicit-thread idempotency harness family runs non-daemon workers that
    release together at the Barrier. This test injects the EXACT issue trigger:
    a repository/session CONSTRUCTOR failure BEFORE the barrier. The failure
    fires at the shipping worker's actual ``Session(engine)`` expression: the
    module-global ``Session`` callable is patched so worker index 0 raises
    ``InjectedReservationFailure`` while the other 7 construct real Sessions
    and reach the Barrier. ``pre_body`` only records the worker identity; it
    does not raise. Pre-fix, index 0's absence left the other 7 waiting inside
    an unbounded ``Barrier.wait()``: the parent's bare join returns, its
    assertions can even pass, and the whole pytest process then hangs at
    ``threading._shutdown()``. The barrier bound converts the failing worker's
    absence into ``BrokenBarrierError``, and the construction-inside-catch-all
    scope attributes the constructor exception before any result assertion.
    """

    import threading

    from services.orchestrator.reservation import reserve_candidate

    db_path = tmp_path / "reserve_race.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute(f"ATTACH DATABASE '{db_path}' AS ops")

    Base.metadata.create_all(engine)

    key = "gfs:cyc:basin:forcing"
    common = dict(
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    n = 8
    # Explicitly short with margin: the bound is what is under test, so waiting
    # out the 60s production backstop would just slow the suite (design D2).
    barrier = threading.Barrier(n, timeout=2)
    results: list[Any] = [None] * n
    errors: list[tuple[int, BaseException]] = []
    thread_local = threading.local()
    session_invoked: list[int] = []

    def pre_body(index: int) -> None:
        # Selector/marker only (Phase 6.2 item 2): record the worker identity
        # so the patched Session constructor knows which index is the failing
        # one. It does NOT raise -- the failure is the Session constructor's.
        thread_local.harness_index = index

    real_session = Session

    def failing_session(*args: Any, **kwargs: Any) -> Session:
        # The SHIPPING worker executes `Session(engine)` here. Index 0 fails
        # with the injected identity at the real constructor expression; the
        # other indices construct real Sessions and reach the Barrier.
        index = getattr(thread_local, "harness_index", None)
        if index is not None:
            session_invoked.append(index)
        if index == 0:
            raise InjectedReservationFailure("injected constructor failure")
        return real_session(*args, **kwargs)

    monkeypatch.setattr("tests.gateway_reconcile_helpers.Session", failing_session)

    # The SHIPPING worker body -- the SAME factory the original uses. The
    # injection copy uses the same transactional launch seam as the shipped
    # harness (task 5.2), so the pre-arrival path and the peer set it joins are
    # exactly the ones the original exercises.
    _attempt = _make_idempotency_attempt_worker(
        engine=engine,
        barrier=barrier,
        results=results,
        errors=errors,
        key=key,
        common=common,
        reserve=reserve_candidate,
        pre_body=pre_body,
    )
    try:
        started = _start_attempt_threads(_attempt, n, barrier, join_timeout=5)
    finally:
        monkeypatch.undo()

    # The patched Session constructor must have been invoked EXACTLY ONCE for
    # every worker, in any scheduling order -- callback append order across
    # started threads is not a contract. The sorted equality already proves
    # multiplicity given equal lengths, so the explicit length check keeps the
    # failure message clear.
    assert len(session_invoked) == n, session_invoked
    assert sorted(session_invoked) == list(range(n)), session_invoked

    # The injected constructor exception and the peer broken-barrier outcome
    # must be reported before any missing-result/state assertion (design D3).
    assert len(errors) == n
    for index, error in errors:
        if index == 0:
            assert isinstance(error, InjectedReservationFailure)
        else:
            assert isinstance(error, threading.BrokenBarrierError), (
                f"peer {index} should observe BrokenBarrierError, got {type(error).__name__}"
            )
    assert all(not thread.is_alive() for thread in started)
    assert results == [None] * n


class InjectedGatewayThreadStartFailure(RuntimeError):
    """A deterministic second ``Thread.start()`` failure with an identity of its own (#1645)."""


def test_gateway_explicit_harness_partial_thread_start_aborts_joins_and_preserves_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ``Thread.start()`` failure must abort, clean up, and re-raise (#1645, task 5.2).

    The 8-party gateway harness starts its explicit peers in a loop. If the
    second ``start()`` raises while the first peer is running, that peer would
    otherwise wait forever inside ``Barrier.wait()`` (the exact strand). The
    transactional launch must abort the Barrier, join every successfully
    started peer against one absolute cleanup deadline, and re-raise the
    ORIGINAL launch cause -- never a peer ``BrokenBarrierError`` or an
    ``abort()`` side effect.

    The regression proof observes the SHIPPING helper's own ``Thread.join()``
    calls on the exactly-tracked successfully started peer (round-2 verifier
    evidence-r2-01): if only the exception-path join is removed, no helper join
    is observed and the joined-before-reraise assertion deterministically reds.
    Worker side effects alone (``peer_errors``, ``first_started``) cannot see a
    missing parent-side join.
    """

    barrier = threading.Barrier(8, timeout=2)
    barrier_aborted = threading.Event()
    first_started = threading.Event()

    real_start = threading.Thread.start
    # Exact Thread objects whose real start() succeeded -- NOT inferred from
    # worker side effects.
    successfully_started: list[threading.Thread] = []
    start_attempts = 0

    def failing_start(thread: threading.Thread) -> None:
        nonlocal start_attempts
        start_attempts += 1
        if start_attempts == 2:
            raise InjectedGatewayThreadStartFailure("injected second start failure")
        real_start(thread)
        successfully_started.append(thread)
        if start_attempts == 1:
            first_started.set()

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    real_abort = threading.Barrier.abort

    def tracked_abort(barrier: threading.Barrier) -> None:
        barrier_aborted.set()
        real_abort(barrier)

    monkeypatch.setattr(threading.Barrier, "abort", tracked_abort)

    # Observe the REAL join() calls the shipping helper makes on the tracked
    # peers. `launch_observable` flips only after pytest.raises returns, so a
    # recorded join proves the helper joined that peer BEFORE the launch
    # exception became observable to the parent.
    real_join = threading.Thread.join
    launch_observable = False
    helper_joins: list[tuple[threading.Thread, bool, bool]] = []

    def observing_join(thread: threading.Thread, *args: Any, **kwargs: Any) -> None:
        real_join(thread, *args, **kwargs)
        if thread in successfully_started:
            helper_joins.append((thread, thread.is_alive(), launch_observable))

    monkeypatch.setattr(threading.Thread, "join", observing_join)

    peer_errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            first_started.wait(timeout=5)
            barrier.wait()
        except BaseException as error:
            # The peer leaves the aborted barrier as BrokenBarrierError; the
            # real harness records it, so this test records it too instead of
            # letting it escape as an unhandled thread exception.
            peer_errors.append(error)

    started = time.monotonic()
    try:
        with pytest.raises(InjectedGatewayThreadStartFailure, match="injected second start failure"):
            _start_attempt_threads(worker, 8, barrier, join_timeout=5)
    finally:
        # Fallback cleanup via the saved REAL join, bypassing the observation
        # wrapper: even a mutated helper that never joins cannot leave a live
        # peer. It records nothing, so it cannot make the proof below pass.
        for thread in successfully_started:
            real_join(thread, timeout=5)
    launch_observable = True
    elapsed = time.monotonic() - started

    assert barrier_aborted.is_set(), "partial launch must abort the Barrier"
    assert elapsed < 5, f"cleanup must respect one absolute deadline, took {elapsed:.2f}s"
    assert first_started.is_set(), "the first peer must have started"
    # Exact successfully started peer population (this schedule: one peer).
    assert len(successfully_started) == 1, successfully_started
    # The shipping helper joined every successfully started peer exactly once,
    # BEFORE the launch exception became observable to the parent, and each
    # observed join returned with that peer no longer alive.
    assert len(helper_joins) == len(successfully_started), helper_joins
    assert {id(thread) for thread, _, _ in helper_joins} == {
        id(thread) for thread in successfully_started
    }
    assert all(not alive for _, alive, _ in helper_joins), helper_joins
    assert all(not observable for _, _, observable in helper_joins), helper_joins
    assert peer_errors and all(
        isinstance(error, threading.BrokenBarrierError) for error in peer_errors
    ), f"started peer must observe BrokenBarrierError, got {peer_errors}"


def test_gateway_session_created_wrapper_failed_close_exactly_once(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw Session created + wrapper construction fails -> close exactly once (#1645, task 5.4).

    The harness tracks Session identity separately from the wrappers, so a
    successfully created raw ``Session`` must be closed exactly once even when
    ``PipelineStore``/``_StoreRepo`` construction fails afterwards. This proof
    executes the SHIPPING worker body (``_make_idempotency_attempt_worker``)
    and fails index 1 at the ACTUAL ``_StoreRepo(PipelineStore(session))``
    expression: the module-global ``_StoreRepo`` constructor is patched so only
    index 1 raises ``WrapperConstructionFailure``, while indices 0 and 2..7
    construct real wrappers, reach the Barrier, and break with
    ``BrokenBarrierError`` -- the peer population really reaches the barrier,
    so the close count and the peer break are both non-vacuous. ``post_session``
    only tags the raw Session as a selector; it does not raise. The injected
    wrapper failure identity must be preserved, peers break bounded, and no
    started thread remains alive.
    """

    import threading

    from services.orchestrator.reservation import reserve_candidate

    class WrapperConstructionFailure(RuntimeError):
        pass

    db_path = tmp_path / "reserve_race.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute(f"ATTACH DATABASE '{db_path}' AS ops")

    Base.metadata.create_all(engine)

    key = "gfs:cyc:basin:forcing"
    common = dict(
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    n = 8
    barrier = threading.Barrier(n, timeout=2)
    results: list[Any] = [None] * n
    errors: list[tuple[int, BaseException]] = []
    close_counts: dict[int, int] = {}
    thread_local = threading.local()
    wrapper_invoked: list[int] = []

    def pre_body(index: int) -> None:
        # Selector/marker only (Phase 6.2 item 3): the patched _StoreRepo
        # constructor uses this to pick index 1 as the failing worker.
        thread_local.harness_index = index

    def post_session(index: int, session: Session) -> None:
        # Tag ONLY index 1's raw Session so the injected close is attributable
        # and the close count stays exactly {1: 1}; peers 0 and 2..7 create and
        # close their own raw Sessions on the BrokenBarrierError path but are
        # deliberately not counted. No raise here.
        if index == 1:
            session.info["_harness_index"] = index

    real_store_repo = _StoreRepo

    def failing_store_repo(store: PipelineStore) -> _StoreRepo:
        # The SHIPPING worker executes `_StoreRepo(PipelineStore(session))`
        # here. Index 1 fails with the injected wrapper identity; the other
        # indices construct real wrappers and reach the Barrier.
        index = getattr(thread_local, "harness_index", None)
        if index is not None:
            wrapper_invoked.append(index)
        if index == 1:
            raise WrapperConstructionFailure("injected wrapper construction failure")
        return real_store_repo(store)

    monkeypatch.setattr("tests.gateway_reconcile_helpers._StoreRepo", failing_store_repo)

    # The shipping worker body -- the SAME factory the original uses. No
    # pre_body failure: every peer reaches Session creation, and only index 1
    # fails at the real wrapper-construction expression.
    _attempt = _make_idempotency_attempt_worker(
        engine=engine,
        barrier=barrier,
        results=results,
        errors=errors,
        key=key,
        common=common,
        reserve=reserve_candidate,
        pre_body=pre_body,
        post_session=post_session,
    )
    real_close = Session.close

    def counting_close(self: Session) -> None:
        idx = self.info.get("_harness_index")
        if idx is not None:
            close_counts[idx] = close_counts.get(idx, 0) + 1
        real_close(self)

    monkeypatch.setattr(Session, "close", counting_close)
    try:
        started = _start_attempt_threads(_attempt, n, barrier, join_timeout=5)
    finally:
        monkeypatch.undo()

    assert all(not thread.is_alive() for thread in started)
    # The patched wrapper constructor ran EXACTLY ONCE for every worker (index
    # 1 raised), in any scheduling order -- callback append order across
    # started threads is not a contract. Sorted equality plus matching length
    # proves multiplicity without depending on append order.
    assert len(wrapper_invoked) == n, wrapper_invoked
    assert sorted(wrapper_invoked) == list(range(n)), wrapper_invoked
    # Only index 1's raw Session is tagged (only index 1 reached wrapper
    # construction after Session creation), and it was closed exactly once.
    assert close_counts == {1: 1}, f"expected exactly one close at index 1, got {close_counts}"
    by_index = {index: error for index, error in errors}
    assert isinstance(by_index[1], WrapperConstructionFailure), type(by_index[1]).__name__
    for index in range(n):
        if index == 1:
            continue
        assert isinstance(by_index[index], threading.BrokenBarrierError), (
            f"peer {index} should observe BrokenBarrierError, "
            f"got {type(by_index[index]).__name__}"
        )
    assert results == [None] * n


def test_gateway_worker_cleanup_failure_is_indexed_after_the_body_error(
    tmp_path: Any,
) -> None:
    """A worker ``Session.close()`` failure must be parent-visible, after the body error (#1645, task 5.3).

    ``session.close()`` runs in the shipping worker's ``finally``. If it
    raises, the exception would otherwise escape the worker and be reported
    only as ``PytestUnhandledThreadExceptionWarning`` -- the parent sees no
    indexed record. This proof executes the SHIPPING worker body
    (``_make_idempotency_attempt_worker``) so the cleanup capture itself is
    what is under test, keeping an earlier body error FIRST in stable
    body-before-cleanup order. Worker 1 carries BOTH a body failure and a
    cleanup failure, so its two indexed errors must appear in stable order.
    """

    import threading

    from services.orchestrator.reservation import reserve_candidate

    class SessionCloseFailure(RuntimeError):
        pass

    db_path = tmp_path / "reserve_race.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute(f"ATTACH DATABASE '{db_path}' AS ops")

    Base.metadata.create_all(engine)

    key = "gfs:cyc:basin:forcing"
    common = dict(
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    n = 8
    barrier = threading.Barrier(n, timeout=2)
    results: list[Any] = [None] * n
    errors: list[tuple[int, BaseException]] = []

    def pre_body(index: int) -> None:
        if index == 0:
            raise InjectedReservationFailure("injected body failure")

    def post_session(index: int, session: Session) -> None:
        # Tag the session so the injected close failure is attributed to
        # exactly this peer's cleanup, independent of scheduling.
        session.info["_harness_index"] = index
        if index == 1:
            # Body failure AFTER the raw Session exists: the double-failure
            # seam under test (body + cleanup both fail for this worker).
            raise InjectedReservationFailure("injected body failure")

    # The shipping worker body -- the SAME factory the original uses.
    _attempt = _make_idempotency_attempt_worker(
        engine=engine,
        barrier=barrier,
        results=results,
        errors=errors,
        key=key,
        common=common,
        reserve=reserve_candidate,
        pre_body=pre_body,
        post_session=post_session,
    )

    close_attempts = 0
    real_close = Session.close

    def failing_close(self: Session) -> None:
        nonlocal close_attempts
        close_attempts += 1
        if self.info.get("_harness_index") == 1:
            raise SessionCloseFailure("injected session close failure")
        real_close(self)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Session, "close", failing_close)
    try:
        started = _start_attempt_threads(_attempt, n, barrier, join_timeout=5)
    finally:
        monkeypatch.undo()

    assert all(not thread.is_alive() for thread in started)
    assert close_attempts == n - 1, f"expected {n - 1} closes, got {close_attempts}"
    # Worker 1: body error FIRST, cleanup error SECOND -- stable
    # body-before-cleanup order in the parent-visible channel.
    index_one_errors = [error for i, error in errors if i == 1]
    assert len(index_one_errors) == 2, index_one_errors
    assert isinstance(index_one_errors[0], InjectedReservationFailure), (
        type(index_one_errors[0]).__name__
    )
    assert isinstance(index_one_errors[1], SessionCloseFailure), (
        type(index_one_errors[1]).__name__
    )
    # Worker 0: body failure only (no Session to close).
    zero_errors = [error for i, error in errors if i == 0]
    assert len(zero_errors) == 1 and isinstance(zero_errors[0], InjectedReservationFailure)
    # Peers 2..n-1: body BrokenBarrierError, close succeeds.
    for index in range(2, n):
        peer_errors = [error for i, error in errors if i == index]
        assert len(peer_errors) == 1 and isinstance(peer_errors[0], threading.BrokenBarrierError), (
            f"peer {index} should observe one BrokenBarrierError, got {peer_errors}"
        )
