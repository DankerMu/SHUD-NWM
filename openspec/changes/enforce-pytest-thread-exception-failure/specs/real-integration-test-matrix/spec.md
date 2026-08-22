## ADDED Requirements

### Requirement: Repository pytest MUST fail on unhandled worker-thread exceptions

Repository pytest configuration SHALL treat `pytest.PytestUnhandledThreadExceptionWarning` as an error. The policy SHALL target that exact category, preserve unrelated warning behavior, retain the worker's original cause in failure output, and execute in local, targeted, full, and real-integration pytest lanes installed from the repository development dependencies. Changes to either the pytest configuration or dependency lock SHALL select and execute the dedicated semantic policy suite in PR CI.

This repository-wide boundary is defense in depth. It SHALL NOT replace harness-owned exception capture, cause-before-result ordering, peer release, bounded joins/waits, cleanup, or whole-process terminability proof where an issue-specific harness contract requires those stronger outcomes.

#### Scenario: Unhandled worker exception fails with its cause

- **WHEN** a throwaway test starts and joins a worker that raises a unique `RuntimeError` while the test body otherwise returns normally under repository pytest configuration
- **THEN** pytest exits nonzero and reports both the unique cause and `PytestUnhandledThreadExceptionWarning`
- **AND** the same config with only the exact filter removed exits zero with a passing test plus that warning, proving the shipping policy is load-bearing

#### Scenario: Unrelated warnings and baseline failures retain their behavior

- **WHEN** a test emits an unrelated `UserWarning` under repository configuration
- **THEN** the test remains passing with a warning rather than failing
- **AND** repository configuration adds no broad all-warning error filter or custom thread-exception hook
- **AND** final-head tracked tests and `conftest.py` files contain no explicit warning-filter override of this policy
- **AND** ordinary pytest markers or executed Python warning-filter calls are documented as intentional, reviewable overrides rather than claimed to be statically impossible
- **AND** a pre-existing non-thread-policy test failure is reported against its owning issue rather than suppressed, fixed out of scope, or counted as thread-warning debt

#### Scenario: Config and lock changes execute the policy owner

- **WHEN** `pyproject.toml` or `uv.lock` changes in a pull request
- **THEN** the targeted selector includes the dedicated thread-exception policy suite and selector meta-guard while retaining its prior core-smoke ownership
- **AND** final-head CI executes those assertions rather than reporting only a collect-only smoke

#### Scenario: Existing harness contracts remain stronger

- **WHEN** an issue-owned harness captures/re-raises a worker cause, releases peers, joins workers, cleans resources, or proves process termination
- **THEN** those local assertions remain required and unchanged
- **AND** a globally escalated warning cannot be used to waive a missing release, ordering, cleanup, or termination leg

### Requirement: Universal pytest timeout policy MUST be evidence-calibrated

A universal per-test timeout plugin/configuration SHALL NOT be adopted from whole-job duration evidence. Before any future global timeout is enabled, the repository SHALL have per-test setup/body/teardown distributions for every affected marker lane, SHALL select and prove a timeout method against the required process-termination and teardown/reporting contract, and SHALL separately cover hangs that begin after a passing test's timer is canceled plus any child-process boundary. Until those prerequisites exist, issue-owned local protocol bounds and CI job-level timeouts remain the explicit hang backstops.

This decision adds no `pytest-timeout` dependency, no global `timeout`/`timeout_method`/timeout addopts, no marker-only `@pytest.mark.timeout` annotations, and no CI job-timeout change. The marker-only path still needs the same dependency plus calibrated per-test values/methods; current known concurrency harnesses already carry more precise local bounds, while annotating an incomplete subset would make an unsupported coverage claim. This decision does not weaken #1633/#1645/#1648 local harness requirements, solve #1671's slow full-job lane, or treat #1632's marker-lane umask work as duration evidence.

#### Scenario: Whole-job timing does not set a per-test bound

- **WHEN** a full pytest job is measured as slow-but-finite near its CI `timeout-minutes` boundary
- **THEN** that session-total measurement is not used as a universal per-test timeout value
- **AND** no marker expression, test selection, or assertion is removed to manufacture a safe-looking bound

#### Scenario: Timeout method trade-offs are explicit

- **WHEN** a future timeout proposal chooses a signal-based method that interrupts a test or a thread-based method that hard-exits the process
- **THEN** it proves the required termination behavior and explicitly accepts or preserves fixture teardown, cleanup, JUnit/evidence output, and remaining-test semantics
- **AND** platform fallback behavior is covered rather than assumed equivalent

#### Scenario: Warning escalation makes no hang claim

- **WHEN** a worker blocks without raising, or a test returns while a live non-daemon worker later strands interpreter shutdown
- **THEN** the warning-as-error policy makes no claim to catch that condition
- **AND** the applicable local harness/process bound or CI job timeout remains necessary

## MODIFIED Requirements

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

Both the `finally` and the captured-exception assertion remain required in that
implementation. Repository pytest now escalates an escaped
`PytestUnhandledThreadExceptionWarning` as defense in depth, but that global
boundary does not prove the helper's direct-call semantics, cause-before-result
ordering, resource cleanup, peer release, or whole-process terminability. A
warning failure therefore cannot substitute for either local leg.

This governs the test harness only. It places no requirement on the
production code under test, and it does not authorize weakening a
concurrency oracle: iteration counts, real (non-doubled) calls into the
code under test, and the substantive assertions about observed state are
unaffected by conforming a harness to this shape.

Adjacent hazards are governed rather than grandfathered. The
`Barrier`-mediated variant — a worker that raises before reaching a Barrier,
stranding its peers — is the same hang family but not a spin-wait completion
harness; the adjacent Barrier requirement governs its pre-arrival and partial
launch paths.

The adjacent production-state polling requirement governs diagnostic quality
for `heartbeat_seq`, `lost`, and terminal-intent polls rather than a dedicated
harness completion sentinel. Those polls are outside this requirement, not
exceptions to it. In particular, production `_LeaseHeartbeat._run` catches renew
exceptions and maps them to `lost = True`, so this requirement SHALL NOT claim
all three are uncaught-thread-exception paths. The repository-wide exact warning
escalation and evidence prerequisites for any universal timeout are governed by
this change's adjacent requirements.

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
