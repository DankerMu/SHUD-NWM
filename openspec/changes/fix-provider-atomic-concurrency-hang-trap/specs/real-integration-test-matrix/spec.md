# Spec Delta: real-integration-test-matrix

## ADDED Requirements

### Requirement: Blocking-dependent concurrency tests MUST fail, never hang or silently pass

A test whose main thread cannot make progress until a worker thread reaches a synchronization point SHALL bound that wait, guarantee the synchronization point is reached on every exit path from the worker, capture any exception the worker raised, and assert on it — so a worker failure surfaces as a bounded, attributed test failure instead of an unbounded hang or a passing run carrying only a warning.

The governed shape is specifically **blocking dependence**: the main
thread waits on an `Event` the worker is expected to set, or on a
`Barrier` the worker is expected to reach. A bare `thread.join()` is
NOT governed — a worker that raises simply dies and the join returns,
so no hang is possible.

For a governed test, all of the following hold:

- the completion signal is set from a `finally`, so an exception cannot
  skip it;
- worker exceptions are collected (catch-all `except BaseException`)
  rather than left to die inside the thread;
- every wait is bounded — `Barrier(..., timeout=)` or `wait(timeout=)`
  for the synchronization point, `join(timeout=)` for the join, and an
  independent deadline on any main-thread spin loop, so the loop
  terminates even if the signal is never set at all;
- the assertions include an empty-error-list assertion and a
  no-thread-still-alive assertion, and **the error assertion comes
  first**, before any assertion on data the worker produced — a worker
  that fails on its first iteration leaves that data empty, and a
  data-first ordering reports the symptom instead of the cause.

Both the `finally` and the captured-exception assertion are required;
neither alone is sufficient. The `finally` alone converts the hang into
a silent pass, because `threading.Thread` swallows exceptions, pytest
downgrades them to `PytestUnhandledThreadExceptionWarning`, and this
repo declares no `filterwarnings`, so that warning fails nothing.

This governs the test harness only. It places no requirement on the
production code under test, and it does not authorize weakening a
concurrency oracle: iteration counts, real (non-doubled) calls into the
code under test, and the substantive assertions about observed state are
unaffected by conforming a harness to this shape.

Known exceptions, tracked and deliberately not fixed by the change that
introduced this requirement: `tests/test_gateway_reconcile.py:3574` and
`tests/test_gateway_reconcile.py:10028` both use an unbounded
`threading.Barrier` reached after a constructor that can raise, and are
governed but non-conforming pending issue #1645.

#### Scenario: A worker that raises produces a bounded, attributed failure

- **WHEN** the worker thread of a governed test raises before it would
  normally signal completion
- **THEN** the `finally` sets the signal so the waiting main thread
  exits, the exception is captured, the join returns within its bounded
  timeout, and the test FAILS naming that exception
- **AND** the run does NOT hang, and does NOT pass carrying only a
  `PytestUnhandledThreadExceptionWarning`

#### Scenario: The cause is reported, not the symptom

- **WHEN** the worker raises on its first iteration, so the collection
  the main thread was filling is still empty
- **THEN** the reported failure is the captured worker exception, not a
  downstream assertion about the empty collection

#### Scenario: The waiting loop is bounded independently of the signal

- **WHEN** a main-thread loop spins until a completion signal is set
- **THEN** the loop also carries its own deadline, so it terminates even
  if the signal is never set at all

#### Scenario: A bare join is not governed

- **WHEN** a test starts worker threads and only calls `thread.join()`,
  with no `Event` or `Barrier` the main thread depends on
- **THEN** this requirement does not apply, because a worker that raises
  dies and the join returns — there is no hang to prevent

#### Scenario: Conforming a harness does not weaken its oracle

- **WHEN** a governed test is converted to this harness shape
- **THEN** its substantive assertions, its iteration count, and its use
  of the real code under test are preserved unchanged, and only the
  signalling, waiting, and error-surfacing mechanics change
