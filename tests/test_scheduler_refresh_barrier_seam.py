"""The module-local bounded Barrier seam and its source-level guards (#1645).

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). This file is NOT freely
splittable: `_seam_source` reads `Path(__file__)` and extracts module-level
definitions by AST, so `run_bounded_barrier_peer_harness`, `_join_all` and BOTH
scheduler originals it pins
(`test_provider_destination_lock_serializes_threads_
when_flock_is_process_scoped` and `test_concurrent_receipt_publishers_keep_
exact_newest_32`) must stay in the same file as the extractor and the four
guards that consume it. Moving any one of them to a sibling partition turns the
uniqueness assertion red.

The subprocess probes below import `InjectedBarrierPeerFailure` from this
module by dotted path; #1101 repointed that import string from the deleted
monolith to this partition.
`tests/test_scheduler_refresh_terminability_probes.py` imports the harness and
`refresh` from here for the same reason.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from packages.common import provider_atomic as provider_atomic_module
from packages.common.provider_atomic import (
    provider_destination_lock,
)
from scripts import scheduler_file_provider_refresh as refresh


class InjectedBarrierPeerFailure(RuntimeError):
    """A pre-arrival Barrier failure with an identity of its own (#1645)."""


def run_bounded_barrier_peer_harness(
    worker: Callable[[int, threading.Barrier], None],
    *,
    parties: int,
    barrier_timeout: float,
    join_timeout: float,
) -> tuple[list[tuple[int, BaseException]], list[threading.Thread]]:
    """Run ``parties`` explicit peers through a bounded Barrier and collect errors.

    The ONE module-local seam for the two Barrier harness families in this
    module (#1645). Each worker is a non-daemon thread (daemon status is not a
    cleanup substitute), the Barrier carries a timeout so a participant that
    raises before arrival releases its peers as ``BrokenBarrierError``, and
    every successfully started peer is joined before the caller inspects
    results. The worker receives the Barrier so it can implement the harness's
    own release point. Returns ``(errors, threads)`` so a caller can assert the
    error collection AND peer liveness before any substantive state assertion.

    Launch is transactional (task 5.2): only the threads whose ``start()``
    succeeded are tracked, and if a later ``start()`` raises, the Barrier is
    aborted so waiting peers release, the tracked peers are joined against one
    absolute cleanup deadline (never one full ``join_timeout`` per peer), and
    the ORIGINAL launch cause is re-raised instead of being masked by a peer
    ``BrokenBarrierError`` or the abort itself.
    """

    barrier = threading.Barrier(parties, timeout=barrier_timeout)
    errors: list[tuple[int, BaseException]] = []
    started: list[threading.Thread] = []

    def run(index: int) -> None:
        try:
            worker(index, barrier)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append((index, error))

    try:
        for index in range(parties):
            thread = threading.Thread(target=run, args=(index,))
            thread.start()
            started.append(thread)
    except BaseException as launch_error:  # pragma: no cover - asserted below
        barrier.abort()
        _join_all(started, deadline=time.monotonic() + join_timeout)
        raise launch_error

    _join_all(started, deadline=time.monotonic() + join_timeout)
    return errors, started


def _join_all(threads: list[threading.Thread], *, deadline: float) -> None:
    """Join every thread under one absolute deadline, never a full timeout each.

    A per-thread ``join(timeout)`` multiplies the parent's wait by the peer
    count, so a stranding peer could leave the parent waiting many times longer
    than the barrier bound (task 5.2). The normal path uses the same single
    deadline as the partial-launch cleanup path.
    """

    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)


def test_provider_destination_lock_serializes_threads_when_flock_is_process_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest-last.json"
    monkeypatch.setattr(provider_atomic_module.fcntl, "flock", lambda *_args: None)
    release_first = threading.Event()
    guard = threading.Lock()
    active = 0
    maximum_active = 0

    def contender(index: int, barrier: threading.Barrier) -> None:
        del index  # the release point is the shared barrier, not the index
        nonlocal active, maximum_active
        barrier.wait()
        with provider_destination_lock(destination):
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
                if active > 1:
                    release_first.set()
            release_first.wait(timeout=0.05)
            with guard:
                active -= 1

    # The module-local bounded seam (design D1/D2): one timeout owner for BOTH
    # scheduler Barrier families, keeping 20 real contenders, the flock-free
    # double, the serialization oracle, and the lock-cleanup assertion. The
    # join bound is strictly larger than the barrier bound (65 > 60) so a
    # parent wait can never expire before the Barrier timeout.
    errors, threads = run_bounded_barrier_peer_harness(
        contender, parties=20, barrier_timeout=60, join_timeout=65
    )

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert maximum_active == 1
    assert not provider_atomic_module._PROCESS_LOCKS


def test_provider_lock_barrier_harness_fails_bounded_when_a_peer_raises_before_arrival(
    tmp_path: Path,
) -> None:
    """A pre-arrival peer failure must break the 20-way lock harness bounded (#1645).

    Pre-fix, this harness's Barrier had no bound and its peers were non-daemon:
    a contender that raised before ``start.wait()`` left the other 19 waiting
    forever, so the parent's bounded join/assertions fired while
    ``threading._shutdown()`` still hung the whole run. The bound converts the
    missing participant into ``BrokenBarrierError`` for every waiter, and no
    started peer survives the test.
    """

    destination = tmp_path / "manifest-last.json"
    maximum_active = 0
    active = 0
    guard = threading.Lock()

    def contender(index: int, barrier: threading.Barrier) -> None:
        nonlocal active, maximum_active
        if index == 0:
            raise InjectedBarrierPeerFailure("injected pre-arrival failure")
        barrier.wait()
        with provider_destination_lock(destination):
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.01)
            with guard:
                active -= 1

    errors, threads = run_bounded_barrier_peer_harness(
        contender, parties=20, barrier_timeout=2, join_timeout=5
    )

    assert len(errors) == 20
    for index, error in errors:
        if index == 0:
            assert isinstance(error, InjectedBarrierPeerFailure)
        else:
            assert isinstance(error, threading.BrokenBarrierError), (
                f"peer {index} should observe BrokenBarrierError, got {type(error).__name__}"
            )
    assert all(not thread.is_alive() for thread in threads)
    assert maximum_active == 0
    assert not provider_atomic_module._PROCESS_LOCKS


def test_concurrent_receipt_publishers_keep_exact_newest_32(tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    receipts = [
        refresh._receipt(
            run_id=f"refresh_{index:02d}",
            started=refresh.datetime(2026, 7, 14, index // 60, index % 60, tzinfo=refresh.UTC),
            outcome="failed",
            reason="provider_invalid",
            phase="precommit",
            providers=[],
        )
        for index in range(40)
    ]

    def publish(index: int, barrier: threading.Barrier) -> None:
        barrier.wait()
        refresh._publish_primary_receipt(root, receipts[index])

    # The module-local bounded seam (design D1/D2): one timeout owner for BOTH
    # scheduler Barrier families, keeping 40 real publishes, the exact
    # newest-32 history, the latest-receipt oracle, and the liveness assertion.
    # The join bound is strictly larger than the barrier bound (65 > 60) so a
    # parent wait can never expire before the Barrier timeout.
    errors, threads = run_bounded_barrier_peer_harness(
        publish, parties=len(receipts), barrier_timeout=60, join_timeout=65
    )

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert json.loads((root / "latest.json").read_text())["run_id"] == "refresh_39"
    assert {path.stem for path in (root / "history").iterdir()} == {
        f"refresh_{index:02d}" for index in range(8, 40)
    }


def test_receipt_publisher_barrier_harness_fails_bounded_when_a_peer_raises_before_arrival(
    tmp_path: Path,
) -> None:
    """A pre-arrival peer failure must break the 40-way receipt harness bounded (#1645).

    Pre-fix, this harness's Barrier had no bound and its peers were non-daemon:
    a publisher that raised before ``barrier.wait()`` left the other 39 waiting
    forever, so the parent's bounded join/assertions fired while
    ``threading._shutdown()`` still hung the whole run. The bound converts the
    missing participant into ``BrokenBarrierError`` for every waiter, and no
    started peer survives the test.
    """

    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    receipts = [
        refresh._receipt(
            run_id=f"refresh_{index:02d}",
            started=refresh.datetime(2026, 7, 14, index // 60, index % 60, tzinfo=refresh.UTC),
            outcome="failed",
            reason="provider_invalid",
            phase="precommit",
            providers=[],
        )
        for index in range(40)
    ]

    def publish(index: int, barrier: threading.Barrier) -> None:
        if index == 0:
            raise InjectedBarrierPeerFailure("injected pre-arrival failure")
        barrier.wait()
        refresh._publish_primary_receipt(root, receipts[index])

    errors, threads = run_bounded_barrier_peer_harness(
        publish, parties=len(receipts), barrier_timeout=2, join_timeout=5
    )

    assert len(errors) == len(receipts)
    for index, error in errors:
        if index == 0:
            assert isinstance(error, InjectedBarrierPeerFailure)
        else:
            assert isinstance(error, threading.BrokenBarrierError), (
                f"peer {index} should observe BrokenBarrierError, got {type(error).__name__}"
            )
    assert all(not thread.is_alive() for thread in threads)
    assert not (root / "latest.json").exists()


class InjectedThreadStartFailure(RuntimeError):
    """A deterministic nth ``Thread.start()`` failure with an identity of its own (#1645)."""


def test_barrier_seam_partial_thread_start_aborts_barrier_joins_peers_and_preserves_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later ``Thread.start()`` failure must abort, clean up, and re-raise (#1645, task 5.2).

    The shipped seam starts ``parties`` explicit threads in a loop. If the
    k-th ``start()`` raises after earlier peers are running, those peers would
    otherwise wait forever inside ``Barrier.wait()`` (the exact strand). The
    seam must abort the Barrier, join every successfully started peer against
    one absolute cleanup deadline, and re-raise the ORIGINAL launch cause --
    never a peer ``BrokenBarrierError`` or an ``abort()`` side effect.

    The regression proof observes the SHIPPING seam's own ``Thread.join()``
    calls on the exactly-tracked successfully started peers (round-2 verifier
    evidence-r2-01): if only the exception-path join is removed, no seam join
    is observed and the joined-before-reraise assertion deterministically reds.
    Worker side effects (``started_events``) alone cannot see a missing
    parent-side join.
    """

    started_events: list[threading.Event] = [threading.Event() for _ in range(5)]
    barrier_aborted = threading.Event()
    start_attempts = 0

    real_start = threading.Thread.start
    # Exact Thread objects whose real start() succeeded -- NOT inferred from
    # worker side effects.
    successfully_started: list[threading.Thread] = []

    def failing_start(thread: threading.Thread) -> None:
        nonlocal start_attempts
        start_attempts += 1
        if start_attempts == 3:
            raise InjectedThreadStartFailure("injected nth start failure")
        real_start(thread)
        successfully_started.append(thread)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    real_abort = threading.Barrier.abort

    def tracked_abort(barrier: threading.Barrier) -> None:
        barrier_aborted.set()
        real_abort(barrier)

    monkeypatch.setattr(threading.Barrier, "abort", tracked_abort)

    # Observe the REAL join() calls the shipping seam makes on the tracked
    # peers. `launch_observable` flips only after pytest.raises returns, so a
    # recorded join proves the seam joined that peer BEFORE the launch
    # exception became observable to the parent.
    real_join = threading.Thread.join
    launch_observable = False
    helper_joins: list[tuple[threading.Thread, bool, bool]] = []

    def observing_join(thread: threading.Thread, *args: Any, **kwargs: Any) -> None:
        real_join(thread, *args, **kwargs)
        if thread in successfully_started:
            helper_joins.append((thread, thread.is_alive(), launch_observable))

    monkeypatch.setattr(threading.Thread, "join", observing_join)

    def worker(index: int, barrier: threading.Barrier) -> None:
        started_events[index].set()
        barrier.wait()

    started = time.monotonic()
    try:
        with pytest.raises(InjectedThreadStartFailure, match="injected nth start failure"):
            run_bounded_barrier_peer_harness(
                worker, parties=5, barrier_timeout=10, join_timeout=5
            )
    finally:
        # Fallback cleanup via the saved REAL join, bypassing the observation
        # wrapper: even a mutated seam that never joins cannot leave a live
        # peer. It records nothing, so it cannot make the proof below pass.
        for thread in successfully_started:
            real_join(thread, timeout=5)
    launch_observable = True
    elapsed = time.monotonic() - started

    assert barrier_aborted.is_set(), "partial launch must abort the Barrier"
    assert elapsed < 5, f"cleanup must respect one absolute deadline, took {elapsed:.2f}s"
    # Exact successfully started peer population (this schedule: two peers).
    assert len(successfully_started) == 2, successfully_started
    # The shipping seam joined every successfully started peer exactly once,
    # BEFORE the launch exception became observable to the parent, and each
    # observed join returned with that peer no longer alive.
    assert len(helper_joins) == len(successfully_started), helper_joins
    assert {id(thread) for thread, _, _ in helper_joins} == {
        id(thread) for thread in successfully_started
    }
    assert all(not alive for _, alive, _ in helper_joins), helper_joins
    assert all(not observable for _, _, observable in helper_joins), helper_joins
    # Both successfully started peers ran to completion (joined by the seam) and
    # the third thread never started, so only indices 0 and 1 report ready.
    assert started_events[0].is_set(), "the first peer must have started"
    assert started_events[1].is_set(), "the second peer must have started"
    assert not started_events[2].is_set(), "the third thread must never have started"
    assert not started_events[3].is_set() and not started_events[4].is_set()


def test_barrier_seam_attributes_errors_by_participant_index_not_completion_order() -> None:
    """Delayed participant 0 must not reorder its error to the front (#1645, task 5.7).

    ``run_bounded_barrier_peer_harness`` appends ``(index, error)`` in
    completion order, so a peer that arrives early can record its
    ``BrokenBarrierError`` before participant 0's injected pre-arrival failure.
    Any consumer that reads ``errors[0]`` therefore depends on incidental
    scheduling. The harness must expose a per-index lookup so callers can
    attribute by participant index instead of append order.
    """

    def worker(index: int, barrier: threading.Barrier) -> None:
        if index == 0:
            time.sleep(0.2)
            raise InjectedBarrierPeerFailure("injected pre-arrival failure")
        barrier.wait()

    errors, threads = run_bounded_barrier_peer_harness(
        worker, parties=4, barrier_timeout=2, join_timeout=5
    )

    by_index = {index: error for index, error in errors}
    assert len(errors) == 4
    assert isinstance(by_index[0], InjectedBarrierPeerFailure), type(by_index[0]).__name__
    for index in range(1, 4):
        assert isinstance(by_index[index], threading.BrokenBarrierError), (
            f"peer {index} should observe BrokenBarrierError, got {type(by_index[index]).__name__}"
        )
    assert all(not thread.is_alive() for thread in threads)
    assert max(index for index, _ in errors) == 3  # every participant attributed


def test_ast_guards_require_exactly_one_module_level_function_definition() -> None:
    """AST source extraction must bind one top-level owner, not a nested decoy (#1645, task 5.5).

    A nested SAME-NAME function is the exact decoy shape: it must never be
    selected, and the shipped seam source must still resolve to the single
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
    segment = _module_level_segment(same_name_nested, "target")
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
        _module_level_segment(nested_only, "target")
    except AssertionError:
        pass
    else:
        raise AssertionError("nested-only target must fail deterministically (zero module-level owners)")

    # Zero module-level owners -> deterministic failure.
    zero_owner = "def other():\n    return 1\n"
    try:
        _module_level_segment(zero_owner, "target")
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
        _module_level_segment(duplicate, "target")
    except AssertionError:
        pass
    else:
        raise AssertionError("duplicate top-level owners must fail deterministically")

    # The shipped extractor is exercised on the real module so the real guard
    # target still resolves exactly once.
    assert _seam_source("run_bounded_barrier_peer_harness")


def _module_level_segment(text: str, function_name: str) -> str:
    """Return the exact source of the single module-level ``function_name`` in ``text``.

    The ONE uniqueness implementation (task 5.5): only ``ast.Module.body`` is
    examined, exactly one module-level ``FunctionDef`` must match, and nested
    same-name definitions are ignored. ``_seam_source`` delegates here so the
    synthetic nested/zero/duplicate cases exercise the shipping logic (Phase 2
    gap 2).
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


def test_barrier_seam_constructed_variants_execute_their_red_observables() -> None:
    """Executable no-capture / no-join seam variants redden, then are cleaned up (#1645).

    Unlike the string-comparison source pins, these CONSTRUCTED variants of the
    real seam are executed in a bounded child subprocess so a no-capture or
    no-join regression cannot hide: each variant must fail with its own
    observable, and the child is killed/reaped if it ever hangs (a no-join
    variant can strand the interpreter).
    """

    seam = _seam_source("run_bounded_barrier_peer_harness")
    join_helper = _seam_source("_join_all")

    # Build variants by deleting the shipped lines from the real seam source.
    no_capture = seam.replace(
        "            errors.append((index, error))",
        "            del error  # no-capture mutant",
    )
    no_join = seam.replace(
        "    _join_all(started, deadline=time.monotonic() + join_timeout)\n    return errors, started",
        "    pass  # no-join mutant: peers are never joined\n    return errors, started",
    )
    assert "errors.append((index, error))" not in no_capture
    assert "    _join_all(started, deadline=time.monotonic() + join_timeout)\n    return errors, started" not in no_join

    # The seam body references the module-global barrier_timeout/join_timeout
    # defaults only through parameters; wrap each variant in a driver that
    # imports the same module the real seam uses. The join helper is shipped
    # alongside because the variants call it exactly as the real seam does.
    repository = Path(__file__).resolve().parents[1]

    def drive(variant_source: str, tag: str, tail: str) -> tuple[str, str]:
        probe = (
            "import sys, threading, time\n"
            "from collections.abc import Callable\n"
            "from tests.test_scheduler_refresh_barrier_seam import InjectedBarrierPeerFailure\n"
            + join_helper
            + "\n\n"
            + variant_source
            + "\n\n"
            + "def worker(index, barrier, ready, post_barrier_ready, release):\n"
            + "    if index == 0:\n"
            + "        raise InjectedBarrierPeerFailure('injected pre-arrival failure')\n"
            + "    try:\n"
            + "        barrier.wait()\n"
            + "        print('worker-arrived', index)\n"
            + "    except threading.BrokenBarrierError:\n"
            + "        # The peer leaves the broken barrier; it must then report\n"
            + "        # post-barrier readiness and stay alive on the release gate.\n"
            + "        pass\n"
            + "    post_barrier_ready.set()\n"
            + "    assert release.wait(timeout=5), 'release gate never opened'\n"
            + "\n"
            + "ready = threading.Event()\n"
            + "post_barrier_ready = threading.Event()\n"
            + "release = threading.Event()\n"
            + "errors, threads = run_bounded_barrier_peer_harness(\n"
            + "    lambda index, barrier: worker(index, barrier, ready, post_barrier_ready, release),\n"
            + "    parties=4, barrier_timeout=2, join_timeout=5)\n"
            + "print('variant-returned')\n"
            + "print('errors:', [type(e).__name__ for _, e in errors])\n"
            + tail
        )
        process = subprocess.Popen(
            [sys.executable, "-c", probe],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            try:
                stdout, stderr = process.communicate(timeout=10.0)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise AssertionError(f"{tag} variant hung: no-join/no-capture seam strand") from None
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return stdout, stderr

    # No-capture: the peer failure is swallowed, so `errors` stays empty and the
    # injected cause is never reported.
    no_capture_stdout, no_capture_stderr = drive(no_capture, "no-capture", "")
    assert "variant-returned" in no_capture_stdout, (
        f"no-capture variant printed {no_capture_stdout.strip()!r} / {no_capture_stderr.strip()!r}"
    )
    assert "errors: []" in no_capture_stdout, (
        f"no-capture variant should swallow the error, got {no_capture_stdout.strip()!r}"
    )

    # No-join: peers may still be alive when the parent returns. A NON-FAILING
    # peer reports post-barrier readiness and then blocks on an explicit
    # release gate; index 0's failure never sets `post_barrier_ready`. The
    # parent waits for that gate, asserts at least one controlled peer is alive
    # SPECIFICALLY on the release gate, opens it, joins every peer, and asserts
    # zero residue (Phase 2 gap 7/C: the release gate is load-bearing, no exact
    # counts).
    no_join_tail = (
        "assert post_barrier_ready.wait(timeout=5), 'no non-failing peer reached post-barrier readiness'\n"
        "live = sum(t.is_alive() for t in threads)\n"
        "assert live >= 1, f'no controlled peer alive on the release gate ({live})'\n"
        "print('live-peers-confirmed:', live)\n"
        "release.set()\n"
        "for t in threads:\n"
        "    t.join(timeout=5)\n"
        "assert not any(t.is_alive() for t in threads), 'peer residue after release'\n"
        "print('released-peers:', sum(t.is_alive() for t in threads))\n"
    )
    no_join_stdout, no_join_stderr = drive(no_join, "no-join", no_join_tail)
    assert "variant-returned" in no_join_stdout, (
        f"no-join variant printed {no_join_stdout.strip()!r} / {no_join_stderr.strip()!r}"
    )
    assert "live-peers-confirmed:" in no_join_stdout, (
        f"no-join variant must observe a controlled live peer, got {no_join_stdout.strip()!r}"
    )
    assert "released-peers: 0" in no_join_stdout, (
        f"no-join variant must release and join every peer, got {no_join_stdout.strip()!r}"
    )


def _seam_source(function_name: str) -> str:
    """Read ``function_name``'s exact source from this file via AST.

    ``inspect.getsource`` is unreliable under pytest's assertion rewriting when
    the module is fully loaded (it can return a shifted/other block), so the
    anti-vacuity mutants are derived from the AST segment of the exact live
    seam instead of mutating the tracked working tree.

    Only module-level definitions bind (task 5.5): ``ast.Module.body`` is
    examined, exactly one top-level ``FunctionDef`` must match, and nested
    same-name definitions are never selected. Zero or duplicate top-level
    owners fail deterministically instead of first-match binding. The
    uniqueness logic lives in ``_module_level_segment`` so the synthetic
    decoy/zero/duplicate cases execute the exact code the shipping extractor
    uses (Phase 2 gap 2).
    """

    text = Path(__file__).read_text()
    return _module_level_segment(text, function_name)


def test_barrier_seam_source_legs_each_fail_bounded_and_distinct() -> None:
    """Source-mutation guards for the module-local Barrier seam (#1645).

    These are STRING-COMPARISON guards, not executed semantic mutants: each
    asserts that removing one shipped repair line actually changes the derived
    seam source, so the removal target is real and not dead text. The
    behavioral red observables (barrier bound, error capture, join) are
    exercised by the failure-injection tests and the bounded subprocess mutant
    proof, not here.
    """

    source = _seam_source("run_bounded_barrier_peer_harness")

    # Leg 1: remove the barrier bound -> a pre-arrival worker exception strands peers.
    unbounded = source.replace(
        "threading.Barrier(parties, timeout=barrier_timeout)",
        "threading.Barrier(parties)",
    )
    assert source != unbounded, "unbounded mutant must differ from source"
    assert "timeout=barrier_timeout" not in unbounded

    # Leg 2: remove the error capture -> a worker failure goes unreported.
    no_capture = source.replace(
        "errors.append((index, error))",
        "del error  # error capture removed",
    )
    assert source != no_capture, "error-capture mutant must differ from source"
    assert "errors.append((index, error))" not in no_capture

    # Leg 3: remove the absolute-deadline join -> a live peer goes undetected.
    no_join = source.replace(
        "    _join_all(started, deadline=time.monotonic() + join_timeout)\n    return errors, started",
        "    return errors, started",
    )
    assert source != no_join, "join-removed mutant must differ from source"
    assert "    _join_all(started, deadline=time.monotonic() + join_timeout)\n    return errors, started" not in no_join

    # Leg 4: join every peer with the full join bound each -> a stranding peer
    # multiplies the parent's wait by the peer count instead of one deadline.
    per_peer = source.replace(
        "    _join_all(started, deadline=time.monotonic() + join_timeout)\n    return errors, started",
        "    for thread in started:\n        thread.join(timeout=join_timeout)\n    return errors, started",
    )
    assert source != per_peer, "per-peer-join mutant must differ from source"
    deadline_join = "    _join_all(started, deadline=time.monotonic() + join_timeout)\n    return errors, started"
    assert deadline_join in source
    assert deadline_join not in per_peer

    # Both harness families demonstrably consume the seam (task 1.3): the two
    # failure-injection tests call it through the same helper.
    assert len({source, unbounded, no_capture, no_join, per_peer}) == 5

    # Leg 5: the join helper must apply ONE absolute deadline, never a full
    # join bound per peer (which would multiply the parent wait by peer count).
    join_helper = _seam_source("_join_all")
    assert "for thread in threads:" in join_helper
    assert "remaining = deadline - time.monotonic()" in join_helper
    per_peer_helper = join_helper.replace(
        (
            "        remaining = deadline - time.monotonic()\n"
            "        if remaining <= 0:\n"
            "            break\n"
            "        thread.join(timeout=remaining)"
        ),
        "        thread.join(timeout=timeout_each)  # per-peer full timeout mutant",
    )
    assert join_helper != per_peer_helper, "absolute-deadline helper mutant must differ from source"


def test_scheduler_originals_pin_seam_call_and_assertion_ordering() -> None:
    """Exact-site source pins for BOTH scheduler originals (#1645).

    STRING-COMPARISON guards (not executed mutants): each original must call
    the bounded seam with barrier 60 < join 65, and must assert worker errors
    then liveness BEFORE its first substantive oracle. Deleting either the seam
    call geometry or moving the assertions after the oracle reddens here.
    """

    cases = (
        (
            "test_provider_destination_lock_serializes_threads_when_flock_is_process_scoped",
            "run_bounded_barrier_peer_harness(\n        contender, parties=20, barrier_timeout=60, join_timeout=65",
            "assert maximum_active == 1",
        ),
        (
            "test_concurrent_receipt_publishers_keep_exact_newest_32",
            (
                "run_bounded_barrier_peer_harness(\n"
                "        publish, parties=len(receipts), barrier_timeout=60, join_timeout=65"
            ),
            'assert json.loads((root / "latest.json").read_text())["run_id"] == "refresh_39"',
        ),
    )
    for function_name, seam_call, first_oracle in cases:
        original = _seam_source(function_name)

        # Exact seam call geometry: bounded seam with parent wait > barrier bound.
        assert seam_call in original, f"{function_name} must call the seam with barrier 60 < join 65"
        no_seam = original.replace(seam_call, "del seam_call  # seam call removed")
        assert original != no_seam, f"{function_name} seam-call mutant must differ"

        # Error assertion then liveness assertion, both BEFORE the first oracle.
        assert "    assert not errors" in original
        assert "    assert all(not thread.is_alive() for thread in threads)" in original
        assert original.index("    assert not errors") < original.index(first_oracle)
        assert original.index("    assert all(not thread.is_alive() for thread in threads)") < original.index(
            first_oracle
        )
        moved_after_oracle = original.replace(
            "    assert not errors\n    assert all(not thread.is_alive() for thread in threads)\n",
            "",
        )
        assert original != moved_after_oracle, f"{function_name} ordering-mutant must differ"
