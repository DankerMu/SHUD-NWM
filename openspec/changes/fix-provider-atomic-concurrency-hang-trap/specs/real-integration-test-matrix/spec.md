# Spec Delta: real-integration-test-matrix

## ADDED Requirements

### Requirement: Spin-wait concurrency harnesses MUST fail, never hang or silently pass

A test whose main thread spin-waits on a completion sentinel that a worker thread is expected to set SHALL guarantee the sentinel is set on every exit path from the worker, bound the spin-loop with its own deadline, bound the join, capture any exception the worker raised, and assert on it — so a worker failure surfaces as a bounded, attributed test failure instead of an unbounded hang or a run that passes carrying only a warning.

The governed shape is defined by **dependency**, not by CPU burn: the
main thread's progress depends on a sentinel that a worker thread must
set, and it waits by polling that sentinel — with or without a
`time.sleep()` between reads. Carrying a deadline on that poll
**satisfies (b); it does not exit the trigger.** Stating this matters:
scoping the trigger to undeadlined loops would make (b) vacuous, since
every loop that could violate it would fall outside by construction.

Not governed: a blocking `event.wait(timeout=N)`, which terminates on
its own without polling; and a bare `thread.join()` over workers that
depend on no sentinel and on no synchronization point with each other,
where a worker that raises simply dies and the join returns.

For a governed harness the obligations are stated as **outcomes**, not
as a mandated code shape:

- **(a) Release** — every exit path from the worker releases the main
  thread's wait, so no worker outcome can strand it;
- **(b) Bound** — the main thread's wait carries its own upper bound,
  independent of the worker doing anything, so it terminates even if the
  release in (a) never happens. The bound must make the **run**
  terminable, not merely the asserting thread: if workers are left
  stranded on a synchronization point and they are non-daemon threads,
  `threading._shutdown()` joins them without limit at interpreter exit
  and the process never exits, even though every assertion has already
  reported. A bounded `join()` plus a fired assertion does NOT satisfy
  (b) while a stranded non-daemon worker remains;
- **(c) Attribute** — a worker exception is surfaced and attributed, and
  that surfacing comes **before** any assertion on data the worker
  produced. A worker that fails on its first iteration leaves that data
  empty, and a data-first ordering reports the symptom instead of the
  cause.

Mechanism is deliberately not prescribed, because prescribing it
misjudges correct code: a `ThreadPoolExecutor` whose
`Future.result(timeout=N)` re-raises satisfies (a), (b) and (c) with no
error list, no `finally`, and no liveness assertion anywhere. A rule
written in terms of those constructs would flag it, wrongly.

One conforming implementation, and the one this change adopts: set the
sentinel from a `finally` (a); give the spin-loop its own deadline and
the join a `timeout=` (b); collect worker exceptions via a catch-all
`except BaseException` into a list, and assert that list is empty — and
that no thread is still alive — before the substantive assertions (c).

Both the `finally` and the captured-exception assertion are required in
that implementation; neither alone suffices. The `finally` alone
converts the hang into a silent pass or a misattributed failure, because
`threading.Thread` swallows exceptions, pytest downgrades them to
`PytestUnhandledThreadExceptionWarning`, and this repo declares no
`filterwarnings`, so that warning fails nothing.

This governs the test harness only. It places no requirement on the
production code under test, and it does not authorize weakening a
concurrency oracle: iteration counts, real (non-doubled) calls into the
code under test, and the substantive assertions about observed state are
unaffected by conforming a harness to this shape.

Adjacent hazards, routed rather than grandfathered: the
`Barrier`-mediated variant — a worker that raises before reaching an
unbounded `threading.Barrier`, stranding its peers — is the same failure
class but is not a spin-wait and so falls outside this requirement's
trigger; it is tracked in issue #1645 and is expected to come under a
widened form of this requirement when fixed. Recorded reason for not
closing it here: those sites lie outside the blast radius of this
test-only, single-file change, and fixing them requires touching a
different suite with its own DB-backed setup. Residual risk, disclosed
rather than implied absent: until #1645 lands, a constructor failure in
either of those two tests still strands its peers and hangs the run —
this requirement does not currently protect them. Three further sites are governed by this requirement and satisfy (a) and
(b) but violate (c): `tests/test_production_scheduler.py:44138` and
`:44149`, and
`tests/test_node27_timeseries_compression_supervisor.py:974`. Each polls
a sentinel set by a worker thread, captures no worker exception, and
asserts on worker-produced data first. Recorded reason for not closing
them here: they span two other suites with their own fixtures and thread
models, outside this single-file change's blast radius. Residual risk:
until issue #1648 lands, a worker failure in those three surfaces as a
data-shaped assertion failure rather than the real exception — a
debugging cost, not a correctness hole, since (b) holds and they neither
hang nor pass silently. The repo-wide backstops
that would close the class independently of harness shape
(`filterwarnings = ["error::pytest.PytestUnhandledThreadExceptionWarning"]`,
`pytest-timeout`) are tracked in issue #1646.

#### Scenario: A worker that raises produces a bounded, attributed failure

- **WHEN** the worker thread of a governed harness raises before it
  would normally set the sentinel
- **THEN** the `finally` sets the sentinel so the spin-loop exits, the
  exception is captured, the join returns within its bounded timeout,
  and the test FAILS naming that exception
- **AND** the run does NOT hang, and does NOT pass carrying only a
  `PytestUnhandledThreadExceptionWarning`

#### Scenario: The cause is reported, not the symptom

- **WHEN** the worker raises on its first iteration, so the collection
  the main thread was filling is still empty
- **THEN** the reported failure is the captured worker exception, not a
  downstream assertion about the empty collection

#### Scenario: A blocked worker is caught by the loop deadline

- **WHEN** the worker blocks indefinitely instead of raising, so it
  never reaches its `finally` and the sentinel is never set
- **THEN** the spin-loop still terminates at its own deadline and the
  test fails, because the `finally` is unreachable in this case and the
  deadline is the only backstop

#### Scenario: Bounded waits and independent bare joins are not governed

- **WHEN** a test's main thread waits via `event.wait(timeout=N)`, or
  starts workers and only calls `thread.join()` where the workers depend
  on no sentinel **and on no synchronization point with each other**
- **THEN** this requirement does not apply — the first already
  terminates on its own, and in the second a worker that raises dies and
  the join returns
- **AND** the qualifier is load-bearing: a bare `join()` over workers
  that DO share a synchronization point can still hang, because a worker
  that dies early strands its peers there and the join never returns.
  `tests/test_gateway_reconcile.py:3591` is exactly that shape, which is
  why it is routed to #1645 rather than excluded here

#### Scenario: Conforming a harness does not weaken its oracle

- **WHEN** a governed harness is converted to this shape
- **THEN** its substantive assertions, its iteration count, and its use
  of the real code under test are preserved unchanged, and only the
  signalling, waiting, and error-surfacing mechanics change
