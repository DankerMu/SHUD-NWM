# real-integration-test-matrix — delta for display-warmer-stop-semantics (#1276)

## ADDED Requirements

### Requirement: Background daemons started by app factories are stoppable and test-session-hygienic

Background daemon threads SHALL be stoppable — a thread started as a
side effect of building an application (such as the display-catalog
warmer) exposes a stop hook that signals a stop event, joins within a
bounded timeout, and resets the module's started-state only after a
successful join, returning a truthy result the caller can assert; a
join timeout returns falsy WITHOUT resetting state so a stuck thread
fails loudly rather than permitting a silent second thread. The test
suite SHALL invoke the stop hook between tests (autouse teardown) so
no such thread outlives the test that caused it, and a guard test
SHALL pin liveness, stop, absence after stop, restartability by
name against `threading.enumerate()`, loop-body replay liveness (a
faked replay target is really invoked), and the loud join-timeout
path. A test that replaces a process-global clock with an
exhaustible iterator SHALL consume it with an explicit default
chosen so every deadline comparison in reach stays decidable (for
monotonic clocks: `float("inf")`), so consumption by a concurrent
thread degrades to a pinned clock instead of `StopIteration` or an
unreachable timeout branch.
Production lifecycle is unaffected: in the live process the daemon
runs until process exit; the stop hook exists for callers that own
the lifecycle, and the stop event doubles as the loop's interval
sleep so stopping needs no waiting-out of the full interval.

#### Scenario: Stop hook joins and resets, loudly on timeout

- **WHEN** the stop hook is called while the warm thread is running
- **THEN** the stop event ends the thread's current wait, the thread
  is joined within the bounded timeout, started-state and handle are
  reset, and the hook returns truthy; and **WHEN** the join times
  out **THEN** the hook returns falsy, started-state is NOT reset,
  and the event remains set so the stuck thread still exits at its
  next wait

#### Scenario: No daemon outlives its test

- **WHEN** any test builds a display-readonly application (starting
  the warmer as a side effect) and finishes
- **THEN** the autouse teardown stops the thread and asserts
  success, and a session-level probe of `threading.enumerate()`
  after such suites finds no `display-catalog-warmer` thread

#### Scenario: Exhaustible patched clocks degrade instead of raising

- **WHEN** a test patches the process-global `time.monotonic` with a
  finite iterator and a concurrent thread consumes extra elements
- **THEN** the test's clock reads continue at the explicit default
  (`float("inf")`) — `StopIteration` is impossible at the patched
  sites and every `monotonic() >= deadline` timeout branch remains
  reachable rather than degrading into an unbounded spin
