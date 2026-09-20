"""Whole-run terminability: bounded child subprocesses for the two harnesses
(#1633, #1645).

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). Owns the spin-wait writer
harness and its three in-process legs, the bounded-subprocess driver, the real
40-way receipt-barrier terminability proof with its unbounded mutant, and the
two driver cases that separate a pre-checkpoint startup stall from the expected
mutant red.

The child probes import `run_spin_wait_writer_harness` from THIS module and
`InjectedBarrierPeerFailure` / `run_bounded_barrier_peer_harness` / `refresh`
from `tests/test_scheduler_refresh_barrier_seam.py`, which owns the seam; #1101
repointed all three import strings off the deleted monolith.
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
from typing import NamedTuple

import pytest

from packages.common.provider_atomic import (
    atomic_replace_provider_bytes,
)
from tests.provider_mode_helpers import write_provider_destination

SPIN_WAIT_HANG_BACKSTOP_SECONDS = 30.0
"""Hang backstop for `run_spin_wait_writer_harness` -- NOT a performance bound.

The writer below makes 40 real `atomic_replace_provider_bytes` calls, each of
which fsyncs, and a loaded CI runner must never trip this: the value only has
to be shorter than a human's patience, not close to the expected runtime (well
under a second locally). A tight value would turn the harness's own
`assert not thread.is_alive()` into a fresh flake source.
"""


def run_spin_wait_writer_harness(
    writer_body: Callable[[], None],
    observe: Callable[[], None],
    *,
    deadline_seconds: float = SPIN_WAIT_HANG_BACKSTOP_SECONDS,
    started_threads: list[threading.Thread] | None = None,
) -> None:
    """Run `writer_body` in a thread while the caller spin-waits, and FAIL on trouble.

    The ONE shared harness for the spin-wait writer tests in this module
    (#1633). It is shared on purpose: a copied harness drifts, and once it has,
    deleting the `finally` or the `assert not errors` from the real test leaves
    the failure-injection tests green -- the guard stops guarding the thing it
    exists to guard.

    Three exit paths are covered, and none of them is a hang:

    * the writer returns  -> the `finally` sets the sentinel, the loop ends;
    * the writer raises   -> the catch-all records it, the `finally` STILL sets
      the sentinel, and `assert not errors` names the exception;
    * the writer blocks   -> the `finally` is never reached, so the loop
      deadline is the only backstop and it fails on the sentinel assertion.

    The assertion order is load-bearing: the worker exception is reported
    before any assertion the CALLER makes on what `observe` collected, because
    a worker that dies on iteration 0 leaves that collection empty and the
    symptom would then mask the cause.

    `started_threads`, when given, receives the worker thread before it starts,
    so a caller that deliberately blocks the worker can release and join it.
    """

    finished = threading.Event()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            writer_body()
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)
        finally:
            # Unconditional: a writer that raised must still release the
            # spin-loop, or its failure surfaces as a hang instead.
            finished.set()

    # Daemon is the last-resort run-termination backstop (design D8): Python
    # cannot cancel a blocked thread, and a non-daemon worker that outlives
    # the deadline assertion would strand threading._shutdown() so the whole
    # pytest process hangs. Every controllably blocked caller still releases
    # and joins its worker; daemon status is not a cleanup substitute.
    thread = threading.Thread(target=worker, daemon=True)
    if started_threads is not None:
        started_threads.append(thread)
    thread.start()
    deadline = time.monotonic() + deadline_seconds
    while not finished.is_set() and time.monotonic() < deadline:
        observe()
    thread.join(timeout=deadline_seconds)

    assert not errors, errors
    assert finished.is_set(), f"spin-wait deadline of {deadline_seconds}s exceeded before the writer finished"
    assert not thread.is_alive()


def test_provider_atomic_readers_observe_only_complete_old_or_new_json(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    old = json.dumps({"generation": "old", "rows": list(range(50))}).encode()
    new = json.dumps({"generation": "new", "rows": list(range(100))}).encode()
    # The seed pins SHARED_PROVIDER_MODE, not the umask (#1513): a 0o664 seed
    # makes the writer thread raise provider_destination_access_invalid. That
    # used to HANG the whole pytest session rather than fail it -- the writer
    # skipped `finished.set()` and the reader loop spun forever. It cannot
    # any more: the harness is fail-fast (#1633), setting the sentinel from a
    # `finally`, bounding both the spin-loop and the join, and asserting the
    # captured writer exception before anything about `observed`.
    write_provider_destination(destination, old)
    observed: list[bytes] = []

    def writer() -> None:
        for index in range(40):
            atomic_replace_provider_bytes(destination, new if index % 2 else old, max_bytes=4096)

    run_spin_wait_writer_harness(writer, lambda: observed.append(destination.read_bytes()))

    assert observed
    assert set(observed) <= {old, new}
    assert all(json.loads(content)["generation"] in {"old", "new"} for content in observed)


class InjectedWriterFailure(RuntimeError):
    """A writer failure with an identity of its own, so attribution is checkable."""


def test_spin_wait_harness_reports_the_writer_exception_not_the_empty_observation_symptom() -> None:
    """A raising writer must produce a bounded failure naming the cause (#1633).

    Pre-fix this shape hung forever; the `finally` alone would have made it
    PASS carrying only a PytestUnhandledThreadExceptionWarning, because
    `threading.Thread` swallows exceptions. The repository now escalates that
    exact warning to an error (#1646), but the global boundary does not prove
    this helper's direct-call semantics, cause-before-result ordering, or
    whole-process terminability, so the local capture and ordering assertions
    remain mandatory. Both halves are needed, so both are asserted here.

    The seam is the harness's own writer-body parameter -- deliberately NOT a
    monkeypatch. Patching `atomic_replace_provider_bytes` on
    `provider_atomic_module` would be INERT, because this module bound that name
    directly at import; and patching an inner call instead would have
    `atomic_replace_provider_bytes` convert the injected failure into a
    `ProviderAtomicError` (see `provider_restored_previous` above), erasing the
    identity this test exists to follow through the harness.
    """

    observed: list[bytes] = []

    def writer() -> None:
        # Fails where iteration 0 would be, i.e. before the reader loop has had
        # any chance to collect an observation: the D3 ordering case.
        raise InjectedWriterFailure("injected writer failure")

    def observe() -> None:
        """Collect nothing, so `observed` is deterministically empty here."""

    started = time.monotonic()
    with pytest.raises(AssertionError) as error_info:
        run_spin_wait_writer_harness(writer, observe)
        # Mirrors the real test's first substantive assertion. It must never be
        # reached: if the harness reported this instead, the failure would name
        # the downstream symptom rather than the writer's own exception.
        assert observed, "downstream empty-observation symptom"
    elapsed = time.monotonic() - started

    assert elapsed < SPIN_WAIT_HANG_BACKSTOP_SECONDS
    assert "InjectedWriterFailure" in str(error_info.value)
    assert "injected writer failure" in str(error_info.value)
    assert "downstream empty-observation symptom" not in str(error_info.value)
    assert not observed


def test_spin_wait_harness_deadline_catches_a_writer_that_blocks_instead_of_raising() -> None:
    """The loop deadline is the ONLY backstop for a blocked writer (#1633).

    The injection test above cannot reach this path: raising runs the `finally`,
    which sets the sentinel, so the deadline never fires there. A writer that
    blocks never reaches its `finally` at all.
    """

    release = threading.Event()
    threads: list[threading.Thread] = []

    def blocked_writer() -> None:
        # Bounded even if the release below were somehow skipped, so a failure
        # in this test cannot leak a live thread into the rest of the session.
        release.wait(timeout=SPIN_WAIT_HANG_BACKSTOP_SECONDS)

    def observe() -> None:
        """Collect nothing: this case is about the loop, not about the data."""

    # Explicitly short, not the 30s production backstop: the deadline is the
    # thing under test here, so waiting it out would just make the suite slow.
    deadline_seconds = 0.25
    started = time.monotonic()
    try:
        with pytest.raises(AssertionError) as error_info:
            run_spin_wait_writer_harness(
                blocked_writer,
                observe,
                deadline_seconds=deadline_seconds,
                started_threads=threads,
            )
        elapsed = time.monotonic() - started
        assert "spin-wait deadline" in str(error_info.value)
        assert deadline_seconds <= elapsed < SPIN_WAIT_HANG_BACKSTOP_SECONDS
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=SPIN_WAIT_HANG_BACKSTOP_SECONDS)

    assert threads and all(not thread.is_alive() for thread in threads)


def test_spin_wait_harness_permanently_blocked_writer_does_not_strand_interpreter_shutdown() -> None:
    """Whole-run terminability: a permanently blocked writer must not hang exit (#1633).

    The in-process deadline test releases its worker and joins it, so it cannot
    see what happens when the deadline assertion has fired while the blocked
    worker is still alive. Python cannot cancel that thread; if it is a
    non-daemon thread, `threading._shutdown()` waits for it without limit at
    interpreter exit and the pytest process hangs even though every assertion
    has already reported (spec obligation (b), design D8).

    This test therefore runs the REAL shared harness in a bounded child
    subprocess: the child imports `run_spin_wait_writer_harness`, passes a
    writer that blocks forever and a short deadline, and catches the expected
    deadline `AssertionError`. The daemon worker is the last-resort backstop
    that lets the child's interpreter exit while that writer remains blocked.
    The child must exit before this parent's external bound; if it does not,
    the parent kills the whole process group and the test fails.
    """

    deadline_seconds = 0.25
    external_timeout_seconds = 30.0
    repository = Path(__file__).resolve().parents[1]
    probe = f"""
import sys
from tests.test_scheduler_refresh_terminability_probes import run_spin_wait_writer_harness

blocked = __import__("threading").Event()

def writer() -> None:
    blocked.wait()

def observe() -> None:
    pass

try:
    run_spin_wait_writer_harness(writer, observe, deadline_seconds={deadline_seconds})
except AssertionError as error:
    if "spin-wait deadline" not in str(error):
        raise
    print("caught-spin-wait-deadline")
    # Deliberately DO NOT release `blocked` or join the worker: this probe is
    # the whole-run-terminability proof. Exiting while the injected writer is
    # still blocked must not strand `threading._shutdown()`.
    raise SystemExit(0)
print("unexpectedly-no-deadline")
raise SystemExit(1)
"""
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
            stdout, stderr = process.communicate(timeout=external_timeout_seconds)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "child did not exit within the external bound while the injected "
                "writer stayed blocked: a non-daemon harness worker strands "
                "threading._shutdown() (design D8). Killing the process group."
            ) from None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()

    assert stdout.strip() == "caught-spin-wait-deadline", (
        f"child printed {stdout.strip()!r} / {stderr.strip()!r}"
    )


class _SubprocessResult(NamedTuple):
    """Outcome of a bounded child run: output plus an explicit timed-out flag.

    The flag is load-bearing (task 5.6/Phase 2): a child that prints the
    expected checkpoint and then EXITS normally must be distinguishable from a
    child that prints it and then hangs. ``timed_out`` is ``True`` only when
    the external bound expired and the process group was killed/reaped.
    """

    stdout: str
    stderr: str
    timed_out: bool


def _run_bounded_subprocess(
    probe: str, *, timeout_seconds: float, checkpoint: str | None = None
) -> _SubprocessResult:
    """Run ``probe`` in a fresh interpreter under an external bound and reap on timeout.

    The module-local driver for whole-run terminability proofs (#1645): the
    child runs with its own process group, and if it outlives the external
    bound the whole group is SIGKILLed and reaped so a broken harness can never
    leak a live process. Returns ``(stdout, stderr, timed_out)``.

    A timeout never discards the child's partial progress (task 5.6): the
    ``TimeoutExpired`` partial stdout/stderr are preserved, and ``timed_out``
    explicitly distinguishes a timeout from a normal exit even when both carry
    the same checkpoint. On timeout the process group is killed and the pipes
    are drained via ``communicate()`` before reaping, so no unread PIPE data
    can block the reap. When ``checkpoint`` is given, a timeout whose partial
    output lacks the checkpoint is an unrelated import/startup stall and fails
    immediately.
    """

    repository = Path(__file__).resolve().parents[1]
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
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            partial_stdout = error.stdout.decode("utf-8", "replace") if error.stdout else ""
            partial_stderr = error.stderr.decode("utf-8", "replace") if error.stderr else ""
            # Kill the whole group, then drain the pipes via communicate() so
            # the reap never blocks on unread PIPE data. On supported Pythons
            # the drained stream is the COMPLETE captured output, so it is
            # authoritative when non-empty -- concatenating it with
            # ``error.stdout`` would duplicate already-read bytes.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            drained_stdout, drained_stderr = process.communicate()
            if drained_stdout:
                partial_stdout = drained_stdout
            if drained_stderr:
                partial_stderr = drained_stderr
            if checkpoint is not None and checkpoint not in partial_stdout:
                raise AssertionError(
                    f"child timed out BEFORE its {checkpoint!r} checkpoint: an "
                    "import/startup stall, not the expected terminability red. "
                    f"Partial stdout: {partial_stdout.strip()!r} / stderr: {partial_stderr.strip()!r}"
                ) from None
            return _SubprocessResult(partial_stdout, partial_stderr, timed_out=True)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        process.wait()
    return _SubprocessResult(stdout, stderr, timed_out=False)


def test_receipt_barrier_harness_whole_run_is_terminable_and_the_unbounded_mutant_times_out(
    tmp_path: Path,
) -> None:
    """Whole-run terminability of the real 40-way receipt harness (#1645, task 1.4).

    The in-process injection tests release and join every peer, so they cannot
    see what happens when the harness must FAIL while a pre-arrival worker
    exception is still pending. This proof runs the REAL module-local seam in a
    bounded child subprocess with the same pre-arrival failure injected: the
    repaired seam must report the failure and exit before the external bound. An
    isolated mutant that removes the Barrier timeout (restoring the unbounded
    non-daemon strand shape) must hit the external bound instead, proving the
    bound is load-bearing rather than a courtesy.
    """

    # External mutant bound has margin over the ~0.3s module import and the
    # short repaired leg (task 1.4): just enough to prove the unbounded mutant
    # cannot exit, not a minute-scale wait.
    external_timeout_seconds = 10.0
    barrier_bound_seconds = 2.0
    join_bound_seconds = 5.0
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)

    repaired_probe = f"""
import sys, threading, time
from pathlib import Path
from tests.test_scheduler_refresh_barrier_seam import (
    InjectedBarrierPeerFailure,
    run_bounded_barrier_peer_harness,
    refresh,
)

root = Path({str(root)!r})

def worker(index, barrier):
    if index == 0:
        raise InjectedBarrierPeerFailure("injected pre-arrival failure")
    barrier.wait()
    refresh._publish_primary_receipt(root, refresh._receipt(
        run_id=f"refresh_{{index:02d}}",
        started=refresh.datetime(2026, 7, 14, index // 60, index % 60, tzinfo=refresh.UTC),
        outcome="failed", reason="provider_invalid", phase="precommit", providers=[]))

started = time.monotonic()
errors, threads = run_bounded_barrier_peer_harness(
    worker, parties=40, barrier_timeout={barrier_bound_seconds}, join_timeout={join_bound_seconds})
elapsed = time.monotonic() - started
assert len(errors) == 40, len(errors)
by_index = dict(errors)
assert isinstance(by_index[0], InjectedBarrierPeerFailure), type(by_index[0]).__name__
assert all(isinstance(e, threading.BrokenBarrierError) for i, e in errors if i != 0)
assert all(not t.is_alive() for t in threads)
assert elapsed < {external_timeout_seconds}
print("repaired-whole-run-exited")
print(f"elapsed={{elapsed:.2f}}s")
print("errors:", [type(e).__name__ for i, e in sorted(errors)], "...")
print("alive:", sum(t.is_alive() for t in threads))
"""

    result = _run_bounded_subprocess(repaired_probe, timeout_seconds=external_timeout_seconds)
    assert not result.timed_out, (
        f"repaired child must exit before the external bound: "
        f"{result.stdout.strip()!r} / {result.stderr.strip()!r}"
    )
    assert "repaired-whole-run-exited" in result.stdout, (
        f"child printed {result.stdout.strip()!r} / {result.stderr.strip()!r}"
    )
    assert "BrokenBarrierError" in result.stdout, (
        f"child printed {result.stdout.strip()!r} / {result.stderr.strip()!r}"
    )

    mutant_probe = f"""
import sys, threading, time
from pathlib import Path
from tests.test_scheduler_refresh_barrier_seam import refresh

root = Path({str(root)!r})

# Mutant: Barrier WITHOUT the timeout bound, restoring the pre-fix unbounded
# non-daemon strand shape. Peer readiness is DETERMINISTIC (one peer must
# actually reach the barrier) and the injected pre-arrival failure is driven
# by a handshake Event, so the checkpoint below can only flush after both
# states hold. The main thread then prints and returns, exactly as the pre-fix
# harness's bare join/timeout would; interpreter shutdown must wait forever on
# the stranded peers, and the child's captured partial output proves the
# external timeout is the load-bearing terminability red, not a startup stall.
barrier = threading.Barrier(40)
peer_ready = threading.Event()
injected_now = threading.Event()
errors = []
def publish(receipt):
    try:
        peer_ready.set()
        barrier.wait()
        refresh._publish_primary_receipt(root, receipt)
    except BaseException as error:
        errors.append(error)

receipts = [
    refresh._receipt(run_id=f"refresh_{{index:02d}}",
        started=refresh.datetime(2026, 7, 14, index // 60, index % 60, tzinfo=refresh.UTC),
        outcome="failed", reason="provider_invalid", phase="precommit", providers=[])
    for index in range(40)
]
def injected(receipt):
    if receipt["run_id"] == "refresh_00":
        assert peer_ready.wait(timeout=5), "peer never reached the barrier"
        injected_now.set()
        raise RuntimeError("injected pre-arrival failure")
    publish(receipt)
threads = [threading.Thread(target=injected, args=(r,)) for r in receipts]
for t in threads:
    t.start()
assert peer_ready.wait(timeout=5), "no peer reached the barrier"
assert injected_now.wait(timeout=5), "injected failure never fired"
print("mutant-threads-started-no-join")
print("mutant-ready-injected-checkpoint", flush=True)
print("done", flush=True)
"""
    # The unbounded mutant must NOT exit: it must hit the external bound and be
    # killed/reaped. The partial output is preserved, and the expected red is
    # accepted ONLY when the post-readiness/injection checkpoint is present --
    # an import/startup stall before that checkpoint fails as unrelated (task 5.6).
    mutant_result = _run_bounded_subprocess(
        mutant_probe,
        timeout_seconds=external_timeout_seconds,
        checkpoint="mutant-ready-injected-checkpoint",
    )
    assert mutant_result.timed_out, (
        f"unbounded mutant must hit the external deadline: "
        f"{mutant_result.stdout.strip()!r} / {mutant_result.stderr.strip()!r}"
    )
    assert "mutant-ready-injected-checkpoint" in mutant_result.stdout, (
        f"expected mutant checkpoint missing from partial output: "
        f"{mutant_result.stdout.strip()!r} / {mutant_result.stderr.strip()!r}"
    )
    assert "mutant-threads-started-no-join" in mutant_result.stdout, (
        f"mutant never reached thread-start: {mutant_result.stdout.strip()!r} / {mutant_result.stderr.strip()!r}"
    )
    assert "repaired-whole-run-exited" not in mutant_result.stdout


def test_bounded_subprocess_rejects_a_timeout_before_the_checkpoint() -> None:
    """A child that stalls before its checkpoint is NOT counted as mutant red (#1645, task 5.6).

    The expected terminability red is load-bearing only when the child reached
    its post-readiness/injection checkpoint first. A child that hangs during
    import/startup -- before the checkpoint -- must fail as an unrelated
    startup timeout, proving the driver distinguishes the two instead of
    accepting any generic timeout.
    """

    stall_probe = (
        "import sys, time\n"
        "time.sleep(30)\n"
        "print('never-reached')\n"
    )
    try:
        _run_bounded_subprocess(
            stall_probe,
            timeout_seconds=2,
            checkpoint="checkpoint-never-flushed",
        )
    except AssertionError as error:
        assert "timed out BEFORE" in str(error), error
        assert "checkpoint-never-flushed" in str(error), error
        return
    raise AssertionError(
        "a pre-checkpoint stall was accepted as the expected mutant red; "
        "the checkpoint protocol is not load-bearing"
    )


def test_bounded_subprocess_distinguishes_a_checkpoint_then_normal_exit() -> None:
    """A child that prints the checkpoint and EXITS normally is NOT mutant red (#1645, task 5.6).

    A checkpoint alone must not satisfy the terminability oracle: the contract
    requires the unbounded mutant to HIT the external deadline, not merely to
    report readiness. The explicit ``timed_out`` discriminator is what separates
    a normal exit (``timed_out=False``) from a hang (``timed_out=True``), so a
    mutant that prints the checkpoint and exits can never be accepted as the
    expected red -- the ``test_receipt_barrier...`` mutant leg asserts
    ``timed_out is True``, which this shape cannot satisfy.
    """

    exits_probe = (
        "import sys\n"
        "print('mutant-ready-injected-checkpoint', flush=True)\n"
        "print('done', flush=True)\n"
    )
    result = _run_bounded_subprocess(
        exits_probe,
        timeout_seconds=2,
        checkpoint="mutant-ready-injected-checkpoint",
    )
    # The child exited normally with the checkpoint flushed: timed_out must be
    # False. If the driver reported this as the expected mutant red
    # (timed_out=True), the terminability oracle would be vacuous.
    assert not result.timed_out, (
        "a normal exit with the checkpoint was misclassified as a timeout: "
        f"{result.stdout.strip()!r} / {result.stderr.strip()!r}"
    )
    assert "mutant-ready-injected-checkpoint" in result.stdout, (
        f"child must have flushed the checkpoint: {result.stdout.strip()!r}"
    )
