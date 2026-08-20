# Spec Delta: real-integration-test-matrix

## ADDED Requirements

### Requirement: Spin-wait concurrency harnesses MUST fail, never hang or silently pass

A test whose main thread spin-waits on a completion sentinel that a worker thread is expected to set SHALL guarantee the sentinel is set on every exit path from the worker, bound the spin-loop with its own deadline, bound the join, capture any exception the worker raised, and assert on it — so a worker failure surfaces as a bounded, attributed test failure instead of an unbounded hang or a run that passes carrying only a warning.

The governed shape is narrow and deliberate: the main thread's only
exit condition is a sentinel the worker itself must set, and it burns
CPU until that happens. A bounded `event.wait(timeout=N)` is not a
spin-wait and is not governed — it already terminates on its own. A
bare `thread.join()` is not governed either: a worker that raises
simply dies and the join returns, so no hang is possible.

For a governed harness, all of the following hold:

- the sentinel is set from a `finally`, so an exception in the worker
  cannot skip it;
- worker exceptions are collected (catch-all `except BaseException`)
  rather than left to die inside the thread;
- the spin-loop carries an independent deadline, so it terminates even
  if the sentinel is never set at all — which is the only backstop when
  a worker *blocks* rather than raises, since a blocked worker never
  reaches its `finally`;
- the join is bounded (`join(timeout=)`);
- the assertions include an empty-error-list assertion and a
  no-thread-still-alive assertion, and **the error assertion comes
  first**, before any assertion on data the worker produced — a worker
  that fails on its first iteration leaves that data empty, and a
  data-first ordering reports the symptom instead of the cause.

Both the `finally` and the captured-exception assertion are required;
neither alone suffices. The `finally` alone converts the hang into a
silent pass or a misattributed failure, because `threading.Thread`
swallows exceptions, pytest downgrades them to
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
widened form of this requirement when fixed. The repo-wide backstops
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

#### Scenario: Bounded waits and bare joins are not governed

- **WHEN** a test's main thread waits via `event.wait(timeout=N)`, or
  starts workers and only calls `thread.join()` with no sentinel it
  depends on
- **THEN** this requirement does not apply — the first already
  terminates on its own, and in the second a worker that raises dies and
  the join returns

#### Scenario: Conforming a harness does not weaken its oracle

- **WHEN** a governed harness is converted to this shape
- **THEN** its substantive assertions, its iteration count, and its use
  of the real code under test are preserved unchanged, and only the
  signalling, waiting, and error-surfacing mechanics change
