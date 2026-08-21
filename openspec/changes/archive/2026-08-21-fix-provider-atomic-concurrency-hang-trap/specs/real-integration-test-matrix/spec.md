# Spec Delta: real-integration-test-matrix

## ADDED Requirements

### Requirement: Spin-wait concurrency harnesses MUST fail, never hang or silently pass

A test-owned concurrency harness whose main thread spin-waits on a dedicated completion sentinel SHALL guarantee the sentinel is set on every exit path from the harness-owned worker, bound the spin-loop with its own deadline, bound the join, capture any exception the worker raised, and assert on it — so a worker failure surfaces as a bounded, attributed test failure instead of an unbounded hang or a run that passes carrying only a warning.

The governed shape requires all three properties:

1. the test itself starts the worker;
2. the harness owns a dedicated completion sentinel whose sole purpose is to
   release the main thread's polling loop; and
3. the main thread polls that sentinel before joining and checking worker
   output.

The trigger is defined by that ownership/dependency contract, not by CPU burn:
polling may include `time.sleep()`, and carrying a deadline satisfies obligation
(b) rather than removing the harness from scope.

Not governed: a blocking `event.wait(timeout=N)`; a bare `thread.join()` whose
workers share no other synchronization point; a poll of production state that
is itself under assertion (for example a heartbeat counter, lease-loss flag, or
terminal-intent path); a subprocess-ready file; or inter-worker synchronization
such as `Barrier`. Those may need their own diagnostics or bounds, but they are
not dedicated harness-completion sentinels.

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
sentinel from a `finally` (a); give the spin-loop its own deadline and the join a
`timeout=`, and make the test-local worker daemon so a permanently blocked
thread cannot strand interpreter shutdown (b); collect worker exceptions via a
catch-all `except BaseException` into a list, and assert that list is empty — and
that no thread is still alive — before the substantive assertions (c). The
daemon backstop SHALL be proven with a bounded subprocess that catches the
main-thread deadline assertion and still exits while its injected writer remains
permanently blocked. Controlled in-process blockers SHALL still be released and
joined; daemon status does not excuse cleanup.

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

Adjacent hazards are routed rather than grandfathered. The
`Barrier`-mediated variant — a worker that raises before reaching an unbounded
`threading.Barrier`, stranding its peers — is the same hang family but not a
spin-wait completion harness; #1645 tracks it. Until #1645 lands, those tests can
still strand non-daemon peers and hang the run.

#1648 tracks diagnostic quality in three tests that poll production state
(`heartbeat_seq`, `lost`, terminal-intent path) rather than a dedicated harness
completion sentinel. They are outside this requirement, not exceptions to it.
In particular, production `_LeaseHeartbeat._run` catches renew exceptions and
maps them to `lost = True`, so this requirement SHALL NOT claim all three are
uncaught-thread-exception paths. The repo-wide warning/timeout backstops that
operate independently of harness shape are tracked in #1646.

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

#### Scenario: A blocked worker is caught and the whole run terminates

- **WHEN** the harness-owned worker blocks indefinitely instead of raising, so
  it never reaches its `finally` and the completion sentinel is never set
- **THEN** the spin-loop terminates at its own deadline and the test fails on the
  missing completion sentinel
- **AND** a bounded subprocess that catches that assertion still exits while the
  injected writer remains blocked, proving interpreter shutdown is not stranded
- **AND** changing the harness worker back to non-daemon makes that subprocess
  hit its parent's external timeout, proving the terminability test bites

#### Scenario: Production-state polls and other waits are not governed

- **WHEN** a test waits via `event.wait(timeout=N)`, performs an independent bare
  join, polls production state or a subprocess-ready file, or coordinates
  workers through an inter-worker synchronization primitive
- **THEN** this requirement does not apply because the object is not a dedicated
  completion sentinel owned by a test harness
- **AND** this exclusion makes no safety claim: a bare join whose workers share
  an unbounded `Barrier` can still hang and is routed to #1645, while
  symptom-first assertions on production state are tracked by #1648

#### Scenario: Conforming a harness does not weaken its oracle

- **WHEN** a governed harness is converted to this shape
- **THEN** its substantive assertions, its iteration count, and its use
  of the real code under test are preserved unchanged, and only the
  signalling, waiting, and error-surfacing mechanics change
