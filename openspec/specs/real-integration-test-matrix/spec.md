# real-integration-test-matrix Specification

## Purpose
TBD - created by archiving change issue-126-real-integration-test-matrix. Update Purpose after archive.
## Requirements
### Requirement: Real database migrations are verified

The system SHALL provide an integration test lane that applies all `db/migrations/*.sql` files from an empty PostgreSQL database with PostGIS and TimescaleDB available, then verifies schema metadata and idempotency.

#### Scenario: Fresh real database migration

- **WHEN** the integration lane runs with `NHMS_RUN_INTEGRATION=1` and a real PostgreSQL/PostGIS/TimescaleDB `NHMS_INTEGRATION_DATABASE_URL`
- **THEN** every migration is applied in filename order, required extensions/schemas/enums/tables/indexes/constraints exist, PostGIS geometry columns have the expected SRID/type, Timescale hypertables exist where required, and a second migration pass skips already applied migrations without error

#### Scenario: Integration database unavailable in fast local tests

- **WHEN** the normal fast test command runs without `NHMS_RUN_INTEGRATION=1`
- **THEN** external-service integration tests are skipped or excluded intentionally, and fast unit/API tests continue to run without Docker or PostgreSQL

### Requirement: Real-schema API smoke covers production surfaces

The system SHALL provide real-schema API smoke tests for models, forecast-series, pipeline, and state-snapshots using deterministic seeded data.

#### Scenario: Core API smoke succeeds on real schema

- **WHEN** seeded records exist in the migrated real database for a model, river segment, hydro run, pipeline jobs, and a state snapshot
- **THEN** API requests for model listing/active-model discovery, river forecast-series, pipeline status/stages/jobs, and state snapshot list/detail return successful responses with expected identifiers and data fields

### Requirement: Worker chain smoke is deterministic

The system SHALL provide a bounded worker integration smoke that exercises canonical conversion, forcing production, SHUD dry-run or runtime mock, and output parsing using temporary local object-store data.

#### Scenario: Worker composition produces durable artifacts

- **WHEN** the worker smoke runs with synthetic input products and a temporary object store
- **THEN** each stage writes its expected manifest/artifact records and downstream stages consume those artifacts without external network, real S3, or real SHUD solver execution

### Requirement: Slurm gateway smoke uses fake binaries

The system SHALL provide real gateway smoke coverage using fake `sbatch`, `sacct`, `scancel`, and `sinfo` binaries on `PATH`.

#### Scenario: Fake Slurm command boundary

- **WHEN** the real Slurm gateway submits, inspects, cancels, and reads logs for a test job or array job through fake binaries
- **THEN** command arguments are shell-safe, job IDs and array task statuses are parsed correctly, queue status is reported, logs are read from the configured workspace, and no real Slurm cluster is required

### Requirement: Validation commands are layered

The repository SHALL document and/or encode separate validation commands for fast backend tests, real integration tests, frontend tests, and E2E tests.

#### Scenario: CI and developer command matrix is explicit

- **WHEN** a developer or CI runner needs to validate the project
- **THEN** it can run a documented fast command without external services, an explicit opt-in integration command with PostgreSQL/PostGIS/TimescaleDB service variables, frontend unit/build commands, and targeted E2E commands without guessing which services are required

### Requirement: Hermetic test oracles express their intent platform-portably

Hermetic tests SHALL express the same oracle on every platform the
suite supports (Linux CI and macOS development machines) — this
binds tests that execute embedded shell snippets, construct
filesystem fixtures, or trigger interpreter-version-sensitive
behavior: an embedded
snippet using a tool dialect unavailable on the running platform is
executed through a probed, pinned dialect substitution of exactly the
affected tool invocations — never by skipping the test and never by
weakening what the guard's control flow judges — while any
doc-equality assertion keeps comparing the canonical published
snippet text; a fixture path SHALL NOT depend on platform path
topology (such as a symlinked system tempdir) to reach the gate it
asserts, and where two refusal gates could answer, each gate gets its
own fixture row; a test that needs an interpreter-triggered failure
(such as `RecursionError`) SHALL pin inputs measured to trigger it
deterministically on every supported interpreter version rather than
on one version's internal limits. Green-for-the-wrong-reason is
treated as red: assertions name the specific refusal branch they
exercise, so a platform that diverts the control flow into a
different branch fails loudly instead of passing vacuously.

#### Scenario: A GNU-only snippet runs on a BSD-userland machine

- **WHEN** a hermetic test executes a guard snippet that invokes
  `stat -c` and the running platform's stat lacks `-c`
- **THEN** the test executes a copy with the pinned BSD-equivalent
  invocations substituted, the guard's control flow is otherwise
  byte-identical, the named refusal branch is still the one
  exercised, and the canonical GNU text remains what doc-equality
  assertions compare

#### Scenario: Each refusal gate has its own fixture row

- **WHEN** a fixture path could be refused by more than one gate
  (symlink-component refusal versus approved-root refusal)
- **THEN** the suite carries one row per gate — a resolved
  symlink-free path outside the approved root asserting the
  root-approval refusal, and an explicit symlink-bearing path
  asserting the symlink refusal — and neither row's outcome depends
  on the platform's tempdir topology

#### Scenario: Interpreter-version-sensitive triggers are pinned deterministically

- **WHEN** a test needs `RecursionError` from JSON parsing to
  exercise a never-raises error branch
- **THEN** the input depth is one measured to raise on every
  supported CPython version, the payload stays within the production
  size limit asserted in-test, and the adjacent non-recursive
  malformed shape (such as a top-level list) is pinned by its own
  independent case

#### Scenario: Wrong-branch passes are impossible

- **WHEN** a guard test's platform diverts execution into a
  different refusal branch than the one the test names
- **THEN** the test fails — its assertions bind the specific
  refusal message or error code of the named branch, not a generic
  refusal shape

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

### Requirement: Mocked e2e GFS lane MUST stay executable against the cloud-era adapter

The `-m e2e` mocked GFS pipeline tests SHALL drive the adapter over the
NOMADS-bundle backend only, with the backend chain pinned hermetically in the
fixture (immune to ambient GFS_SOURCE_BACKENDS), and SHALL serve every
manifest bundle entry a payload carrying all of that entry's bundle
variables, so the download and canonical stages execute real assertions
instead of failing on fixture drift.

#### Scenario: e2e lane runs green under the marker gate

WHEN `NHMS_RUN_E2E=1 pytest tests/test_e2e.py -m e2e` runs on a clean tree
THEN the m1 and m2 pipeline tests execute their assertions and pass

#### Scenario: f000 bundle short-count is asserted truthfully

WHEN forecast hour 0 is part of the exercised cycle
THEN the canonical product count expectation excludes the f000-unavailable
variables instead of demanding the full per-hour variable set

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

### Requirement: Barrier concurrency harnesses MUST fail bounded after pre-arrival failure

Every issue-owned pytest harness governed by this requirement SHALL bound its Barrier protocol
independently of successful worker progress. This applies when multiple workers depend on all
participants reaching a `threading.Barrier`. If a worker raises before Barrier arrival, or worker launch fails after
only a subset has started, the harness SHALL abort or otherwise break the Barrier so every
successfully started waiting peer leaves within the bound. The parent SHALL observe and attribute
worker, Future, launch, and cleanup failures before asserting race output; every successfully started
peer in this governed failure path SHALL terminate; and every returned Future SHALL be consumed
before the failure leaves the harness. Conformance SHALL preserve the harness's participant count,
real code-under-test calls, race window, and substantive concurrency assertions.

This requirement initially governs the four sites explicitly routed by #1645: the concurrent
idempotency reservation test in `tests/test_gateway_reconcile_idempotency_barrier.py`, the file-submit
collision test in `tests/test_gateway_reconcile_file_submit_barrier.py`, and the thread-lock
serialization and receipt-retention tests in `tests/test_scheduler_file_provider_refresh.py`. It does
not require an in-process test harness to cancel an already-started worker that blocks indefinitely
outside the Barrier, does not make a repository-wide claim about unrelated Barrier sites, and does
not replace global warning/timeout policy tracked by #1646.

#### Scenario: All participants arrive and the original race oracle is unchanged

- **WHEN** every participant reaches a governed Barrier and the code under test succeeds
- **THEN** the Barrier releases the same participant population into the same real concurrent
  operation
- **AND** the original winner/loser, serialization, state, retention, and result assertions remain
  unchanged and pass

#### Scenario: A participant raises before Barrier arrival

- **WHEN** one governed worker raises before reaching the Barrier
- **THEN** waiting peers leave with a bounded broken-barrier outcome rather than remaining stranded
- **AND** the parent reports the injected worker exception, peer failures, and any cleanup failure
  before any missing-result or state assertion
- **AND** every successfully started worker is joined and every returned Future is consumed before
  the failure leaves the harness

#### Scenario: Worker launch fails after partial success

- **WHEN** explicit-thread launch or executor submission raises after at least one peer or Future has
  started
- **THEN** the harness aborts the Barrier and joins or drains all successfully started peers or
  returned Futures under one cleanup deadline
- **AND** the original launch exception propagates after cleanup instead of being masked by
  broken-barrier or downstream result errors

#### Scenario: The whole pytest process remains terminable

- **WHEN** a bounded subprocess drives a governed harness through a pre-arrival worker-exception path
- **THEN** the subprocess exits before its external deadline after reporting the failure
- **AND** a mutant that removes the Barrier bound or restores the strand shape reaches and flushes
  its post-readiness failure checkpoint, then hits the external deadline and is killed/reaped,
  proving the terminability oracle is load-bearing rather than an unrelated startup timeout

#### Scenario: Boundedness is not a performance assertion

- **WHEN** a normal run executes on a loaded CI runner
- **THEN** the configured Barrier/join bound is a generous hang backstop rather than a tight duration
  SLA
- **AND** controlled injection tests may pass an explicitly shorter bound to remain fast

#### Scenario: Adjacent global policy remains separate

- **WHEN** repository-wide handling of `PytestUnhandledThreadExceptionWarning` or a global pytest
  timeout is considered
- **THEN** this change does not alter that policy, dependency, or configuration because #1646 owns
  the shape-independent decision

### Requirement: Production-state polling tests MUST distinguish unexpected causes from expected state outcomes

Each issue-owned pytest test that starts or observes a worker and polls production state (`heartbeat_seq`, `lost`, or a terminal-intent path) SHALL surface an unexpected cause observable at that test boundary before asserting worker-produced state or result collections. Expected production outcomes SHALL remain distinct: `_LeaseHeartbeat._run` SHALL continue mapping `renew()` exceptions to fail-closed `lost=True`; a real stolen-token renewal returning `False` SHALL remain the takeover oracle; and an expected terminal-intent `TerminalStateError` SHALL remain a substantive reader result. Captured-failure paths SHALL retain the existing poll/join bounds and complete test-owned thread and file-lock cleanup before the parent reports the cause.

This requirement governs only `test_lease_heartbeat_advances_then_detects_takeover` in `tests/test_production_scheduler.py` and `test_shared_finalizer_and_authoritative_reader_complete_without_deadlock` in `tests/test_node27_timeseries_compression_supervisor.py`, plus regression tests that exercise those exact shipping functions. It does not reclassify production-state polls as #1633 dedicated completion sentinels, change production modules or state semantics, or replace repository-wide warning/timeout policy tracked by #1646.

#### Scenario: Heartbeat renewal exception precedes sequence symptoms

- **WHEN** the real heartbeat calls the test-observed `renew()` boundary and it raises before the first successful sequence increment
- **THEN** production maps the failure to `lost=True`, while the shipping test reports the captured exception identity before asserting `heartbeat_seq`
- **AND** the bounded poll and heartbeat stop/join cleanup still complete

#### Scenario: Exception cannot impersonate a stolen-token takeover

- **WHEN** one real renewal succeeds, the test replaces the lease token, and a later observed renewal raises instead of returning `False`
- **THEN** the shipping test reports that exception before accepting `lost=True`
- **AND** in the unchanged normal path the real token mismatch returns `False`, `lost=True` remains the substantive takeover result, and no captured exception exists

#### Scenario: Production exception-to-lost mapping remains fail-closed

- **WHEN** `FileSchedulerLease.renew()` raises an `Exception` in a direct `_LeaseHeartbeat` control
- **THEN** `_LeaseHeartbeat._run` catches it and sets `lost=True` without requiring the exception to escape the production daemon
- **AND** no production heartbeat, lease, scheduler-runtime, or state contract is changed

#### Scenario: Unexpected finalizer or reader failure is cause-first

- **WHEN** the issue-owned supervisor test's finalizer or reader worker raises an unexpected `BaseException`
- **THEN** the parent reports the injected cause before asserting `finalizer_result` or `reader_result`
- **AND** both workers are bounded-joined and the parent-owned file lock is released and closed before that failure surfaces

#### Scenario: Expected terminal-intent error remains a result

- **WHEN** the finalizer records pending intent while the parent holds the terminal lock and the authoritative reader encounters the expected `TerminalStateError`
- **THEN** the finalizer result remains `False`, the expected error remains in `reader_result`, and subsequent final receipt publication still succeeds
- **AND** the expected domain error is not placed in the unexpected-worker-error channel

#### Scenario: Exact shipping owners prove attribution

- **WHEN** tracked regressions inject unique failures into first renewal, post-takeover renewal, finalizer, and reader call sites
- **THEN** each injected callable is proven to execute and direct invocation of the corresponding shipping test reports its unique cause
- **AND** removing only the shipping test's cause-observation leg restores a state symptom, a false-green, or a warning-only failure, so copied toy harnesses cannot satisfy the evidence

#### Scenario: Adjacent global policy remains separate

- **WHEN** repository-wide `PytestUnhandledThreadExceptionWarning`, pytest timeout, or cancellation policy is considered
- **THEN** this change does not edit that policy, dependency, or configuration because #1646 owns the shape-independent backstop

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

### Requirement: Integration teardown MUST NOT issue unbounded DELETEs against guarded hypertables

Integration test teardown SHALL bound every `DELETE` it issues against a
hypertable registered in the write guard's guarded set with a `valid_time` lower
and upper bound, and SHALL issue no `DELETE` at all when no rows match the
identity predicate. The bound SHALL be derived from the rows actually present
under that identity predicate, not from constants known to the seeding fixture,
so that extending the fixture cannot silently narrow what teardown removes.

This holds regardless of which identity column the statement uses, so that an
identity-column migration cannot drop the bound.

#### Scenario: Rows present under the identity predicate

- **WHEN** integration teardown cleans a guarded hypertable and rows exist under
  its identity predicate
- **THEN** it first reads the minimum and maximum `valid_time` present under that
  same identity predicate, and issues a single `DELETE` carrying both a
  `valid_time >=` lower bound and a `valid_time <=` upper bound covering that
  range

#### Scenario: No rows present under the identity predicate

- **WHEN** integration teardown cleans a guarded hypertable and no row exists
  under its identity predicate
- **THEN** no `DELETE` statement is issued against that hypertable, so that a
  compressed chunk elsewhere in the table cannot fail a delete that would have
  matched nothing

#### Scenario: Seeding fixture is extended with a new timestamp

- **WHEN** the seeding fixture writes a row under an existing identity at a
  `valid_time` outside the range it previously used
- **THEN** teardown still removes that row, because the bound is probed from the
  table rather than taken from the fixture's own constants

### Requirement: The per-run integration database name MUST be asserted as a generator property

Tests covering the integration lane's per-run database name SHALL assert
properties of the **generator** — what its output is a function of — and SHALL
NOT assert properties of a single random draw whose outcome depends on ambient
process state. An assertion whose pass or fail is decided by the value of
`os.getpid()`, the wall clock, or any other ambient input outside the code under
test does not express the property it claims: it reports a sampling accident,
and its failure rate is a function of the environment rather than of a defect.

Where the intended property is "the name does not depend on input X", the test
SHALL establish it by pinning the generator's declared source of randomness to a
known value and asserting the output equals the value derived from that pin
alone. Exact equality demonstrates that every unpinned input — including X —
contributes nothing.

#### Scenario: The name must be shown not to derive from the process id

- **WHEN** a test asserts that the per-run integration database name is not
  derived from the process id
- **THEN** it stubs the generator's randomness source to a fixed sentinel and
  asserts the produced name equals the name built from that sentinel alone,
  rather than searching the produced name for the process id's decimal digits

#### Scenario: A correct generator under an adversarial process id

- **WHEN** the test suite runs in an environment that assigns short process ids,
  such as inside a PID namespace where the pytest process is pid 1
- **THEN** the assertions on the database name pass deterministically, because
  no assertion's outcome depends on the process id's value or digit count

#### Scenario: Shape assertions remain

- **WHEN** the generator property is pinned by a stub
- **THEN** the unstubbed assertions on the real generator's output — that two
  consecutive names differ, that a name matches the declared `nhms_it_` plus
  32 hex-character shape, and that its suffix parses as a UUID — are retained,
  so the stub does not become the only thing under test

