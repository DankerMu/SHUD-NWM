"""File-submit commit barrier: 2-party CAS race, bounded harness failure
injection, and source-mutation guards (#1645).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    wait,
)
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import _file_cohort_repository


def _consume_futures_deadline(
    futures: list[Any], *, deadline: float, outcomes: list[str]
) -> list[BaseException]:
    """Consume every returned Future under one absolute deadline.

    The 2-party executor harness's consumption seam (task 5.2): every Future is
    drained within a single absolute deadline -- never one full result bound
    per future -- so a peer that breaks late cannot multiply the parent's wait
    by the peer count. ``concurrent.futures.wait`` waits ONCE for the remaining
    deadline and returns every completed Future regardless of list order, so a
    later completed error can never hide behind an earlier pending Future
    (Phase 2 gap 6). Every completed Future is inspected; every Future still
    pending when the deadline expires is recorded as a ``TimeoutError`` so no
    returned Future is silently unobserved. Every exception (injected launch
    cause, peer ``BrokenBarrierError``, code-under-test failure) is returned
    for the caller to assert; successful outcomes are appended to ``outcomes``.
    """

    errors: list[BaseException] = []
    pending = set(futures)
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.extend(FutureTimeoutError() for _ in pending)
            break
        done, still_pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
        for future in done:
            try:
                outcomes.append(future.result())
            except BaseException as error:
                errors.append(error)
        pending = still_pending
    return errors


def _run_executor_barrier(
    barrier: threading.Barrier,
    fn: Callable[[Any], str],
    args: list[Any],
    *,
    result_timeout: float,
) -> tuple[list[str], list[BaseException]]:
    """Submit ``fn`` for every ``args`` entry transactionally and drain every Future.

    The 2-party gateway executor harness's transactional launch seam (task
    5.2): every returned Future is retained. If a later ``pool.submit`` (or the
    executor's underlying worker ``Thread.start()``) raises after at least one
    Future is running, the Barrier is aborted so that running peer leaves
    ``Barrier.wait()``, every returned Future is drained under one absolute
    deadline, and the ORIGINAL launch cause is re-raised instead of being
    masked by a peer ``BrokenBarrierError`` or a result symptom. On the normal
    path every returned Future is consumed the same way before the context
    exits.
    """

    outcomes: list[str] = []
    submitted: list[Any] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            for arg in args:
                submitted.append(pool.submit(fn, arg))
        except BaseException as launch_error:  # pragma: no cover - asserted below
            barrier.abort()
            errors = _consume_futures_deadline(
                submitted, deadline=time.monotonic() + result_timeout, outcomes=outcomes
            )
            raise launch_error
        errors = _consume_futures_deadline(
            submitted, deadline=time.monotonic() + result_timeout, outcomes=outcomes
        )
    return outcomes, errors


def _test_function_source(function_name: str) -> str:
    """Read ``function_name``'s exact source from this file via AST.

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


    text = Path(__file__).read_text()
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



def test_file_submit_attempt_barrier_race_commits_only_one_slurm_id(tmp_path: Any) -> None:
    from threading import Barrier

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(tmp_path, member_count=18)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    # Generous hang backstop (design D2): turns a pre-arrival worker exception
    # into BrokenBarrierError so the peer releases instead of stranding the
    # executor's non-daemon worker at interpreter shutdown. The parent
    # future.result bound is STRICTLY LARGER (65 > 60) so a parent wait can
    # never expire before the Barrier bound.
    barrier = Barrier(2, timeout=60)

    def commit(slurm_job_id: str) -> str:
        contender = FileOrchestrationJournalRepository(repository.root)
        barrier.wait()
        return contender.commit_pipeline_job_submit_attempt(
            key,
            expected_submission_attempt=1,
            slurm_job_id=slurm_job_id,
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        ).outcome

    # Explicit submit + FULL result consumption (design D3): every future is
    # consumed so a first failure cannot leave a peer's BrokenBarrierError
    # unobserved, and the barrier bound (not a context-manager shutdown) is what
    # releases a peer when the other worker fails before/at the barrier. The
    # result bound (65) is strictly larger than the barrier bound (60). The
    # transactional helper aborts the barrier and preserves the launch cause on
    # a partial submission failure (task 5.2).
    outcomes, errors = _run_executor_barrier(
        barrier, commit, ["17667", "17668"], result_timeout=65
    )

    assert not errors, errors
    assert sorted(outcomes) == ["applied", "collision"]
    reopened = FileOrchestrationJournalRepository(repository.root)
    row = reopened.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")
    assert row is not None
    assert row["slurm_job_id"] in {"17667", "17668"}
    assert len(reopened.query_inflight_jobs()) == 1
    assert reopened.query_reserved_unbound_jobs() == []


def test_file_submit_barrier_harness_fails_bounded_when_the_repository_constructor_raises_before_arrival(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-arrival constructor failure must propagate, not strand the executor (#1645).

    The file-submit harness family builds each contender inside the worker and
    then releases both at the Barrier. If construction raises BEFORE the
    barrier, the surviving peer waits in ``Barrier.wait()``; with the pre-fix
    unbounded barrier the executor's context shutdown then blocks on that
    worker forever and the whole pytest process hangs at
    ``threading._shutdown()``. The barrier bound breaks the peer out, and full
    consumption of every future re-raises BOTH the injected constructor
    exception and the peer's ``BrokenBarrierError``.

    The failure injection is DETERMINISTIC via a handshake Event AND fires at
    the ACTUAL ``FileOrchestrationJournalRepository(repository.root)``
    expression: the module-global constructor is patched so the failing worker
    raises ``ConstructorFailure`` there, while the peer invokes the real
    constructor, reaches the pre-wait line (handshake), and enters the Barrier.
    Submission order alone is not enough — the Event proves the surviving
    Future is already RUNNING and has reached the line immediately before
    ``barrier.wait()``, so it cannot be canceled when the first Future raises.
    """

    import threading as _threading
    from threading import Barrier, BrokenBarrierError, Event

    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    repository = _file_cohort_repository(tmp_path, member_count=18)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    # Barrier bound < future-result timeout, so the worker's own
    # BrokenBarrierError propagates instead of the parent timing out first.
    barrier = Barrier(2, timeout=2)
    peer_waiting = Event()
    constructor_invoked: list[str] = []
    thread_local = _threading.local()

    class ConstructorFailure(RuntimeError):
        pass

    real_constructor = journal_module.FileOrchestrationJournalRepository

    def failing_constructor(root: Any, *args: Any, **kwargs: Any) -> Any:
        # The shipping worker executes `FileOrchestrationJournalRepository(
        # repository.root)` here. The failing Future (17667) raises with the
        # injected identity; the peer constructs a real contender. Thread-local
        # identity selects the failing invocation, independent of scheduling.
        invoked_by = getattr(thread_local, "slurm_job_id", None)
        if invoked_by is not None:
            constructor_invoked.append(invoked_by)
        if invoked_by == "17667":
            raise ConstructorFailure("injected constructor failure")
        return real_constructor(root, *args, **kwargs)

    monkeypatch.setattr(
        journal_module,
        "FileOrchestrationJournalRepository",
        failing_constructor,
    )

    def commit(slurm_job_id: str) -> str:
        thread_local.slurm_job_id = slurm_job_id
        # The failing Future waits for the peer at the pre-wait line BEFORE
        # invoking the (patched) constructor, proving the peer is running and
        # past the cancellation boundary.
        if slurm_job_id == "17667":
            assert peer_waiting.wait(timeout=5), "peer never reached the pre-wait line"
        contender = journal_module.FileOrchestrationJournalRepository(repository.root)
        if slurm_job_id == "17668":
            # The surviving Future is already RUNNING and has reached the line
            # immediately before barrier.wait() before the first Future's
            # constructor raises, so it cannot be canceled when the first
            # Future raises; it will proceed into the wait absent an unrelated
            # unexpected exception (not claimed to be already inside).
            peer_waiting.set()
        barrier.wait()
        return contender.commit_pipeline_job_submit_attempt(
            key,
            expected_submission_attempt=1,
            slurm_job_id=slurm_job_id,
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        ).outcome

    try:
        # The injection copy uses the same transactional submit/drain seam as
        # the shipped harness (task 5.2).
        outcomes, failures = _run_executor_barrier(
            barrier, commit, ["17667", "17668"], result_timeout=5
        )
    finally:
        monkeypatch.undo()

    # The patched constructor ran for the failing worker (raised) and the peer,
    # in either scheduling order.
    assert sorted(constructor_invoked) == ["17667", "17668"], constructor_invoked
    # The handshake Event must be set: the peer task crossed the cancellation
    # boundary and reached the pre-wait line, so the BrokenBarrierError below
    # is not a vacuously-absent peer.
    assert peer_waiting.is_set(), "peer never reached the pre-wait line"
    assert outcomes == []
    assert any(isinstance(error, ConstructorFailure) for error in failures), failures
    assert any(isinstance(error, BrokenBarrierError) for error in failures), (
        f"peer should observe BrokenBarrierError, got {[type(e).__name__ for e in failures]}"
    )


class InjectedExecutorStartupFailure(RuntimeError):
    """A deterministic partial executor startup failure with an identity of its own (#1645)."""


def test_executor_partial_startup_aborts_barrier_drains_futures_and_preserves_cause(
    tmp_path: Any,
) -> None:
    """Partial executor startup failure after the first Future runs must abort and preserve the cause (#1645, task 5.2).

    The 2-party harness submits both callables into a ``ThreadPoolExecutor``.
    If the executor's underlying worker ``Thread.start()`` raises while the
    first Future is already running, the submission/startup loop breaks early
    and the running peer would otherwise wait forever inside
    ``Barrier.wait()``. The transactional seam must abort the Barrier, drain
    every returned Future under one absolute deadline, exit the executor
    without waiting indefinitely, and surface the ORIGINAL startup cause --
    not a peer ``BrokenBarrierError`` or a result symptom.

    The injection is DETERMINISTIC: the first Future reaches the line
    immediately before ``barrier.wait()`` (handshake Event) before the second
    worker's ``Thread.start()`` raises, so the running peer really is past the
    cancellation boundary.
    """

    from threading import Barrier, Event

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(tmp_path, member_count=18)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    barrier = Barrier(2, timeout=2)
    peer_waiting = Event()
    peer_done = Event()
    barrier_aborted = Event()
    start_attempts = 0

    real_start = threading.Thread.start

    def failing_start(thread: threading.Thread) -> None:
        nonlocal start_attempts
        start_attempts += 1
        if start_attempts == 2:
            # Deterministic partial startup (Phase 2 gap 5): the second worker
            # start raises only AFTER the first Future is already RUNNING and
            # has crossed the pre-wait boundary -- the peer is past the
            # cancellation point, so the injection cannot fire early.
            assert peer_waiting.wait(timeout=5), "peer never reached the pre-wait line"
            raise InjectedExecutorStartupFailure("injected second worker start failure")
        real_start(thread)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(threading.Thread, "start", failing_start)

    real_abort = Barrier.abort

    def tracked_abort(barrier: Barrier) -> None:
        barrier_aborted.set()
        real_abort(barrier)

    monkeypatch.setattr(Barrier, "abort", tracked_abort)

    def commit(slurm_job_id: str) -> str:
        contender = FileOrchestrationJournalRepository(repository.root)
        # The first Future is already RUNNING and has reached the line
        # immediately before barrier.wait() before the second worker's start
        # raises, so it cannot be canceled when startup fails (task 5.2).
        peer_waiting.set()
        try:
            barrier.wait()
            return contender.commit_pipeline_job_submit_attempt(
                key,
                expected_submission_attempt=1,
                slurm_job_id=slurm_job_id,
                transition=AcceptedSubmitTransition.accepted(status="submitted"),
            ).outcome
        finally:
            # The peer finishes (with BrokenBarrierError after the abort); the
            # parent must prove it completed before the helper re-raises.
            peer_done.set()

    try:
        with pytest.raises(InjectedExecutorStartupFailure, match="injected second worker start failure"):
            _run_executor_barrier(
                barrier, commit, ["17667", "17668"], result_timeout=5
            )
    finally:
        monkeypatch.undo()

    assert peer_waiting.is_set(), "the first Future must have reached the pre-wait line"
    assert peer_done.is_set(), "the running peer must have finished before the helper re-raised"
    assert barrier_aborted.is_set(), "partial startup must abort the Barrier"


def test_file_submit_barrier_source_legs_each_fail_bounded_and_distinct() -> None:
    """Source-mutation guards: each executor repair leg is a distinct string-removal target (#1645).

    These are STRING-COMPARISON guards, not executed semantic mutants: each
    asserts that removing one shipped repair line actually changes the derived
    source, so the removal target is real and not dead text. The behavioral red
    observables (barrier bound, full future consumption, error assertion) are
    exercised by the failure-injection tests, not here.
    """

    source = _test_function_source("test_file_submit_attempt_barrier_race_commits_only_one_slurm_id")

    # Leg 1: remove the barrier bound -> a pre-arrival worker exception strands the
    # executor's non-daemon worker and the context exit hangs.
    unbounded = source.replace("Barrier(2, timeout=60)", "Barrier(2)")
    assert source != unbounded, "unbounded mutant must differ from source"
    assert "Barrier(2)" in unbounded and "Barrier(2, timeout=60)" not in unbounded

    # Leg 2: the launch must go through the transactional helper with
    # absolute-deadline future consumption (task 5.2) -- an inline per-future
    # result loop would multiply the parent wait by the future count.
    assert (
        "    outcomes, errors = _run_executor_barrier(\n"
        "        barrier, commit, [\"17667\", \"17668\"], result_timeout=65\n"
        "    )" in source
    )
    inline_consume = source.replace(
        "    outcomes, errors = _run_executor_barrier(\n"
        "        barrier, commit, [\"17667\", \"17668\"], result_timeout=65\n"
        "    )",
        "    outcomes, errors = [], []\n"
        "    with ThreadPoolExecutor(max_workers=2) as pool:\n"
        "        futures = [pool.submit(commit, s) for s in (\"17667\", \"17668\")]\n"
        "        for future in futures:\n"
        "            outcomes.append(future.result(timeout=65))",
    )
    assert source != inline_consume, "inline-consume mutant must differ from source"
    assert "result_timeout=65" in source

    # Leg 3: remove the error assertion before substantive output -> a worker
    # failure could be masked by the downstream outcome check.
    no_error_assert = source.replace(
        "    assert not errors, errors\n    assert sorted(outcomes) == [\"applied\", \"collision\"]",
        "    assert sorted(outcomes) == [\"applied\", \"collision\"]",
    )
    assert source != no_error_assert, "error-assert-removed mutant must differ from source"
    assert "assert not errors, errors" in source

    # Leg 4: drop the parent-wait margin (result bound equal to the barrier
    # bound) -> a parent wait could expire milliseconds before the Barrier
    # timeout, recording a parent TimeoutError before the peer's BrokenBarrierError.
    no_margin = source.replace("result_timeout=65", "result_timeout=60")
    assert source != no_margin, "no-margin mutant must differ from source"
    assert "result_timeout=65" in source
    assert "result_timeout=65" not in no_margin

    # Exact-site ordering pin: `assert not errors` must precede the substantive
    # `assert sorted(outcomes)` output check (order, not just adjacency removal).
    assert source.index("    assert not errors, errors") < source.index(
        "    assert sorted(outcomes) == [\"applied\", \"collision\"]"
    )
    reordered = source.replace(
        "    assert not errors, errors\n    assert sorted(outcomes) == [\"applied\", \"collision\"]",
        "    assert sorted(outcomes) == [\"applied\", \"collision\"]\n    assert not errors, errors",
    )
    assert source != reordered, "reordered-mutant must differ from source"
    assert "    assert not errors, errors\n    assert sorted(outcomes)" in source

    # The five legs are pairwise distinct source mutations.
    assert len({source, unbounded, inline_consume, no_error_assert, no_margin, reordered}) == 6
