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

