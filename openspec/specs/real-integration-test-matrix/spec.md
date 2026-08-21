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

